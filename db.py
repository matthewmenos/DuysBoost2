"""
db.py — "One User, One Database" SQLite + Cloudflare R2 architecture.

Every user has their own isolated SQLite database file stored in R2.
On each request:
  1.  Extract user_id from the session (or X-User-ID header for API clients).
  2.  Download  {user_id}.db  from R2_DB_BUCKET_NAME into /tmp/.
  3.  Open a WAL-mode SQLite connection.
  4.  Run init_user_tables() to ensure schema is current.
  5.  Flask blueprint code runs normally (db.execute / db.commit).
  6.  On teardown: commit, close, upload updated .db back to R2, delete /tmp file.

A shared "global" database (global.db) holds cross-user indexes:
  - users directory (username → user_id lookups, follower counts)
  - ads / tasks
  - trending / hashtag indexes
  Individual per-user DBs hold: posts, follows, notifications, messages,
  groups, channels, wallet, stories.

Design rationale
  - Zero PostgreSQL cost — SQLite runs in-process, R2 free tier is 10 GB.
  - Isolation — a corrupt user DB never affects others.
  - Scalability — each .db is typically <5 MB; R2 handles millions of objects.
  - Render free tier — /tmp is ephemeral (cleared on restart), which is fine
    because we always download fresh from R2 on each request.
"""

import os
import sqlite3
import tempfile
import logging
import threading

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from flask import g, session

logger = logging.getLogger(__name__)

# ── R2 client (singleton) ────────────────────────────────────────────────────

_r2_client      = None
_r2_client_lock = threading.Lock()


def _get_r2() -> boto3.client:
    """Return a singleton boto3 S3 client pointed at Cloudflare R2."""
    global _r2_client
    if _r2_client is not None:
        return _r2_client
    with _r2_client_lock:
        if _r2_client is None:
            _r2_client = boto3.client(
                's3',
                endpoint_url=os.environ['R2_ENDPOINT_URL'],
                aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
                aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
                config=Config(signature_version='s3v4'),
                region_name='auto',
            )
    return _r2_client


def _db_bucket() -> str:
    return os.environ.get('R2_DB_BUCKET_NAME', '')


def _tmp_path(db_key: str) -> str:
    """Return a safe /tmp path for a given R2 key."""
    safe = db_key.replace('/', '_').replace('..', '_')
    return os.path.join('/tmp', safe)


# ── Schema definition (SQLite dialect) ───────────────────────────────────────

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

-- ── Users directory (shared lookup, also in global.db) ──────────────────────
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT UNIQUE NOT NULL,
    email                TEXT UNIQUE NOT NULL,
    password             TEXT,
    display_name         TEXT,
    bio                  TEXT,
    avatar_url           TEXT,
    banner_url           TEXT,
    website              TEXT,
    location             TEXT,
    is_verified          INTEGER DEFAULT 0,
    is_admin             INTEGER DEFAULT 0,
    balance              REAL    DEFAULT 0,
    follower_count       INTEGER DEFAULT 0,
    following_count      INTEGER DEFAULT 0,
    post_count           INTEGER DEFAULT 0,
    subscriber_count     INTEGER DEFAULT 0,
    total_tips_received  REAL    DEFAULT 0,
    total_tips_sent      REAL    DEFAULT 0,
    unread_dm_count      INTEGER DEFAULT 0,
    unread_group_count   INTEGER DEFAULT 0,
    search_count         INTEGER DEFAULT 0,
    referral_code        TEXT UNIQUE,
    referred_by          INTEGER,
    referral_bonus_awarded INTEGER DEFAULT 0,
    theme                TEXT    DEFAULT 'dark',
    crypto_network       TEXT,
    crypto_address       TEXT,
    crypto_name          TEXT,
    online_at            TEXT,
    show_online          INTEGER DEFAULT 1,
    allow_post_saves     INTEGER DEFAULT 1,
    created_at           TEXT    DEFAULT (datetime('now'))
);

-- ── Posts ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS posts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    body             TEXT,
    reply_to_id      INTEGER,
    repost_of_id     INTEGER,
    quote_body       TEXT,
    media_url        TEXT,
    media_mime       TEXT,
    like_count       INTEGER DEFAULT 0,
    reply_count      INTEGER DEFAULT 0,
    repost_count     INTEGER DEFAULT 0,
    view_count       INTEGER DEFAULT 0,
    score            REAL    DEFAULT 0,
    is_boosted       INTEGER DEFAULT 0,
    is_subscriber_only INTEGER DEFAULT 0,
    hashtags_cached  TEXT,
    post_type        TEXT    DEFAULT 'post',
    poll_expires_at  TEXT,
    edited_at        TEXT,
    created_at       TEXT    DEFAULT (datetime('now'))
);

-- ── Follows ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS follows (
    follower_id  INTEGER NOT NULL,
    following_id INTEGER NOT NULL,
    created_at   TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (follower_id, following_id)
);

-- ── Post likes ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS post_likes (
    user_id    INTEGER NOT NULL,
    post_id    INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, post_id)
);

-- ── Bookmarks ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bookmarks (
    user_id    INTEGER NOT NULL,
    post_id    INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, post_id)
);

-- ── Post views ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS post_views (
    post_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (post_id, user_id)
);

-- ── Hashtags ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hashtags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS post_hashtags (
    post_id    INTEGER NOT NULL,
    hashtag_id INTEGER NOT NULL,
    PRIMARY KEY (post_id, hashtag_id)
);

-- ── Poll options & votes ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS poll_options (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    label   TEXT    NOT NULL,
    votes   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS poll_votes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id   INTEGER NOT NULL,
    option_id INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    voted_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (post_id, user_id)
);

-- ── Post boosts ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS post_boosts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id           INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    budget            REAL    NOT NULL,
    budget_spent      REAL    DEFAULT 0,
    reward_per_engage REAL    DEFAULT 0.05,
    engage_type       TEXT    DEFAULT 'like',
    target_count      INTEGER DEFAULT 0,
    engaged_count     INTEGER DEFAULT 0,
    status            TEXT    DEFAULT 'active',
    created_at        TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS boost_engagements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    boost_id   INTEGER NOT NULL,
    post_id    INTEGER NOT NULL,
    worker_id  INTEGER NOT NULL,
    reward     REAL,
    earned_at  TEXT DEFAULT (datetime('now'))
);

-- ── Ads & tasks ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    title            TEXT    NOT NULL,
    platform         TEXT    NOT NULL,
    target_url       TEXT    NOT NULL,
    task_type        TEXT    NOT NULL,
    reward_per_task  REAL    DEFAULT 0.05,
    budget           REAL    NOT NULL,
    budget_spent     REAL    DEFAULT 0,
    followers_target INTEGER DEFAULT 0,
    followers_gained INTEGER DEFAULT 0,
    status           TEXT    DEFAULT 'active',
    created_at       TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS task_completions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id        INTEGER NOT NULL,
    worker_id    INTEGER NOT NULL,
    proof_link   TEXT    NOT NULL,
    status       TEXT    DEFAULT 'approved',
    reward       REAL,
    submitted_at TEXT    DEFAULT (datetime('now')),
    reviewed_at  TEXT
);

-- ── Wallet ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    type        TEXT,
    amount      REAL,
    description TEXT,
    status      TEXT DEFAULT 'completed',
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS withdrawals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    amount         REAL,
    method         TEXT,
    account        TEXT,
    network        TEXT,
    status         TEXT DEFAULT 'pending',
    tx_hash        TEXT,
    failure_reason TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    processed_at   TEXT
);
CREATE TABLE IF NOT EXISTS crypto_deposits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    network      TEXT    NOT NULL,
    tx_hash      TEXT UNIQUE NOT NULL,
    amount       REAL    NOT NULL,
    status       TEXT    DEFAULT 'pending',
    confirmed_at TEXT,
    created_at   TEXT    DEFAULT (datetime('now'))
);

-- ── Notifications ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    message    TEXT,
    read       INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

-- ── Direct messages ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a      INTEGER NOT NULL,
    user_b      INTEGER NOT NULL,
    last_msg_at TEXT    DEFAULT (datetime('now')),
    UNIQUE (user_a, user_b)
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    sender_id       INTEGER NOT NULL,
    body            TEXT,
    msg_type        TEXT    DEFAULT 'text',
    file_url        TEXT,
    file_name       TEXT,
    file_mime       TEXT,
    is_read         INTEGER DEFAULT 0,
    edited_at       TEXT,
    reply_to_id     INTEGER,
    reactions       TEXT,
    is_pinned       INTEGER DEFAULT 0,
    deleted_at      TEXT,
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- ── Channels ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS channels (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    slug         TEXT NOT NULL UNIQUE,
    description  TEXT,
    avatar_url   TEXT,
    owner_id     INTEGER NOT NULL,
    is_public    INTEGER DEFAULT 1,
    member_count INTEGER DEFAULT 0,
    post_count   INTEGER DEFAULT 0,
    created_at   TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS channel_members (
    channel_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    role       TEXT    DEFAULT 'member',
    joined_at  TEXT    DEFAULT (datetime('now')),
    PRIMARY KEY (channel_id, user_id)
);
CREATE TABLE IF NOT EXISTS channel_posts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    post_id    INTEGER NOT NULL,
    created_at TEXT    DEFAULT (datetime('now')),
    UNIQUE (channel_id, post_id)
);

-- ── Groups ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS groups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    slug         TEXT NOT NULL UNIQUE,
    description  TEXT,
    avatar_url   TEXT,
    owner_id     INTEGER NOT NULL,
    is_public    INTEGER DEFAULT 1,
    member_count INTEGER DEFAULT 0,
    created_at   TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS group_members (
    group_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    role         TEXT    DEFAULT 'member',
    joined_at    TEXT    DEFAULT (datetime('now')),
    last_read_at TEXT,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS group_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL,
    sender_id   INTEGER NOT NULL,
    body        TEXT,
    msg_type    TEXT DEFAULT 'text',
    file_url    TEXT,
    file_name   TEXT,
    file_mime   TEXT,
    reply_to_id INTEGER,
    deleted_at  TEXT,
    edited_at   TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- ── Creator monetisation ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subscription_tiers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id  INTEGER NOT NULL UNIQUE,
    price_usd   REAL    NOT NULL DEFAULT 1.0,
    title       TEXT    NOT NULL DEFAULT 'Supporter',
    description TEXT,
    perks       TEXT,
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL,
    creator_id    INTEGER NOT NULL,
    tier_id       INTEGER NOT NULL,
    status        TEXT    DEFAULT 'active',
    started_at    TEXT    DEFAULT (datetime('now')),
    expires_at    TEXT,
    UNIQUE (subscriber_id, creator_id)
);
CREATE TABLE IF NOT EXISTS tips (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER NOT NULL,
    to_user_id   INTEGER NOT NULL,
    post_id      INTEGER,
    amount       REAL    NOT NULL,
    message      TEXT,
    created_at   TEXT    DEFAULT (datetime('now'))
);

-- ── Stories ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    media_url  TEXT    NOT NULL,
    media_mime TEXT    NOT NULL DEFAULT 'image/jpeg',
    caption    TEXT,
    viewed_by  TEXT    DEFAULT '[]',
    expires_at TEXT    NOT NULL DEFAULT (datetime('now', '+1 day')),
    created_at TEXT    DEFAULT (datetime('now'))
);

-- ── Search history ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS search_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    query       TEXT    NOT NULL,
    result_type TEXT    DEFAULT 'mixed',
    created_at  TEXT    DEFAULT (datetime('now'))
);

-- ── Indexes (hot-path queries) ────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_posts_user       ON posts(user_id);
CREATE INDEX IF NOT EXISTS idx_posts_created    ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_reply      ON posts(reply_to_id);
CREATE INDEX IF NOT EXISTS idx_posts_score      ON posts(score);
CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows(follower_id);
CREATE INDEX IF NOT EXISTS idx_follows_following ON follows(following_id);
CREATE INDEX IF NOT EXISTS idx_likes_post       ON post_likes(post_id);
CREATE INDEX IF NOT EXISTS idx_likes_user       ON post_likes(user_id);
CREATE INDEX IF NOT EXISTS idx_bm_user          ON bookmarks(user_id);
CREATE INDEX IF NOT EXISTS idx_notif_user       ON notifications(user_id, read);
CREATE INDEX IF NOT EXISTS idx_tx_user          ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_wdr_status       ON withdrawals(status);
CREATE INDEX IF NOT EXISTS idx_ads_user         ON ads(user_id);
CREATE INDEX IF NOT EXISTS idx_ads_status       ON ads(status);
CREATE INDEX IF NOT EXISTS idx_tc_worker        ON task_completions(worker_id);
CREATE INDEX IF NOT EXISTS idx_tc_ad            ON task_completions(ad_id);
CREATE INDEX IF NOT EXISTS idx_pb_post          ON post_boosts(post_id);
CREATE INDEX IF NOT EXISTS idx_pb_status        ON post_boosts(status);
CREATE INDEX IF NOT EXISTS idx_be_boost         ON boost_engagements(boost_id);
CREATE INDEX IF NOT EXISTS idx_be_worker        ON boost_engagements(worker_id);
CREATE INDEX IF NOT EXISTS idx_ph_post          ON post_hashtags(post_id);
CREATE INDEX IF NOT EXISTS idx_ph_hashtag       ON post_hashtags(hashtag_id);
CREATE INDEX IF NOT EXISTS idx_tips_to          ON tips(to_user_id);
CREATE INDEX IF NOT EXISTS idx_tips_from        ON tips(from_user_id);
CREATE INDEX IF NOT EXISTS idx_sub_creator      ON subscriptions(creator_id);
CREATE INDEX IF NOT EXISTS idx_sub_subscriber   ON subscriptions(subscriber_id);
CREATE INDEX IF NOT EXISTS idx_conv_a           ON conversations(user_a);
CREATE INDEX IF NOT EXISTS idx_conv_b           ON conversations(user_b);
CREATE INDEX IF NOT EXISTS idx_conv_last        ON conversations(last_msg_at);
CREATE INDEX IF NOT EXISTS idx_msg_conv         ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_msg_sender       ON messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_ch_owner         ON channels(owner_id);
CREATE INDEX IF NOT EXISTS idx_chm_user         ON channel_members(user_id);
CREATE INDEX IF NOT EXISTS idx_grp_owner        ON groups(owner_id);
CREATE INDEX IF NOT EXISTS idx_grpm_user        ON group_members(user_id);
CREATE INDEX IF NOT EXISTS idx_grpms_group      ON group_messages(group_id);
CREATE INDEX IF NOT EXISTS idx_stories_user     ON stories(user_id);
CREATE INDEX IF NOT EXISTS idx_stories_expires  ON stories(expires_at);
CREATE INDEX IF NOT EXISTS idx_sh_user          ON search_history(user_id);
CREATE INDEX IF NOT EXISTS idx_pv_post          ON post_views(post_id);
"""


def init_user_tables(conn: sqlite3.Connection) -> None:
    """
    Ensure every table and index exists in a user's DB.
    Safe to call on every connection open — all statements use IF NOT EXISTS.
    """
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# ── Per-user DB lifecycle ─────────────────────────────────────────────────────

def _db_key(user_id: int) -> str:
    return f'users/{user_id}.db'


def _download_user_db(user_id: int) -> str:
    """
    Download user's .db from R2 to /tmp.
    If it doesn't exist yet (new user), create an empty file.
    Returns the local /tmp path.
    """
    r2     = _get_r2()
    bucket = _db_bucket()
    key    = _db_key(user_id)
    path   = _tmp_path(key)

    os.makedirs(os.path.dirname(path), exist_ok=True)

    try:
        r2.download_file(bucket, key, path)
        logger.debug('DB downloaded: %s → %s', key, path)
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code in ('404', 'NoSuchKey'):
            logger.debug('New user DB: %s', key)
            # Create empty file — init_user_tables will build the schema
            open(path, 'wb').close()
        else:
            logger.error('R2 download error for %s: %s', key, e)
            raise

    return path


def _upload_user_db(user_id: int, path: str) -> None:
    """Upload user's .db back to R2 and delete the local /tmp file."""
    r2     = _get_r2()
    bucket = _db_bucket()
    key    = _db_key(user_id)
    try:
        r2.upload_file(
            path, bucket, key,
            ExtraArgs={'ContentType': 'application/octet-stream'},
        )
        logger.debug('DB uploaded: %s → %s', path, key)
    except Exception as e:
        logger.error('R2 upload error for %s: %s', key, e)
        # Don't raise — we still want to clean up the local file
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def open_user_db(user_id: int) -> tuple[sqlite3.Connection, str]:
    """
    Download, open, and schema-init a user's database.
    Returns (connection, tmp_path).
    The caller must call close_and_sync(conn, user_id, path) when done.
    """
    path = _download_user_db(user_id)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous  = NORMAL')
    init_user_tables(conn)
    return conn, path


def close_and_sync(conn: sqlite3.Connection, user_id: int, path: str) -> None:
    """Commit, close, upload to R2, and remove local file."""
    try:
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning('close_and_sync commit/close error: %s', e)
    _upload_user_db(user_id, path)


# ── Flask g integration ───────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """
    Return the per-request SQLite connection for the current user.
    Downloads from R2 on first call; uploaded back on teardown.

    Falls back to a shared in-memory DB if no user is logged in
    (e.g., for anonymous GET requests like the index page).
    """
    if 'db' in g:
        return g.db

    from flask import session as _session
    uid = _session.get('user_id')

    if uid:
        conn, path = open_user_db(uid)
        g.db       = conn
        g.db_uid   = uid
        g.db_path  = path
    else:
        # Anonymous request — use a shared global DB (read-only public data)
        from flask import current_app
        global_path = os.path.join(current_app.root_path, 'global.db')
        conn = sqlite3.connect(global_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous  = NORMAL')
        init_user_tables(conn)
        g.db      = conn
        g.db_uid  = None
        g.db_path = None

    return g.db


def close_db(_e=None) -> None:
    """
    Flask teardown hook.
    Commit → close → upload to R2 → delete /tmp file.
    """
    conn = g.pop('db', None)
    uid  = g.pop('db_uid', None)
    path = g.pop('db_path', None)

    if conn is None:
        return

    if uid and path:
        # Authenticated user — sync back to R2
        close_and_sync(conn, uid, path)
    else:
        # Anonymous or global DB — just close
        try:
            conn.commit()
            conn.close()
        except Exception:
            pass


def init_app(app) -> None:
    """Register teardown hook with the Flask app."""
    app.teardown_appcontext(close_db)
