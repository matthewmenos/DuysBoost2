"""
blueprints/stories.py — Instagram-style stories.

• Stories expire automatically after 24 hours (enforced by expires_at column).
• A background thread runs every 10 minutes to hard-delete expired rows + R2 files.
• Users can manually delete their own stories via DELETE /api/story/<id>.
• Viewing a story marks the viewer's user_id in the viewed_by JSON array.
• Stories appear as avatar rings on the feed and profile pages.

Endpoints:
  POST   /api/story/create        — upload a new story (image or video)
  GET    /api/stories/feed        — list active stories from followed users
  GET    /api/stories/user/<uid>  — list stories for a specific user
  POST   /api/story/<id>/view     — mark story as viewed by current user
  DELETE /api/story/<id>          — delete own story (also deletes from R2)
  GET    /api/story/<id>          — single story data (for viewer overlay)
"""

import json
import threading
import time
import logging
from datetime import datetime, timezone

from flask import (
    Blueprint, jsonify, request, session,
    current_app
)

from helpers import get_db, login_required, safe_int
from security import limiter, csrf_exempt

logger = logging.getLogger(__name__)
bp = Blueprint('stories', __name__)

# ── Background cleanup thread ─────────────────────────────────────────────────

_cleanup_started = False
_cleanup_lock    = threading.Lock()


def _run_cleanup(app):
    """Delete expired stories every 10 minutes."""
    import sqlite3
    
    import os

    while True:
        time.sleep(600)  # 10 minutes
        try:
            dsn = (
                os.environ.get('DATABASE_URL') or
                os.environ.get('POSTGRES_URL', '')
            )
            if not dsn:
                continue
            if dsn.startswith('postgres://'):
                dsn = 'postgresql://' + dsn[len('postgres://'):]

            conn = sqlite3.connect(dsn)
            cur  = conn.cursor()

            cur.execute(
                "SELECT id, media_url FROM stories WHERE expires_at < datetime('now')"
            )
            expired = cur.fetchall()

            if expired:
                import storage as _st
                for row in expired:
                    try:
                        _st.delete_object(row['media_url'])
                    except Exception:
                        pass

                ids = [r['id'] for r in expired]
                cur.execute(
                    'DELETE FROM stories WHERE id = ANY(?)', (ids,)
                )
                conn.commit()
                logger.info('Stories cleanup: deleted %d expired stories', len(expired))

            cur.close()
            conn.close()
        except Exception as e:
            logger.warning('Stories cleanup error: ?', e)


def start_cleanup_thread(app):
    """Start background cleanup thread (idempotent)."""
    global _cleanup_started
    with _cleanup_lock:
        if not _cleanup_started:
            t = threading.Thread(target=_run_cleanup, args=(app,), daemon=True)
            t.start()
            _cleanup_started = True


# ── Helper ────────────────────────────────────────────────────────────────────

def _format_story(row, viewer_uid):
    """Convert a DB row to a dict with viewer-specific metadata."""
    d = dict(row)
    # Convert timestamps to ISO strings for JSON
    for col in ('created_at', 'expires_at'):
        if col in d and hasattr(d[col], 'isoformat'):
            d[col] = d[col].isoformat()
    # Parse viewed_by JSON
    try:
        viewed_list = json.loads(d.get('viewed_by') or '[]')
    except Exception:
        viewed_list = []
    d['viewed']      = viewer_uid in viewed_list
    d['view_count']  = len(viewed_list)
    d['is_own']      = d['user_id'] == viewer_uid
    # Time remaining
    try:
        exp = datetime.fromisoformat(d['expires_at'].replace('Z', ''))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        remaining = max(0, (exp - datetime.now(timezone.utc)).total_seconds())
        d['hours_left'] = round(remaining / 3600, 1)
    except Exception:
        d['hours_left'] = 24.0
    return d


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.route('/api/story/create', methods=['POST'])
@login_required
@csrf_exempt
@limiter.limit('20 per hour')
def create_story():
    """
    Upload a story (image or video).
    Accepts a base64 data-URI in the 'media_data' form field.
    """
    import storage as _st
    db      = get_db()
    uid     = session['user_id']
    raw     = (request.form.get('media_data') or '').strip()
    caption = (request.form.get('caption') or '').strip()[:200]

    if not raw or not raw.startswith('data:'):
        return jsonify({'success': False, 'error': 'No media provided.'}), 400

    mime = raw.split(';')[0][5:]   # strip 'data:'
    allowed = _st.ALLOWED_IMAGE_MIMES | {'video/mp4', 'video/webm', 'video/ogg'}
    if mime not in allowed:
        return jsonify({'success': False,
                        'error': 'Unsupported file type. Use JPEG, PNG, WebP, GIF, MP4 or WebM.'}), 400

    try:
        media_url = _st.upload_data_uri(raw, f'stories/{uid}')
    except (ValueError, RuntimeError) as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    db.execute(
        'INSERT INTO stories (user_id, media_url, media_mime, caption) VALUES (?, ?, ?, ?)',
        (uid, media_url, mime, caption or None)
    )
    story_id = db.lastrowid
    db.commit()
    row = db.execute(
        'SELECT id, media_url, media_mime, expires_at, created_at FROM stories WHERE id=?', (story_id,)
    ).fetchone()
    db.commit()

    return jsonify({
        'success':    True,
        'story_id':   story_id,
        'media_url':  media_url,
        'media_mime': mime,
    })


@bp.route('/api/stories/feed')
@login_required
@csrf_exempt
def stories_feed():
    """
    Returns active stories from:
      1. Users the current user follows
      2. The current user themselves
    Grouped by user, sorted: unseen first, then by latest story.
    """
    db  = get_db()
    uid = session['user_id']

    rows = db.execute("""
        SELECT s.*,
               u.id       AS author_id,
               u.username AS author_username,
               u.display_name AS author_display,
               u.avatar_url   AS author_avatar,
               u.is_verified  AS author_verified
        FROM stories s
        JOIN users u ON u.id = s.user_id
        WHERE s.expires_at > datetime('now')
          AND (
              s.user_id = ?
              OR s.user_id IN (SELECT following_id FROM follows WHERE follower_id = ?)
          )
        ORDER BY s.user_id, s.created_at ASC
    """, (uid, uid)).fetchall()

    # Group by user
    users_map = {}
    for row in rows:
        story  = _format_story(row, uid)
        author = row['author_id']
        if author not in users_map:
            users_map[author] = {
                'user_id':        author,
                'username':       row['author_username'],
                'display_name':   row['author_display'],
                'avatar_url':     row['author_avatar'],
                'is_verified':    row['author_verified'],
                'is_own':         author == uid,
                'stories':        [],
                'has_unseen':     False,
            }
        users_map[author]['stories'].append(story)
        if not story['viewed']:
            users_map[author]['has_unseen'] = True

    # Sort: own first, then unseen, then by latest story
    groups = sorted(
        users_map.values(),
        key=lambda g: (
            0 if g['is_own'] else 1,
            0 if g['has_unseen'] else 1,
        )
    )
    return jsonify({'groups': groups})


@bp.route('/api/stories/user/<int:user_id>')
@login_required
@csrf_exempt
def user_stories(user_id):
    """All active stories for a specific user."""
    db  = get_db()
    uid = session['user_id']
    rows = db.execute("""
        SELECT s.*, u.username, u.display_name, u.avatar_url
        FROM stories s JOIN users u ON u.id = s.user_id
        WHERE s.user_id = ? AND s.expires_at > datetime('now')
        ORDER BY s.created_at ASC
    """, (user_id,)).fetchall()
    return jsonify({'stories': [_format_story(r, uid) for r in rows]})


@bp.route('/api/story/<int:story_id>')
@login_required
@csrf_exempt
def get_story(story_id):
    """Fetch a single story."""
    db  = get_db()
    uid = session['user_id']
    row = db.execute("""
        SELECT s.*, u.username, u.display_name, u.avatar_url
        FROM stories s JOIN users u ON u.id = s.user_id
        WHERE s.id = ? AND s.expires_at > datetime('now')
    """, (story_id,)).fetchone()
    if not row:
        return jsonify({'success': False, 'error': 'Story not found or expired.'}), 404
    return jsonify({'success': True, 'story': _format_story(row, uid)})


@bp.route('/api/story/<int:story_id>/view', methods=['POST'])
@login_required
@csrf_exempt
@limiter.limit('200 per hour')
def view_story(story_id):
    """Mark the current user as having viewed this story."""
    db  = get_db()
    uid = session['user_id']
    row = db.execute(
        "SELECT id, viewed_by, user_id FROM stories WHERE id = ? AND expires_at > datetime('now')",
        (story_id,)
    ).fetchone()
    if not row:
        return jsonify({'success': False}), 404

    try:
        viewed = json.loads(row['viewed_by'] or '[]')
    except Exception:
        viewed = []

    if uid not in viewed:
        viewed.append(uid)
        db.execute('UPDATE stories SET viewed_by = ? WHERE id = ?',
                   (json.dumps(viewed), story_id))
        db.commit()

    return jsonify({'success': True, 'view_count': len(viewed)})



@bp.route('/api/story/<int:story_id>/viewers')
@login_required
@csrf_exempt
def story_viewers(story_id):
    """
    Return the list of users who viewed this story.
    Only accessible by the story owner.
    Also returns reaction counts.
    """
    db  = get_db()
    uid = session['user_id']
    row = db.execute(
        'SELECT id, user_id, viewed_by FROM stories WHERE id = ?', (story_id,)
    ).fetchone()
    if not row:
        return jsonify({'success': False, 'error': 'Story not found.'}), 404
    if row['user_id'] != uid:
        return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    try:
        viewed_list = json.loads(row['viewed_by'] or '[]')
    except Exception:
        viewed_list = []

    # Exclude story owner from viewer list/count
    story_owner = row['user_id']
    viewed_list = [v for v in viewed_list if v != story_owner]

    # Fetch user details for each viewer
    viewers = []
    for viewer_uid in viewed_list:
        u = db.execute(
            'SELECT id, username, display_name, avatar_url FROM users WHERE id=?',
            (viewer_uid,)
        ).fetchone()
        if u:
            viewers.append({
                'id':           u['id'],
                'username':     u['username'],
                'display_name': u['display_name'],
                'avatar_url':   u['avatar_url'],
            })

    return jsonify({
        'success':     True,
        'view_count':  len(viewed_list),
        'viewers':     viewers,
    })



@bp.route('/api/story/<int:story_id>/react', methods=['POST'])
@login_required
@csrf_exempt
def story_react(story_id):
    """Add or update a reaction to a story."""
    db  = get_db()
    uid = session['user_id']
    row = db.execute(
        "SELECT id, user_id, viewed_by FROM stories WHERE id=? AND expires_at > datetime('now')",
        (story_id,)
    ).fetchone()
    if not row:
        return jsonify({'success': False, 'error': 'Story not found.'}), 404
    if row['user_id'] == uid:
        return jsonify({'success': False, 'error': 'Cannot react to your own story.'}), 400

    data  = request.get_json(silent=True) or {}
    emoji = (data.get('emoji') or '').strip()
    ALLOWED = {'❤️','😂','😮','😢','😡','🔥','👏','💯','🎉','😍'}
    if emoji not in ALLOWED:
        return jsonify({'success': False, 'error': 'Invalid reaction.'}), 400

    # Store reactions as dict {user_id: emoji} in a separate column
    # We add reactions_data column via migration
    try:
        react_row = db.execute(
            'SELECT reactions_data FROM stories WHERE id=?', (story_id,)
        ).fetchone()
        reactions = json.loads(react_row['reactions_data'] or '{}') if react_row else {}
    except Exception:
        reactions = {}

    reactions[str(uid)] = emoji
    db.execute('UPDATE stories SET reactions_data=? WHERE id=?',
               (json.dumps(reactions), story_id))
    db.commit()
    return jsonify({'success': True, 'emoji': emoji})


@bp.route('/api/story/<int:story_id>', methods=['DELETE'])
@login_required
@csrf_exempt
def delete_story(story_id):
    """Delete own story — removes from DB and R2."""
    import storage as _st
    db  = get_db()
    uid = session['user_id']
    row = db.execute(
        'SELECT id, user_id, media_url, media_mime FROM stories WHERE id = ?', (story_id,)
    ).fetchone()
    if not row:
        return jsonify({'success': False, 'error': 'Story not found.'}), 404
    if row['user_id'] != uid:
        # Admins can also delete
        me = db.execute('SELECT is_admin FROM users WHERE id = ?', (uid,)).fetchone()
        if not me or not me['is_admin']:
            return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    # Delete file from R2
    try:
        _st.delete_object(row['media_url'])
    except Exception as e:
        logger.warning('Story R2 delete failed (continuing): ?', e)

    # Hard-delete from DB
    db.execute('DELETE FROM stories WHERE id = ?', (story_id,))
    db.commit()

    return jsonify({'success': True})
