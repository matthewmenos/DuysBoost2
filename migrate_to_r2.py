"""
migrate_to_r2.py — One-time migration script.

Moves all base64-encoded media blobs currently stored in PostgreSQL
into Cloudflare R2, then replaces the blobs with public CDN URLs.

Tables migrated:
  users.avatar_url         (data-URI  → R2 URL)
  users.banner_url         (data-URI  → R2 URL)
  posts.media_data         (data-URI  → posts.media_url)
  messages.file_data       (data-URI  → messages.file_url)
  group_messages.file_data (data-URI  → group_messages.file_url)

Usage:
    python migrate_to_r2.py

Safe to re-run — skips rows that already have an R2 URL or no data.
Set DATABASE_URL and all R2_* vars in your environment (or .env) first.
"""

import os
import sys

from dotenv import load_dotenv
load_dotenv()

import psycopg2
import psycopg2.extras
import storage  # our storage.py module

DB_URL = os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL', '')
if not DB_URL:
    sys.exit('ERROR: DATABASE_URL is not set.')
if DB_URL.startswith('postgres://'):
    DB_URL = 'postgresql://' + DB_URL[len('postgres://'):]

conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
conn.autocommit = False


def _is_data_uri(value: str) -> bool:
    return bool(value and value.startswith('data:'))


def _is_r2_url(value: str) -> bool:
    """Check if a value is already an R2 CDN URL (not a data-URI blob)."""
    if not value:
        return False
    try:
        base = storage._public_url_base()
        return value.startswith(base) or value.startswith('https://')
    except RuntimeError:
        return False


# ── Per-table migration functions ─────────────────────────────────────────────

def add_columns_if_missing():
    """Add new *_url columns if they don't already exist (idempotent)."""
    cur = conn.cursor()
    migrations = [
        ('posts',          'media_url', 'TEXT'),
        ('messages',       'file_url',  'TEXT'),
        ('group_messages', 'file_url',  'TEXT'),
    ]
    for table, col, col_type in migrations:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
        """, (table, col))
        if not cur.fetchone():
            cur.execute(f'ALTER TABLE {table} ADD COLUMN {col} {col_type}')
            print(f'  + Added column {table}.{col}')
        else:
            print(f'  ✓ Column {table}.{col} already exists')
    conn.commit()


def migrate_users():
    """Migrate users.avatar_url and users.banner_url data-URIs → R2 URLs."""
    cur = conn.cursor()
    cur.execute('SELECT id, avatar_url, banner_url FROM users')
    rows = cur.fetchall()
    updated = 0

    for row in rows:
        uid     = row['id']
        updates = {}

        for col, prefix in [
            ('avatar_url', f'avatars/{uid}'),
            ('banner_url', f'banners/{uid}'),
        ]:
            val = row[col]
            if not val or not _is_data_uri(val):
                continue  # already a URL, empty, or unknown format
            try:
                url = storage.upload_data_uri(val, prefix)
                updates[col] = url
            except Exception as e:
                print(f'  ✗ user {uid} {col}: {e}')

        if updates:
            set_clause = ', '.join(f'{c} = %s' for c in updates)
            cur.execute(
                f'UPDATE users SET {set_clause} WHERE id = %s',
                list(updates.values()) + [uid]
            )
            updated += 1
            print(f'  ✓ user {uid}: migrated {list(updates.keys())}')

    conn.commit()
    print(f'  Users done: {updated}/{len(rows)} rows updated\n')


def migrate_posts():
    """Migrate posts.media_data (base64 blob) → posts.media_url (R2 URL)."""
    cur = conn.cursor()

    # Old column may not exist on a fresh install
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'posts' AND column_name = 'media_data'
    """)
    if not cur.fetchone():
        print('  posts.media_data column not found — skipping (fresh install)\n')
        return

    cur.execute(
        'SELECT id, user_id, media_data FROM posts WHERE media_data IS NOT NULL'
    )
    rows = cur.fetchall()
    updated = 0

    for row in rows:
        if not _is_data_uri(row['media_data']):
            continue
        try:
            url = storage.upload_post_media(row['user_id'], row['media_data'])
            cur.execute(
                'UPDATE posts SET media_url = %s, media_data = NULL WHERE id = %s',
                (url, row['id'])
            )
            updated += 1
            if updated % 25 == 0:
                conn.commit()
                print(f'  posts: {updated} migrated…')
        except Exception as e:
            print(f'  ✗ post {row["id"]}: {e}')

    conn.commit()
    print(f'  Posts done: {updated}/{len(rows)} rows updated\n')


def migrate_messages():
    """Migrate messages.file_data (base64 blob) → messages.file_url (R2 URL)."""
    cur = conn.cursor()

    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'messages' AND column_name = 'file_data'
    """)
    if not cur.fetchone():
        print('  messages.file_data column not found — skipping (fresh install)\n')
        return

    cur.execute("""
        SELECT m.id, m.file_data, m.conversation_id
        FROM messages m WHERE m.file_data IS NOT NULL
    """)
    rows = cur.fetchall()
    updated = 0

    for row in rows:
        if not _is_data_uri(row['file_data']):
            continue
        try:
            url = storage.upload_message_file(row['conversation_id'], row['file_data'])
            cur.execute(
                'UPDATE messages SET file_url = %s, file_data = NULL WHERE id = %s',
                (url, row['id'])
            )
            updated += 1
            if updated % 25 == 0:
                conn.commit()
                print(f'  messages: {updated} migrated…')
        except Exception as e:
            print(f'  ✗ message {row["id"]}: {e}')

    conn.commit()
    print(f'  Messages done: {updated}/{len(rows)} rows updated\n')


def migrate_group_messages():
    """Migrate group_messages.file_data → group_messages.file_url."""
    cur = conn.cursor()

    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'group_messages' AND column_name = 'file_data'
    """)
    if not cur.fetchone():
        print('  group_messages.file_data column not found — skipping (fresh install)\n')
        return

    cur.execute(
        'SELECT id, file_data, group_id FROM group_messages WHERE file_data IS NOT NULL'
    )
    rows = cur.fetchall()
    updated = 0

    for row in rows:
        if not _is_data_uri(row['file_data']):
            continue
        try:
            url = storage.upload_group_file(row['group_id'], row['file_data'])
            cur.execute(
                'UPDATE group_messages SET file_url = %s, file_data = NULL WHERE id = %s',
                (url, row['id'])
            )
            updated += 1
            if updated % 25 == 0:
                conn.commit()
                print(f'  group_messages: {updated} migrated…')
        except Exception as e:
            print(f'  ✗ group_message {row["id"]}: {e}')

    conn.commit()
    print(f'  Group messages done: {updated}/{len(rows)} rows updated\n')


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('=' * 60)
    print('DUYS Boost — Cloudflare R2 Migration')
    print('=' * 60)

    # Verify R2 connection before touching the database
    result = storage.check_connection()
    if not result['ok']:
        sys.exit(f'\nERROR: Cannot connect to R2: {result["error"]}')
    print(f'\n✓ R2 connected')
    print(f'  Bucket:     {result["bucket"]}')
    print(f'  Public URL: {result["public_url"]}\n')

    print('Step 1: Adding new URL columns if missing…')
    add_columns_if_missing()
    print()

    print('Step 2: Migrating user avatars and banners…')
    migrate_users()

    print('Step 3: Migrating post media…')
    migrate_posts()

    print('Step 4: Migrating direct message files…')
    migrate_messages()

    print('Step 5: Migrating group message files…')
    migrate_group_messages()

    conn.close()
    print('✅ Migration complete.\n')
    print('Once you have verified everything is working, you can drop')
    print('the old blob columns to reclaim database space:')
    print()
    print('  ALTER TABLE posts DROP COLUMN media_data, DROP COLUMN media_mime;')
    print('  ALTER TABLE messages DROP COLUMN file_data;')
    print('  ALTER TABLE group_messages DROP COLUMN file_data;')
