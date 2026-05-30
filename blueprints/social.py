"""blueprints/social.py — feed, posts, profiles, explore, channels, groups, DMs."""
import re
import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
from flask import (
    Blueprint, jsonify, redirect, render_template,
    request, session, url_for
)
from helpers import (
    get_db, get_user_db, login_required, safe_float, safe_int,
    add_notification, update_counts, recalc_post_score,
    format_post, format_post_with_poll,
    get_personalized_post_ids,
)
import storage
from security import (
    limiter, csrf_exempt,
    LIMIT_POST, LIMIT_FOLLOW, LIMIT_LIKE, LIMIT_DM,
    LIMIT_UPLOAD, LIMIT_POLL, LIMIT_HEARTBEAT
)

bp = Blueprint('social', __name__)

# In-memory typing state: {(user_id, recipient_username): timestamp}
# Entries expire after _TYPING_TTL seconds; pruned on every write to prevent unbounded growth.
_typing_state: dict = {}
_TYPING_TTL = 30  # seconds


# ── Feed ─────────────────────────────────────────────────────────────────────

@bp.route('/feed')
@login_required
@limiter.limit(LIMIT_POLL)
def feed():
    db   = get_db()
    uid  = session['user_id']
    tab  = request.args.get('tab', 'for_you')
    page = safe_int(request.args.get('page'), 1)
    per  = 20
    off  = (page - 1) * per

    if tab == 'following':
        rows = db.execute("""
            SELECT p.* FROM posts p
            WHERE p.reply_to_id IS NULL
              AND p.user_id IN (SELECT following_id FROM follows WHERE follower_id=?)
              AND p.id NOT IN (SELECT post_id FROM channel_posts)
            ORDER BY p.created_at DESC LIMIT ? OFFSET ?
        """, (uid, per, off)).fetchall()
    elif tab == 'earn':
        rows = db.execute("""
            SELECT DISTINCT p.* FROM posts p
            JOIN post_boosts pb ON pb.post_id = p.id
            WHERE pb.status='active'
              AND pb.budget_spent < pb.budget
              AND pb.user_id != ?
              AND NOT EXISTS (
                SELECT 1 FROM boost_engagements be
                WHERE be.boost_id=pb.id AND be.worker_id=?
              )
            ORDER BY pb.reward_per_engage DESC, p.created_at DESC LIMIT ? OFFSET ?
        """, (uid, uid, per, off)).fetchall()
    else:
        ranked_ids = get_personalized_post_ids(db, uid, limit=per, offset=off)
        if ranked_ids:
            ph   = ','.join(['?'] * len(ranked_ids))
            rows = db.execute(f'SELECT * FROM posts WHERE id IN ({ph})', ranked_ids).fetchall()
            row_map = {r['id']: r for r in rows}
            rows    = [row_map[pid] for pid in ranked_ids if pid in row_map]
        else:
            rows = db.execute("""
                SELECT * FROM posts
                WHERE reply_to_id IS NULL
                  AND id NOT IN (SELECT post_id FROM channel_posts)
                ORDER BY score DESC, created_at DESC LIMIT ? OFFSET ?
            """, (per, off)).fetchall()

    posts    = [format_post_with_poll(r, uid, db) for r in rows]
    has_more = len(rows) == per

    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify({'posts': posts, 'has_more': has_more})

    suggestions = [dict(s) for s in db.execute("""
        SELECT * FROM users
        WHERE id != ?
          AND id NOT IN (SELECT following_id FROM follows WHERE follower_id=?)
        ORDER BY follower_count DESC, id DESC LIMIT 5
    """, (uid, uid)).fetchall()]

    trending = [dict(t) for t in db.execute("""
        SELECT p.*, u.username, u.display_name, u.avatar_url, u.is_verified
        FROM posts p JOIN users u ON p.user_id=u.id
        WHERE p.reply_to_id IS NULL
          AND p.created_at >= datetime('now', '-48 hours')
        ORDER BY p.like_count DESC LIMIT 5
    """).fetchall()]

    return render_template('feed.html', posts=posts, tab=tab,
                           page=page, has_more=has_more,
                           suggestions=suggestions, trending=trending)



# ── Media / cascade helpers ───────────────────────────────────────────────────

def _delete_post_media(post_row):
    """
    Best-effort R2 deletion for a post's media_url.
    Silently skips if url is None or storage module unavailable.
    """
    url = None
    try:
        url = post_row['media_url'] if post_row else None
    except Exception:
        return
    if not url:
        return
    try:
        import storage as _st
        _st.delete_object(url)
    except Exception as _e:
        import logging as _log
        _log.getLogger(__name__).warning('Post R2 media delete failed for %s: %s', url, _e)


def _full_delete_post(db, post_id, *, notify_owner=False):
    """
    Cascade-delete a single post from all tables AND its R2 media.
    Call BEFORE the final commit — does not commit itself.
    """
    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return

    # 1. Delete R2 media first (while we still have the URL)
    _delete_post_media(post)

    # 2. Update parent counters
    if post['reply_to_id']:
        db.execute('UPDATE posts SET reply_count=MAX(0,reply_count-1) WHERE id=?',
                   (post['reply_to_id'],))
    if post['repost_of_id']:
        db.execute('UPDATE posts SET repost_count=MAX(0,repost_count-1) WHERE id=?',
                   (post['repost_of_id'],))

    # 3. Delete child rows in dependency order
    for tbl in ('post_likes', 'bookmarks', 'poll_votes', 'poll_options',
                'post_hashtags', 'channel_posts', 'post_views',
                'boost_engagements', 'post_boosts'):
        try:
            db.execute(f'DELETE FROM {tbl} WHERE post_id=?', (post_id,))
        except Exception:
            pass

    # 4. Delete replies recursively (shallow depth — replies of replies)
    reply_ids = [r['id'] for r in
                 db.execute('SELECT id FROM posts WHERE reply_to_id=?', (post_id,)).fetchall()]
    for rid in reply_ids:
        _delete_post_media(db.execute('SELECT * FROM posts WHERE id=?', (rid,)).fetchone())
        for tbl in ('post_likes', 'bookmarks', 'poll_votes', 'poll_options',
                    'post_hashtags', 'post_views'):
            try:
                db.execute(f'DELETE FROM {tbl} WHERE post_id=?', (rid,))
            except Exception:
                pass
        db.execute('DELETE FROM posts WHERE id=?', (rid,))

    # 5. Delete the post itself
    db.execute('DELETE FROM posts WHERE id=?', (post_id,))

    # 6. Update author post_count
    db.execute('UPDATE users SET post_count=MAX(0,post_count-1) WHERE id=?',
               (post['user_id'],))


# ── Post CRUD ─────────────────────────────────────────────────────────────────

@bp.route('/post', methods=['POST'])
@login_required
@limiter.limit(LIMIT_POST)
@csrf_exempt   # JSON/multipart — SameSite=Lax protects
def create_post():
    db  = get_db()
    uid = session['user_id']

    body            = (request.form.get('body') or '').strip()
    reply_to        = safe_int(request.form.get('reply_to_id'), 0) or None
    repost_of       = safe_int(request.form.get('repost_of_id'), 0) or None
    quote_body      = (request.form.get('quote_body') or '').strip() or None
    subscriber_only = 1 if request.form.get('subscriber_only') else 0
    _raw_media_data = (request.form.get('media_data') or '').strip() or None
    media_mime      = (request.form.get('media_mime') or '').strip() or None
    # Upload media to Cloudflare R2 if present; store URL instead of blob
    media_url  = None
    if _raw_media_data:
        try:
            media_url = storage.upload_post_media(uid, _raw_media_data)
        except ValueError as _e:
            # Bad data URI / unsupported MIME / file too large — reject clearly
            return jsonify({'success': False, 'error': str(_e)}), 400
        except Exception as _e:
            # Any other error (R2 credentials, network, etc.):
            # Log it and continue posting WITHOUT media rather than losing the post.
            # The user already hit "Post" — a silent media failure is better than
            # a 500 that causes them to retry and potentially duplicate the post.
            logger.error('Media upload failed (posting without media): %s', _e)
            media_url = None  # post goes through, attachment is silently dropped
        # Infer mime from data URI if not supplied by client
        if not media_mime and _raw_media_data.startswith('data:'):
            media_mime = _raw_media_data.split(';')[0][5:] or None
    post_type       = (request.form.get('post_type') or 'post').strip().lower()
    channel_id      = safe_int(request.form.get('channel_id'), 0) or None

    poll_options_raw = request.form.get('poll_options') or '[]'
    try:
        poll_options = [str(o).strip() for o in json.loads(poll_options_raw) if str(o).strip()][:10]
    except Exception:
        poll_options = []
    if post_type == 'poll' and len(poll_options) < 2:
        return jsonify({'success': False, 'error': 'A poll needs at least 2 options.'}), 400
    poll_expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat() \
                      if post_type == 'poll' else None

    if not body and not repost_of and not media_url and post_type != 'poll':
        return jsonify({'success': False, 'error': 'Post cannot be empty.'}), 400
    if body and len(body) > 500:
        return jsonify({'success': False, 'error': 'Max 500 characters.'}), 400

    is_sensitive = 1 if request.form.get('is_sensitive') == '1' else 0

    scheduled_at_raw = (request.form.get('scheduled_at') or '').strip() or None
    scheduled_at = None
    post_status  = 'published'
    if scheduled_at_raw:
        try:
            _sched = datetime.fromisoformat(scheduled_at_raw)
            if _sched.tzinfo is None:
                _sched = _sched.replace(tzinfo=timezone.utc)
            if _sched > datetime.now(timezone.utc):
                scheduled_at = _sched.isoformat()
                post_status  = 'scheduled'
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid scheduled time.'}), 400

    now = datetime.now(timezone.utc).isoformat()
    db.execute("""
        INSERT INTO posts (user_id, body, reply_to_id, repost_of_id, quote_body,
                           is_subscriber_only, is_sensitive, media_url, media_mime,
                           post_type, poll_expires_at, scheduled_at, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (uid, body or None, reply_to, repost_of, quote_body,
          subscriber_only, is_sensitive, media_url, media_mime if media_url else None,
          post_type, poll_expires_at, scheduled_at, post_status, now))
    post_id = db.lastrowid

    for opt_label in poll_options:
        db.execute('INSERT INTO poll_options (post_id, label) VALUES (?,?)', (post_id, opt_label))

    if channel_id:
        ch = db.execute('SELECT id FROM channels WHERE id=?', (channel_id,)).fetchone()
        if ch:
            db.execute('INSERT INTO channel_posts (channel_id, post_id) VALUES (?,?) ',
                       (channel_id, post_id))
            db.execute('UPDATE channels SET post_count=post_count+1 WHERE id=?', (channel_id,))

    try:
        if reply_to:
            db.execute('UPDATE posts SET reply_count=reply_count+1 WHERE id=?', (reply_to,))
            parent = db.execute('SELECT user_id FROM posts WHERE id=?', (reply_to,)).fetchone()
            if parent and parent['user_id'] != uid:
                me = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
                me_name = me['username'] if me else str(uid)
                add_notification(db, parent['user_id'], f'💬 @{me_name} replied to your post.')
    except Exception as _e:
        logger.warning('reply notification failed: %s', _e)
    try:
        if repost_of:
            db.execute('UPDATE posts SET repost_count=repost_count+1 WHERE id=?', (repost_of,))
            parent = db.execute('SELECT user_id FROM posts WHERE id=?', (repost_of,)).fetchone()
            if parent and parent['user_id'] != uid:
                me = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
                me_name = me['username'] if me else str(uid)
                add_notification(db, parent['user_id'], f'🔁 @{me_name} reposted your post.')
    except Exception as _e:
        logger.warning('repost notification failed: %s', _e)

    tags = list(set(t.lower() for t in re.findall(r'#(\w+)', body or '')))
    for tag in tags[:10]:
        db.execute('INSERT OR IGNORE INTO hashtags (name) VALUES (?) ', (tag,))
        ht = db.execute('SELECT id FROM hashtags WHERE name=?', (tag,)).fetchone()
        if ht:
            db.execute('INSERT OR IGNORE INTO post_hashtags (post_id,hashtag_id) VALUES (?,?) ',
                       (post_id, ht['id']))
    if tags:
        db.execute('UPDATE posts SET hashtags_cached=? WHERE id=?',
                   (' '.join('#' + t for t in tags), post_id))

    if body:
        mentioned = list(set(re.findall(r'@(\w+)', body)))
        me_row    = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
        me_name   = me_row['username'] if me_row else ''
        for username in mentioned[:10]:
            if username.lower() == me_name.lower():
                continue
            target = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
            if target and target['id'] != uid:
                add_notification(db, target['id'],
                    f'🔔 @{me_name} mentioned you in a post.',
                    icon='mention', link=f'/post/{post_id}')

    update_counts(db, uid)
    recalc_post_score(db, post_id)
    db.commit()

    # Background: fetch Open Graph tags for any URL in the post body
    if body:
        _urls = re.findall(r'https?://[^\s<>"]+', body)
        if _urls:
            _fetch_url = _urls[0]

            def _fetch_og(app_ctx, pid, url):
                try:
                    import requests as _req
                    from html.parser import HTMLParser as _HP
                    import sqlite3 as _sq3

                    class _OGParser(_HP):
                        def __init__(self):
                            super().__init__()
                            self.og = {}
                        def handle_starttag(self, tag, attrs):
                            if tag == 'meta':
                                ad = dict(attrs)
                                prop = ad.get('property', '') or ad.get('name', '')
                                if prop in ('og:title','og:description','og:image','og:url'):
                                    self.og[prop] = ad.get('content', '')[:500]

                    r = _req.get(url, timeout=5, headers={'User-Agent': 'DUYSBoostBot/1.0'}, allow_redirects=True)
                    parser = _OGParser()
                    parser.feed(r.text[:40000])
                    og = parser.og
                    if og:
                        with app_ctx:
                            from db import get_db as _gdb_og
                            _db_og = _gdb_og()
                            _db_og.execute(
                                'UPDATE posts SET og_url=?,og_title=?,og_description=?,og_image=? WHERE id=?',
                                (og.get('og:url', url)[:500],
                                 og.get('og:title', '')[:200] or None,
                                 og.get('og:description', '')[:400] or None,
                                 og.get('og:image', '')[:500] or None,
                                 pid)
                            )
                            _db_og.commit()
                except Exception:
                    pass

            from flask import current_app as _ca_og
            from helpers import _bg_pool as _pool
            _pool.submit(_fetch_og, _ca_og.app_context(), post_id, _fetch_url)

    if post_status == 'scheduled':
        return jsonify({'success': True, 'scheduled': True,
                        'message': f'Post scheduled for {scheduled_at[:16].replace("T", " ")} UTC'})

    try:
        post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
        return jsonify({'success': True, 'post': format_post(post, uid, db)})
    except Exception as _fe:
        # The post was saved — just return success without the enriched data
        logger.warning('format_post failed after create_post (post saved): %s', _fe)
        return jsonify({'success': True, 'post': {
            'id': post_id, 'body': body or '', 'user_id': uid,
            'created_at': now, 'like_count': 0, 'reply_count': 0,
            'repost_count': 0, 'view_count': 0, 'is_boosted': 0,
            'media_url': media_url, 'media_mime': media_mime,
            'author': {}, 'liked': False, 'bookmarked': False,
            'reposted': False, 'active_boost': None, 'locked': False,
        }})



@bp.route('/api/post/<int:post_id>/replies')
@login_required
@csrf_exempt
def post_replies_api(post_id):
    """Return up to 50 latest replies for a post, oldest first."""
    db  = get_db()
    rows = db.execute("""
        SELECT p.id, p.body, p.media_url, p.media_mime, p.created_at,
               u.username, u.display_name, u.avatar_url, u.is_verified,
               u.verified_tier
        FROM posts p
        JOIN users u ON u.id = p.user_id
        WHERE p.reply_to_id = ?
        ORDER BY p.created_at ASC
        LIMIT 50
    """, (post_id,)).fetchall()
    replies = []
    for r in rows:
        d = dict(r)
        replies.append({
            'id':          d['id'],
            'body':        d['body'],
            'media_url':   d.get('media_url'),
            'media_mime':  d.get('media_mime'),
            'created_at':  d['created_at'],
            'author': {
                'username':      d['username'],
                'display_name':  d.get('display_name'),
                'avatar_url':    d.get('avatar_url'),
                'is_verified':   d.get('is_verified'),
                'verified_tier': d.get('verified_tier'),
            }
        })
    return jsonify({'success': True, 'replies': replies})


@bp.route('/post/<int:post_id>/delete', methods=['POST'])
@login_required
@csrf_exempt
def delete_post(post_id):
    db  = get_db()
    uid = session['user_id']
    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    user = db.execute('SELECT is_admin FROM users WHERE id=?', (uid,)).fetchone()
    if post['user_id'] != uid and not (user and user['is_admin']):
        return jsonify({'success': False, 'error': 'Forbidden'}), 403

    _full_delete_post(db, post_id)
    update_counts(db, post['user_id'])
    db.commit()
    return jsonify({'success': True})


@bp.route('/api/report', methods=['POST'])
@login_required
@csrf_exempt
@limiter.limit('10 per hour')
def report_content():
    """Submit a content report (post, user, or message)."""
    db          = get_db()
    uid         = session['user_id']
    target_type = (request.json or {}).get('target_type', '')
    target_id   = safe_int((request.json or {}).get('target_id'), 0)
    reason      = ((request.json or {}).get('reason') or '').strip()
    details     = ((request.json or {}).get('details') or '').strip()

    if target_type not in ('post', 'user', 'message'):
        return jsonify({'success': False, 'error': 'Invalid target type.'}), 400
    if not target_id or not reason:
        return jsonify({'success': False, 'error': 'Target and reason required.'}), 400

    # Prevent duplicate open reports from the same user
    existing = db.execute(
        "SELECT id FROM reports WHERE reporter_id=? AND target_type=? "
        "AND target_id=? AND status='open'",
        (uid, target_type, target_id)
    ).fetchone()
    if existing:
        return jsonify({'success': False, 'error': 'You have already reported this content.'}), 400

    db.execute(
        'INSERT INTO reports (reporter_id, target_type, target_id, reason, details) '
        'VALUES (?, ?, ?, ?, ?)',
        (uid, target_type, target_id, reason, details or None)
    )
    db.commit()
    return jsonify({'success': True})


@bp.route('/post/<int:post_id>/unrepost', methods=['POST'])
@login_required
@csrf_exempt   # JSON POST
def unrepost(post_id):
    """Delete any reposts/quotes the current user made of this post."""
    db  = get_db()
    uid = session['user_id']

    # Find all reposts/quotes of this post by the current user
    reposts = db.execute(
        'SELECT id FROM posts WHERE user_id=? AND repost_of_id=?',
        (uid, post_id)
    ).fetchall()

    if not reposts:
        return jsonify({'success': False, 'error': 'You have not reposted this.'}), 404

    deleted_ids = [r['id'] for r in reposts]
    for rid in deleted_ids:
        db.execute('DELETE FROM post_likes WHERE post_id=?', (rid,))
        db.execute('DELETE FROM bookmarks  WHERE post_id=?', (rid,))
        db.execute('DELETE FROM posts      WHERE id=?',       (rid,))

    # Decrement repost_count by the number of reposts removed
    db.execute(
        'UPDATE posts SET repost_count = MAX(0, repost_count - ?) WHERE id=?',
        (len(deleted_ids), post_id)
    )
    update_counts(db, uid)
    recalc_post_score(db, post_id)
    db.commit()

    new_count = db.execute(
        'SELECT repost_count FROM posts WHERE id=?', (post_id,)
    ).fetchone()
    return jsonify({
        'success':      True,
        'reposted':     False,
        'repost_count': new_count['repost_count'] if new_count else 0,
        'removed':      len(deleted_ids),
    })


@bp.route('/post/<int:post_id>/edit', methods=['POST'])
@login_required
@csrf_exempt
def edit_post(post_id):
    db  = get_db()
    uid = session['user_id']
    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Post not found.'}), 404
    if post['user_id'] != uid:
        return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    body = (request.form.get('body') or '').strip()
    if len(body) > 500:
        return jsonify({'success': False, 'error': 'Max 500 characters.'}), 400
    if not body and not post.get('media_url'):
        return jsonify({'success': False, 'error': 'Post cannot be empty.'}), 400

    # Save old body to edit history before updating
    old = db.execute('SELECT body FROM posts WHERE id=?', (post_id,)).fetchone()
    if old and old['body']:
        try:
            db.execute('INSERT INTO post_edits (post_id, body) VALUES (?,?)', (post_id, old['body']))
        except Exception:
            pass

    now = datetime.now(timezone.utc).isoformat()
    db.execute('UPDATE posts SET body=?, edited_at=? WHERE id=?', (body or None, now, post_id))
    db.commit()

    updated = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    return jsonify({'success': True, 'post': format_post(updated, uid, db)})


@bp.route('/post/<int:post_id>/pin', methods=['POST'])
@login_required
@csrf_exempt
def pin_post(post_id):
    """Toggle pin on a post. Only the owner can pin; only one post pinned at a time."""
    db  = get_db()
    uid = session['user_id']
    post = db.execute('SELECT user_id, is_pinned FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Post not found.'}), 404
    if post['user_id'] != uid:
        return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    currently_pinned = bool(post['is_pinned'])
    if currently_pinned:
        db.execute('UPDATE posts SET is_pinned=0 WHERE id=?', (post_id,))
        db.commit()
        return jsonify({'success': True, 'pinned': False})
    else:
        # Unpin any existing pinned post for this user
        db.execute('UPDATE posts SET is_pinned=0 WHERE user_id=? AND is_pinned=1', (uid,))
        db.execute('UPDATE posts SET is_pinned=1 WHERE id=?', (post_id,))
        db.commit()
        return jsonify({'success': True, 'pinned': True})


@bp.route('/post/<int:post_id>')
@login_required
def post_detail(post_id):
    db  = get_db()
    uid = session['user_id']
    row = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not row:
        return render_template('error.html', code=404, message='Post not found.'), 404
    post = format_post_with_poll(row, uid, db)

    # Build 3-level thread: direct replies + their sub-replies
    direct_reply_rows = db.execute(
        'SELECT * FROM posts WHERE reply_to_id=? ORDER BY created_at ASC', (post_id,)
    ).fetchall()
    replies = []
    for r in direct_reply_rows:
        fp = format_post_with_poll(r, uid, db)
        sub_rows = db.execute(
            'SELECT * FROM posts WHERE reply_to_id=? ORDER BY created_at ASC LIMIT 10', (r['id'],)
        ).fetchall()
        fp['sub_replies'] = [format_post_with_poll(s, uid, db) for s in sub_rows]
        replies.append(fp)

    return render_template('post_detail.html', post=post, replies=replies)


@bp.route('/post/<int:post_id>/like', methods=['POST'])
@login_required
@limiter.limit(LIMIT_LIKE)
@csrf_exempt   # JSON POST
def toggle_like(post_id):
    db  = get_db()
    uid = session['user_id']
    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Not found'}), 404

    existing = db.execute('SELECT 1 FROM post_likes WHERE user_id=? AND post_id=?',
                          (uid, post_id)).fetchone()
    if existing:
        db.execute('DELETE FROM post_likes WHERE user_id=? AND post_id=?', (uid, post_id))
        db.execute('UPDATE posts SET like_count=MAX(0, like_count-1) WHERE id=?', (post_id,))
        liked = False
    else:
        db.execute('INSERT OR IGNORE INTO post_likes (user_id,post_id) VALUES (?,?) ', (uid, post_id))
        db.execute('UPDATE posts SET like_count=like_count+1 WHERE id=?', (post_id,))
        liked = True
        if post['user_id'] != uid:
            me = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
            add_notification(db, post['user_id'], f'❤️ @{me["username"]} liked your post.')

    _lc_row = db.execute('SELECT like_count FROM posts WHERE id=?', (post_id,)).fetchone()
    new_count = _lc_row['like_count'] if _lc_row else 0
    recalc_post_score(db, post_id)
    db.commit()
    return jsonify({'success': True, 'liked': liked, 'like_count': new_count})


@bp.route('/post/<int:post_id>/react', methods=['POST'])
@login_required
@limiter.limit(LIMIT_LIKE)
@csrf_exempt
def react_post(post_id):
    """Add / change / remove a reaction on a post."""
    db   = get_db()
    uid  = session['user_id']
    data = request.get_json(silent=True) or {}
    reaction = (data.get('reaction') or '').strip()
    VALID = {'fire', 'heart', 'laugh', 'target', 'money'}
    if reaction not in VALID and reaction != '':
        return jsonify({'success': False, 'error': 'Invalid reaction'}), 400

    post = db.execute('SELECT user_id FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Not found'}), 404

    existing = db.execute(
        'SELECT reaction_type FROM post_reactions WHERE user_id=? AND post_id=?',
        (uid, post_id)
    ).fetchone()

    if reaction == '' or (existing and existing['reaction_type'] == reaction):
        # Remove reaction (toggle off)
        db.execute('DELETE FROM post_reactions WHERE user_id=? AND post_id=?', (uid, post_id))
        # Also remove from post_likes for consistency
        db.execute('DELETE FROM post_likes WHERE user_id=? AND post_id=?', (uid, post_id))
        db.execute('UPDATE posts SET like_count=MAX(0, like_count-1) WHERE id=? AND like_count>0', (post_id,))
        active_reaction = None
    else:
        if existing:
            db.execute('UPDATE post_reactions SET reaction_type=? WHERE user_id=? AND post_id=?',
                       (reaction, uid, post_id))
        else:
            db.execute('INSERT INTO post_reactions (user_id,post_id,reaction_type) VALUES (?,?,?)',
                       (uid, post_id, reaction))
            # Keep like_count in sync for feed ranking
            db.execute('INSERT OR IGNORE INTO post_likes (user_id,post_id) VALUES (?,?)', (uid, post_id))
            db.execute('UPDATE posts SET like_count=like_count+1 WHERE id=?', (post_id,))
            if post['user_id'] != uid:
                me = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
                EMOJI_MAP = {'fire':'🔥','heart':'❤️','laugh':'😂','target':'🎯','money':'💰'}
                add_notification(db, post['user_id'],
                    f'{EMOJI_MAP.get(reaction,"❤️")} @{me["username"]} reacted to your post.')
        active_reaction = reaction

    # Tally all reactions for this post
    rows = db.execute(
        'SELECT reaction_type, COUNT(*) as cnt FROM post_reactions WHERE post_id=? GROUP BY reaction_type',
        (post_id,)
    ).fetchall()
    counts = {r['reaction_type']: r['cnt'] for r in rows}
    total  = sum(counts.values())

    recalc_post_score(db, post_id)
    db.commit()
    return jsonify({'success': True, 'reaction': active_reaction, 'counts': counts, 'total': total})


@bp.route('/post/<int:post_id>/bookmark', methods=['POST'])
@login_required
@limiter.limit(LIMIT_LIKE)
@csrf_exempt   # JSON POST
def toggle_bookmark(post_id):
    db  = get_db()
    uid = session['user_id']
    existing = db.execute('SELECT 1 FROM bookmarks WHERE user_id=? AND post_id=?',
                          (uid, post_id)).fetchone()
    if existing:
        db.execute('DELETE FROM bookmarks WHERE user_id=? AND post_id=?', (uid, post_id))
        saved = False
    else:
        db.execute('INSERT OR IGNORE INTO bookmarks (user_id,post_id) VALUES (?,?) ', (uid, post_id))
        saved = True
    db.commit()
    return jsonify({'success': True, 'saved': saved})


@bp.route('/bookmarks')
@login_required
def bookmarks():
    db  = get_db()
    uid = session['user_id']
    rows = db.execute("""
        SELECT p.* FROM posts p JOIN bookmarks b ON b.post_id=p.id
        WHERE b.user_id=? ORDER BY b.created_at DESC
    """, (uid,)).fetchall()
    return render_template('bookmarks.html', posts=[format_post(r, uid, db) for r in rows])


# ── Follow / profile ─────────────────────────────────────────────────────────

@bp.route('/user/<username>/follow', methods=['POST'])
@login_required
@limiter.limit(LIMIT_FOLLOW)
@csrf_exempt   # JSON POST
def toggle_follow(username):
    db  = get_db()
    uid = session['user_id']
    target = db.execute('SELECT id,username FROM users WHERE username=?', (username,)).fetchone()
    if not target or target['id'] == uid:
        return jsonify({'success': False, 'error': 'Not found'}), 404

    existing = db.execute('SELECT 1 FROM follows WHERE follower_id=? AND following_id=?',
                          (uid, target['id'])).fetchone()
    if existing:
        db.execute('DELETE FROM follows WHERE follower_id=? AND following_id=?', (uid, target['id']))
        following = False
    else:
        db.execute('INSERT OR IGNORE INTO follows (follower_id,following_id) VALUES (?,?) ',
                   (uid, target['id']))
        following = True
        me = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
        me_name = me['username'] if me else str(uid)
        add_notification(db, target['id'], f'👤 @{me_name} started following you.')

    update_counts(db, uid)
    update_counts(db, target['id'])
    db.commit()

    _fc_row = db.execute('SELECT follower_count FROM users WHERE id=?',
                         (target['id'],)).fetchone()
    new_followers = _fc_row['follower_count'] if _fc_row else 0
    return jsonify({'success': True, 'following': following, 'follower_count': new_followers})


@bp.route('/user/<username>')
@login_required
def profile(username):
    db  = get_db()
    uid = session['user_id']
    _target_row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not _target_row:
        return render_template('error.html', message='User not found'), 404
    target = dict(_target_row)
    target.setdefault('is_verified', 0)
    target.setdefault('verified_tier', 'blue')
    target.setdefault('balance', 0.0)
    target.setdefault('bio', '')
    target.setdefault('follower_count', 0)
    target.setdefault('following_count', 0)
    target.setdefault('post_count', 0)
    target.setdefault('show_online', 1)

    tab      = request.args.get('tab', 'posts')
    page     = max(1, safe_int(request.args.get('page'), 1))
    per      = 20
    offset   = (page - 1) * per
    is_own   = (uid == target['id'])
    is_following = bool(db.execute('SELECT 1 FROM follows WHERE follower_id=? AND following_id=?',
                                   (uid, target['id'])).fetchone())

    if tab == 'replies':
        rows = db.execute(
            'SELECT * FROM posts WHERE user_id=? AND reply_to_id IS NOT NULL '
            'ORDER BY created_at DESC LIMIT ? OFFSET ?',
            (target['id'], per, offset)
        ).fetchall()
    elif tab == 'likes':
        rows = db.execute(
            'SELECT p.* FROM posts p JOIN post_likes l ON l.post_id=p.id '
            'WHERE l.user_id=? ORDER BY l.created_at DESC LIMIT ? OFFSET ?',
            (target['id'], per, offset)
        ).fetchall()
    else:
        _sched_filter = '' if is_own else "AND (status IS NULL OR status='published') "
        rows = db.execute(
            'SELECT * FROM posts WHERE user_id=? AND reply_to_id IS NULL '
            + _sched_filter +
            'AND id NOT IN (SELECT post_id FROM channel_posts) '
            'ORDER BY created_at DESC LIMIT ? OFFSET ?',
            (target['id'], per, offset)
        ).fetchall()

    has_more  = len(rows) == per
    posts     = [format_post_with_poll(r, uid, db) for r in rows]

    # Prepend pinned post on page 1 (posts tab only)
    if tab == 'posts' and page == 1:
        pinned_row = db.execute(
            'SELECT * FROM posts WHERE user_id=? AND is_pinned=1 LIMIT 1', (target['id'],)
        ).fetchone()
        if pinned_row:
            pinned = format_post_with_poll(pinned_row, uid, db)
            pinned['_is_pinned_header'] = True
            posts = [p for p in posts if p['id'] != pinned_row['id']]
            posts.insert(0, pinned)
    followers = [dict(f) for f in db.execute("""
        SELECT u.id, u.username, u.display_name, u.avatar_url, u.is_verified
        FROM follows f JOIN users u ON u.id=f.follower_id
        WHERE f.following_id=? LIMIT 6
    """, (target['id'],)).fetchall()]

    tier = db.execute(
        "SELECT * FROM subscription_tiers WHERE creator_id=? AND is_active=1", (target['id'],)
    ).fetchone()
    is_subscribed = bool(db.execute(
        "SELECT 1 FROM subscriptions WHERE subscriber_id=? AND creator_id=? AND status='active'",
        (uid, target['id'])
    ).fetchone()) if not is_own and tier else False

    top_tips = [dict(t) for t in db.execute("""
        SELECT t.amount, t.message, u.username, u.avatar_url
        FROM tips t JOIN users u ON u.id=t.from_user_id
        WHERE t.to_user_id=? ORDER BY t.amount DESC LIMIT 3
    """, (target['id'],)).fetchall()]

    target_online = False
    if target.get('show_online') and target.get('online_at') and not is_own:
        try:
            _last = datetime.fromisoformat(target['online_at'].replace('Z', ''))
            if _last.tzinfo is None:
                _last = _last.replace(tzinfo=timezone.utc)
            target_online = (datetime.now(timezone.utc) - _last).total_seconds() < 90
        except Exception:
            pass

    return render_template('profile.html', target=dict(target),
                           posts=posts, tab=tab,
                           page=page, has_more=has_more,
                           is_following=is_following, is_own=is_own,
                           followers=followers,
                           tier=dict(tier) if tier else None,
                           is_subscribed=is_subscribed,
                           top_tips=top_tips,
                           target_online=target_online)


@bp.route('/profile/edit', methods=['GET', 'POST'])
@login_required
def edit_profile():
    db  = get_db()
    uid = session['user_id']
    if request.method == 'POST':
        display_name = (request.form.get('display_name') or '').strip()[:60]
        bio          = (request.form.get('bio')          or '').strip()[:160]
        website      = (request.form.get('website')      or '').strip()[:120]
        location     = (request.form.get('location')     or '').strip()[:60]
        allow_saves  = 1 if request.form.get('allow_post_saves', '1') != '0' else 0

        db.execute('UPDATE users SET display_name=?, bio=?, website=?, location=?, allow_post_saves=? WHERE id=?',
                   (display_name or None, bio or None, website or None, location or None, allow_saves, uid))
        db.commit()
        me = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
        return jsonify({'success': True, 'redirect': url_for('social.profile', username=me['username'])})

    user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    user_d = dict(user)
    changes_made = user_d.get('username_changes', 0) or 0
    username_changes_left = max(0, 3 - changes_made)
    last_changed = user_d.get('username_last_changed') or ''
    totp_enabled = bool(user_d.get('totp_enabled', 0))
    return render_template('edit_profile.html', user=user_d,
                           username_changes_left=username_changes_left,
                           username_last_changed=last_changed,
                           totp_enabled=totp_enabled)



@bp.route('/profile/change-username', methods=['POST'])
@login_required
@csrf_exempt
def change_username():
    """
    Change current user's username.
    Rules:
      - 3 changes maximum per account lifetime
      - 60 days cooldown between changes
      - Username must be alphanumeric + underscore, 3-30 chars
      - Must not conflict with an existing username
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    import re as _re

    db   = get_db()
    uid  = session['user_id']
    data = request.get_json(silent=True) or {}
    new_un = (data.get('username') or '').strip().lower()

    # Format check
    if not _re.match(r'^[a-z0-9_]{3,30}$', new_un):
        return jsonify({'success': False,
                       'error': 'Username must be 3–30 letters, numbers, or underscores.'}), 400

    user = db.execute(
        'SELECT username, username_changes, username_last_changed '
        'FROM users WHERE id=?', (uid,)
    ).fetchone()
    if not user:
        return jsonify({'success': False, 'error': 'User not found.'}), 404

    if new_un == user['username']:
        return jsonify({'success': False, 'error': 'This is already your username.'}), 400

    # Lifetime limit (3)
    changes = user['username_changes'] or 0
    if changes >= 3:
        return jsonify({'success': False,
                       'error': 'You\'ve reached the maximum of 3 username changes.'}), 400

    # 60-day cooldown
    last = user['username_last_changed']
    if last:
        try:
            last_dt = _dt.fromisoformat(last.replace('Z',''))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=_tz.utc)
            days_since = (_dt.now(_tz.utc) - last_dt).days
            if days_since < 60:
                remaining = 60 - days_since
                return jsonify({'success': False,
                               'error': f'You can change your username again in {remaining} day(s).'}), 400
        except Exception:
            pass

    # Conflict check
    taken = db.execute('SELECT 1 FROM users WHERE username=? AND id!=?',
                       (new_un, uid)).fetchone()
    if taken:
        return jsonify({'success': False, 'error': 'Username already taken.'}), 400

    # Apply
    now = _dt.now(_tz.utc).strftime('%Y-%m-%d %H:%M:%S')
    db.execute(
        'UPDATE users SET username=?, username_changes=username_changes+1, '
        'username_last_changed=? WHERE id=?',
        (new_un, now, uid)
    )
    # Also update referral_code if it equals the old username
    db.execute(
        'UPDATE users SET referral_code=? WHERE id=? AND referral_code=?',
        (new_un, uid, user['username'])
    )
    db.commit()
    return jsonify({
        'success': True,
        'username': new_un,
        'changes_used': changes + 1,
        'changes_remaining': 3 - (changes + 1)
    })


@bp.route('/account/delete', methods=['POST'])
@login_required
@csrf_exempt
def delete_account():
    """
    Hard-delete the current user's account and all their data.
    Requires the user to type their password to confirm.
    """
    import storage as _st
    from helpers import verify_password
    db  = get_db()
    udb   = get_user_db()
    uid = session['user_id']

    data   = request.get_json(silent=True) or {}
    phrase = (data.get('phrase') or '').strip().lower()
    user   = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not user:
        return jsonify({'success': False, 'error': 'User not found.'}), 404

    # Require typed phrase confirmation — works for all auth methods
    if phrase != 'delete':
        return jsonify({'success': False,
                        'error': 'Please type "delete" to confirm.'}), 400

    # Delete R2 media files
    for row in db.execute('SELECT media_url FROM stories WHERE user_id=?', (uid,)).fetchall():
        try: _st.delete_object(row['media_url'])
        except Exception: pass
    if user.get('avatar_url') and user['avatar_url'].startswith('http'):
        try: _st.delete_object(user['avatar_url'])
        except Exception: pass
    if user.get('banner_url') and user['banner_url'].startswith('http'):
        try: _st.delete_object(user['banner_url'])
        except Exception: pass

    # Delete all user data (cascades via FK where set, manual otherwise)
    # Delete stories — R2 media AND DB rows
    story_rows = db.execute('SELECT media_url FROM stories WHERE user_id=?', (uid,)).fetchall()
    for sr in story_rows:
        try:
            if sr['media_url']:
                storage.delete_object(sr['media_url'])
        except Exception:
            pass
    db.execute('DELETE FROM stories WHERE user_id=?', (uid,))
    # personal data already deleted via udb above
    db.execute('DELETE FROM task_completions WHERE worker_id=?', (uid,))
    db.execute('DELETE FROM post_likes       WHERE user_id=?', (uid,))
    db.execute('DELETE FROM bookmarks        WHERE user_id=?', (uid,))
    db.execute('DELETE FROM follows          WHERE follower_id=? OR following_id=?', (uid, uid))
    db.execute('DELETE FROM search_history   WHERE user_id=?', (uid,))
    db.execute('DELETE FROM post_views       WHERE user_id=?', (uid,))
    db.execute('DELETE FROM poll_votes       WHERE user_id=?', (uid,))
    # personal subscriptions/tips already deleted via udb above
    # ── Delete personal DB data via udb ──────────────────────────────────────
    try:
        udb = get_user_db()
        for tbl, col in [
            ('notifications',      'user_id'),
            ('transactions',       'user_id'),
            ('withdrawals',        'user_id'),
            ('crypto_deposits',    'user_id'),
            ('tips',               'from_user_id'),
            ('tips',               'to_user_id'),
            ('subscriptions',      'subscriber_id'),
            ('subscriptions',      'creator_id'),
            ('subscription_tiers', 'creator_id'),
            ('conversations',      'user_a'),
            ('conversations',      'user_b'),
        ]:
            try:
                udb.execute(f'DELETE FROM {tbl} WHERE {col}=?', (uid,))
            except Exception:
                pass
        # messages are cascade-deleted when conversations are gone
        # but explicitly clean up orphans
        try:
            udb.execute(
                'DELETE FROM messages WHERE conversation_id NOT IN (SELECT id FROM conversations)'
            )
        except Exception:
            pass
        udb.commit()
    except Exception as _ue:
        import logging as _log
        _log.getLogger(__name__).warning('delete_account personal DB cleanup: %s', _ue)

    # ── Delete global DB data ─────────────────────────────────────────────────
    # Delete posts — cascade DB rows AND R2 media
    post_rows = db.execute('SELECT * FROM posts WHERE user_id=?', (uid,)).fetchall()
    for post_row in post_rows:
        _delete_post_media(post_row)          # delete R2 file
        pid = post_row['id']
        for tbl in ('post_likes', 'bookmarks', 'poll_votes', 'poll_options',
                    'post_hashtags', 'channel_posts', 'post_views',
                    'boost_engagements', 'post_boosts'):
            try:
                db.execute(f'DELETE FROM {tbl} WHERE post_id=?', (pid,))
            except Exception:
                pass
    db.execute('DELETE FROM posts WHERE user_id=?', (uid,))
    # Leave groups/channels (don't delete them)
    db.execute('DELETE FROM channel_members WHERE user_id=?', (uid,))
    db.execute('DELETE FROM group_members   WHERE user_id=?', (uid,))
    # Delete DMs (conversations and messages live in personal DB)
    try:
        conv_ids = [r['id'] for r in udb.execute(
            'SELECT id FROM conversations WHERE user_a=? OR user_b=?', (uid, uid)
        ).fetchall()]
        for cid in conv_ids:
            udb.execute('DELETE FROM messages     WHERE conversation_id=?', (cid,))
            udb.execute('DELETE FROM conversations WHERE id=?', (cid,))
    except Exception:
        pass
    # Delete global subscription/tip data
    for tbl, col in [
        ('subscriptions', 'subscriber_id'), ('subscriptions', 'creator_id'),
        ('subscription_tiers', 'creator_id'),
        ('tips', 'from_user_id'), ('tips', 'to_user_id'),
        ('pending_withdrawals', 'user_id'),
    ]:
        try:
            db.execute(f'DELETE FROM {tbl} WHERE {col}=?', (uid,))
        except Exception:
            pass
    # Delete user
    db.execute('DELETE FROM users WHERE id=?', (uid,))
    db.commit()

    session.clear()
    return jsonify({'success': True, 'redirect': url_for('auth.index')})


@bp.route('/profile/upload-photo', methods=['POST'])
@login_required
@limiter.limit(LIMIT_UPLOAD)
@csrf_exempt
def upload_profile_photo():
    db    = get_db()
    uid   = session['user_id']
    photo = request.files.get('photo')
    kind  = (request.form.get('type') or 'avatar').strip().lower()

    if kind not in ('avatar', 'banner'):
        return jsonify({'success': False, 'error': 'Invalid photo type.'}), 400
    if not photo or not photo.filename:
        return jsonify({'success': False, 'error': 'No file selected.'}), 400

    mime = photo.mimetype or ''
    try:
        if kind == 'avatar':
            url = storage.upload_avatar(uid, photo, mime)
        else:
            url = storage.upload_banner(uid, photo, mime)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except RuntimeError as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    col = 'avatar_url' if kind == 'avatar' else 'banner_url'
    db.execute(f'UPDATE users SET {col}=? WHERE id=?', (url, uid))
    db.commit()
    return jsonify({'success': True, 'url': url, 'type': kind})


@bp.route('/user/<username>/followers')
@login_required
def follower_list(username):
    db  = get_db()
    uid = session['user_id']
    target = db.execute('SELECT id,username,display_name FROM users WHERE username=?', (username,)).fetchone()
    if not target:
        return render_template('error.html', code=404, message='User not found.'), 404
    rows = db.execute("""
        SELECT u.id, u.username, u.display_name, u.avatar_url, u.is_verified,
               u.verified_tier, u.follower_count, u.bio,
               EXISTS(SELECT 1 FROM follows WHERE follower_id=? AND following_id=u.id) AS you_follow
        FROM follows f JOIN users u ON u.id=f.follower_id
        WHERE f.following_id=? ORDER BY f.created_at DESC LIMIT 100
    """, (uid, target['id'])).fetchall()
    return render_template('follow_list.html', target=dict(target),
                           users=[dict(r) for r in rows], list_type='Followers')


@bp.route('/user/<username>/following')
@login_required
def following_list(username):
    db  = get_db()
    uid = session['user_id']
    target = db.execute('SELECT id,username,display_name FROM users WHERE username=?', (username,)).fetchone()
    if not target:
        return render_template('error.html', code=404, message='User not found.'), 404
    rows = db.execute("""
        SELECT u.id, u.username, u.display_name, u.avatar_url, u.is_verified,
               u.verified_tier, u.follower_count, u.bio,
               EXISTS(SELECT 1 FROM follows WHERE follower_id=? AND following_id=u.id) AS you_follow
        FROM follows f JOIN users u ON u.id=f.following_id
        WHERE f.follower_id=? ORDER BY f.created_at DESC LIMIT 100
    """, (uid, target['id'])).fetchall()
    return render_template('follow_list.html', target=dict(target),
                           users=[dict(r) for r in rows], list_type='Following')


# ── Discovery ────────────────────────────────────────────────────────────────

def _save_search(db, uid, query, result_type='mixed'):
    if not query or len(query) < 2:
        return
    db.execute('INSERT OR IGNORE INTO search_history (user_id, query, result_type) VALUES (?,?,?)',
               (uid, query[:100], result_type))
    db.execute('UPDATE users SET search_count=search_count+1 '
               'WHERE username LIKE ? OR display_name LIKE ?',
               (f'%{query}%', f'%{query}%'))


def _trending_hashtags(db, hours=48, limit=15):
    rows = db.execute("""
        SELECT h.name,
               COUNT(ph.post_id) AS cnt,
               COUNT(CASE WHEN p.created_at >= datetime('now', '-6 hours') THEN 1 END) AS recent_cnt
        FROM hashtags h
        JOIN post_hashtags ph ON ph.hashtag_id=h.id
        JOIN posts p ON p.id=ph.post_id
        WHERE p.created_at >= ?
        GROUP BY h.id HAVING cnt > 0
        ORDER BY (recent_cnt*3+cnt) DESC LIMIT ?
    """, (hours, limit)).fetchall()
    return [dict(r) for r in rows]


def _who_to_follow(db, uid, limit=8):
    rows = db.execute("""
        SELECT u.id, u.username, u.display_name, u.avatar_url,
               u.is_verified, u.verified_tier, u.follower_count, u.bio, u.subscriber_count,
               COUNT(DISTINCT f2.follower_id) AS mutual_count
        FROM users u
        JOIN follows f1 ON f1.following_id=u.id
        JOIN follows f2 ON f2.following_id=f1.follower_id
        WHERE f2.follower_id=? AND u.id!=?
          AND u.id NOT IN (SELECT following_id FROM follows WHERE follower_id=?)
        GROUP BY u.id
        ORDER BY mutual_count DESC, u.follower_count DESC LIMIT ?
    """, (uid, uid, uid, limit)).fetchall()

    if len(rows) < limit:
        existing_ids = [r['id'] for r in rows] + [uid]
        ph    = ','.join(['?'] * len(existing_ids))
        extra = db.execute(
            f'SELECT id,username,display_name,avatar_url,is_verified,verified_tier,follower_count,bio,'
            f'subscriber_count, 0 AS mutual_count FROM users WHERE id NOT IN ({ph}) '
            f'AND id NOT IN (SELECT following_id FROM follows WHERE follower_id=?) '
            f'ORDER BY follower_count DESC LIMIT ?',
            existing_ids + [uid, limit - len(rows)]
        ).fetchall()
        rows = list(rows) + list(extra)

    return [dict(r) for r in rows]


@bp.route('/explore')
@login_required
def explore():
    db   = get_db()
    uid  = session['user_id']
    q    = request.args.get('q', '').strip()
    tab  = request.args.get('tab', 'top')

    posts, users, tags = [], [], []

    if q:
        _save_search(db, uid, q)
        like = f'%{q}%'
        if tab in ('top', 'posts', 'latest'):
            order = 'p.created_at DESC' if tab == 'latest' else 'p.score DESC, p.like_count DESC'
            post_rows = db.execute(f'SELECT p.* FROM posts p WHERE p.body LIKE ? '
                                   f'AND p.reply_to_id IS NULL ORDER BY {order} LIMIT 40',
                                   (like,)).fetchall()
            posts = [format_post(r, uid, db) for r in post_rows]

        if tab in ('top', 'people'):
            user_rows = db.execute("""
                SELECT id, username, display_name, avatar_url, is_verified,
                       verified_tier, follower_count, bio, subscriber_count,
                       EXISTS(SELECT 1 FROM follows WHERE follower_id=? AND following_id=id) AS you_follow
                FROM users WHERE (username LIKE ? OR display_name LIKE ?) AND id != ?
                ORDER BY follower_count DESC LIMIT 12
            """, (uid, like, like, uid)).fetchall()
            users = [dict(u) for u in user_rows]

        if tab in ('top', 'tags'):
            tag_q = q.lstrip('#').lower()
            tag_rows = db.execute("""
                SELECT h.name, COUNT(ph.post_id) AS cnt
                FROM hashtags h JOIN post_hashtags ph ON ph.hashtag_id=h.id
                WHERE h.name LIKE ? GROUP BY h.id ORDER BY cnt DESC LIMIT 10
            """, (f'%{tag_q}%',)).fetchall()
            tags = [dict(t) for t in tag_rows]
        db.commit()

    trending_tags  = _trending_hashtags(db, hours=48, limit=12)
    who_to_follow  = _who_to_follow(db, uid, limit=6)
    history        = db.execute('SELECT DISTINCT query FROM search_history '
                                'WHERE user_id=? ORDER BY created_at DESC LIMIT 8', (uid,)).fetchall()
    recent_searches = [r['query'] for r in history]
    trending_posts = [format_post(r, uid, db) for r in db.execute("""
        SELECT p.* FROM posts p WHERE p.reply_to_id IS NULL
          AND p.created_at >= datetime('now', '-6 hours')
        ORDER BY p.score DESC LIMIT 8
    """).fetchall()]

    return render_template('explore.html', q=q, tab=tab,
                           posts=posts, users=users, tags=tags,
                           trending_tags=trending_tags,
                           who_to_follow=who_to_follow,
                           recent_searches=recent_searches,
                           trending_posts=trending_posts)


@bp.route('/api/search/autocomplete')
@login_required
def search_autocomplete():
    db  = get_db()
    uid = session['user_id']
    q   = request.args.get('q', '').strip()
    if len(q) < 1:
        return jsonify({'users': [], 'tags': []})
    like = f'{q}%'
    users = db.execute(
        'SELECT username, display_name, avatar_url, is_verified, verified_tier, follower_count '
        'FROM users WHERE (username LIKE ? OR display_name LIKE ?) AND id != ? '
        'ORDER BY follower_count DESC LIMIT 5', (like, like, uid)
    ).fetchall()
    tags = db.execute(
        'SELECT h.name, COUNT(ph.post_id) AS cnt FROM hashtags h '
        'JOIN post_hashtags ph ON ph.hashtag_id=h.id '
        'WHERE h.name LIKE ? GROUP BY h.id ORDER BY cnt DESC LIMIT 5', (like,)
    ).fetchall()
    return jsonify({'users': [dict(u) for u in users], 'tags': [dict(t) for t in tags]})


@bp.route('/api/trending/posts')
@login_required
def api_trending_posts():
    db     = get_db()
    uid    = session['user_id']
    window = request.args.get('window', '24h')
    hours  = {'6h': 6, '24h': 24, '48h': 48, '7d': 168}.get(window, 24)
    # Build cutoff time in Python instead of SQLite to avoid quote issues
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    cutoff = (_dt.now(_tz.utc) - _td(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
    rows   = db.execute(
        'SELECT p.* FROM posts p WHERE p.reply_to_id IS NULL '
        'AND p.created_at >= ? '
        'ORDER BY p.score DESC LIMIT 10', (cutoff,)
    ).fetchall()
    return jsonify([format_post(r, uid, db) for r in rows])


@bp.route('/api/trending/tags')
@login_required
def api_trending_tags():
    return jsonify(_trending_hashtags(get_db(), hours=48, limit=15))


@bp.route('/api/who-to-follow')
@login_required
def api_who_to_follow():
    db  = get_db()
    uid = session['user_id']
    recs = _who_to_follow(db, uid, limit=8)
    for u in recs:
        u['you_follow'] = bool(db.execute(
            'SELECT 1 FROM follows WHERE follower_id=? AND following_id=?', (uid, u['id'])
        ).fetchone())
    return jsonify(recs)


@bp.route('/api/post/<int:post_id>/view', methods=['POST'])
@login_required
@limiter.limit(LIMIT_POLL)
@csrf_exempt
def record_post_view(post_id):
    db  = get_db()
    uid = session['user_id']
    try:
        # Check if this user already viewed this post
        already = db.execute(
            'SELECT 1 FROM post_views WHERE post_id=? AND user_id=?', (post_id, uid)
        ).fetchone()
        if not already:
            # New view: record it and increment counter atomically
            db.execute('INSERT OR IGNORE INTO post_views (post_id, user_id) VALUES (?,?)',
                       (post_id, uid))
            db.execute('UPDATE posts SET view_count=view_count+1 WHERE id=?', (post_id,))
            # Recalculate score every 10th view to avoid per-request overhead
            new_count = db.execute(
                'SELECT view_count FROM posts WHERE id=?', (post_id,)
            ).fetchone()
            if new_count and new_count['view_count'] % 10 == 0:
                recalc_post_score(db, post_id)
            db.commit()
    except Exception:
        pass
    return jsonify({'ok': True})


@bp.route('/trending')
@login_required
def trending():
    db     = get_db()
    uid    = session['user_id']
    window = request.args.get('w', '24h')
    hours  = {'6h': 6, '24h': 24, '48h': 48, '7d': 168}.get(window, 24)

    top_posts = [format_post(r, uid, db) for r in db.execute(
        'SELECT p.* FROM posts p WHERE p.reply_to_id IS NULL '
        'AND p.created_at >= ? '
        'ORDER BY p.score DESC LIMIT 30', (hours,)
    ).fetchall()]
    top_tags      = _trending_hashtags(db, hours=hours, limit=20)
    who_to_follow = _who_to_follow(db, uid, limit=6)

    rising = [dict(r) for r in db.execute("""
        SELECT u.id, u.username, u.display_name, u.avatar_url,
               u.is_verified, u.follower_count, u.bio,
               COUNT(f.follower_id) AS new_followers
        FROM users u JOIN follows f ON f.following_id=u.id
        WHERE f.created_at >= ?
          AND u.id != ? AND u.id NOT IN (SELECT following_id FROM follows WHERE follower_id=?)
        GROUP BY u.id ORDER BY new_followers DESC LIMIT 5
    """, (hours, uid, uid)).fetchall()]

    return render_template('trending.html', top_posts=top_posts, top_tags=top_tags,
                           who_to_follow=who_to_follow, rising=rising, window=window)


@bp.route('/api/search/history/clear', methods=['POST'])
@login_required
def clear_search_history():
    db  = get_db()
    uid = session['user_id']
    db.execute('DELETE FROM search_history WHERE user_id=?', (uid,))
    db.commit()
    return jsonify({'success': True})


@bp.route('/tag/<tag>')
@login_required
def hashtag_feed(tag):
    db  = get_db()
    uid = session['user_id']
    tag = tag.lower().lstrip('#')
    ht  = db.execute('SELECT * FROM hashtags WHERE name=?', (tag,)).fetchone()
    if not ht:
        posts = []
    else:
        rows  = db.execute("""
            SELECT p.* FROM posts p JOIN post_hashtags ph ON ph.post_id=p.id
            WHERE ph.hashtag_id=? AND p.reply_to_id IS NULL ORDER BY p.created_at DESC LIMIT 40
        """, (ht['id'],)).fetchall()
        posts = [format_post(r, uid, db) for r in rows]

    trending_tags = db.execute("""
        SELECT h.name, COUNT(ph.post_id) as cnt FROM hashtags h
        JOIN post_hashtags ph ON ph.hashtag_id=h.id JOIN posts p ON p.id=ph.post_id
        WHERE p.created_at >= datetime('now', '-7 days')
        GROUP BY h.id ORDER BY cnt DESC LIMIT 10
    """).fetchall()
    return render_template('hashtag_feed.html', tag=tag, posts=posts,
                           trending_tags=[dict(t) for t in trending_tags])


# ── Polls ─────────────────────────────────────────────────────────────────────

@bp.route('/post/<int:post_id>/poll/vote', methods=['POST'])
@login_required
@limiter.limit(LIMIT_LIKE)
@csrf_exempt   # JSON POST
def poll_vote(post_id):
    db        = get_db()
    uid       = session['user_id']
    option_id = safe_int(request.form.get('option_id'), 0)

    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Post not found.'}), 404
    if (post['post_type'] if 'post_type' in post.keys() else 'post') != 'poll':
        return jsonify({'success': False, 'error': 'Not a poll.'}), 400

    exp = post['poll_expires_at'] if 'poll_expires_at' in post.keys() else None
    if exp:
        try:
            if datetime.fromisoformat(exp.replace('Z', '')) < datetime.now(timezone.utc):
                return jsonify({'success': False, 'error': 'This poll has ended.'}), 400
        except Exception:
            pass

    opt = db.execute('SELECT * FROM poll_options WHERE id=? AND post_id=?',
                     (option_id, post_id)).fetchone()
    if not opt:
        return jsonify({'success': False, 'error': 'Invalid option.'}), 400

    existing = db.execute('SELECT option_id FROM poll_votes WHERE post_id=? AND user_id=?',
                          (post_id, uid)).fetchone()
    if existing:
        db.execute('UPDATE poll_options SET votes=MAX(0, votes-1) WHERE id=?', (existing['option_id'],))
        db.execute('DELETE FROM poll_votes WHERE post_id=? AND user_id=?', (post_id, uid))

    db.execute('INSERT OR IGNORE INTO poll_votes (post_id,option_id,user_id) VALUES (?,?,?) ',
               (post_id, option_id, uid))
    db.execute('UPDATE poll_options SET votes=votes+1 WHERE id=?', (option_id,))
    db.commit()

    options = db.execute('SELECT * FROM poll_options WHERE post_id=? ORDER BY id', (post_id,)).fetchall()
    total   = sum(o['votes'] for o in options)
    result  = [{'id': o['id'], 'label': o['label'], 'votes': o['votes'],
                'pct': round(o['votes']*100/total) if total else 0} for o in options]
    return jsonify({'success': True, 'options': result, 'total': total, 'user_vote': option_id})


@bp.route('/post/<int:post_id>/poll/edit', methods=['POST'])
@login_required
@csrf_exempt
def poll_edit(post_id):
    db  = get_db()
    uid = session['user_id']
    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post or post['user_id'] != uid:
        return jsonify({'success': False, 'error': 'Not found or not authorized.'}), 404
    if (post['post_type'] if 'post_type' in post.keys() else 'post') != 'poll':
        return jsonify({'success': False, 'error': 'Not a poll.'}), 400

    _tv_row = db.execute('SELECT COALESCE(SUM(votes),0) FROM poll_options WHERE post_id=?',
                         (post_id,)).fetchone()
    total_votes = _tv_row[0] if _tv_row else 0
    if total_votes > 0:
        return jsonify({'success': False, 'error': 'Cannot edit a poll that already has votes.'}), 400

    data        = request.get_json(silent=True) or {}
    new_options = [str(o).strip() for o in (data.get('options') or []) if str(o).strip()][:10]
    if len(new_options) < 2:
        return jsonify({'success': False, 'error': 'A poll needs at least 2 options.'}), 400

    db.execute('DELETE FROM poll_options WHERE post_id=?', (post_id,))
    for label in new_options:
        db.execute('INSERT OR IGNORE INTO poll_options (post_id, label) VALUES (?,?)', (post_id, label))
    db.commit()

    options = db.execute('SELECT * FROM poll_options WHERE post_id=? ORDER BY id', (post_id,)).fetchall()
    return jsonify({'success': True, 'options': [{'id': o['id'], 'label': o['label']} for o in options]})


# ── Settings ─────────────────────────────────────────────────────────────────

@bp.route('/settings/saves', methods=['POST'])
@login_required
@csrf_exempt
def toggle_post_saves():
    db  = get_db()
    uid = session['user_id']
    data  = request.get_json(silent=True) or {}
    allow = 1 if data.get('allow', True) else 0
    db.execute('UPDATE users SET allow_post_saves=? WHERE id=?', (allow, uid))
    db.commit()
    return jsonify({'success': True, 'allow_post_saves': bool(allow)})


@bp.route('/api/online/heartbeat', methods=['POST'])
@login_required
@limiter.limit(LIMIT_HEARTBEAT)
@csrf_exempt   # JSON POST, high frequency
def online_heartbeat():
    db  = get_db()
    uid = session['user_id']
    now = datetime.now(timezone.utc).isoformat()
    db.execute('UPDATE users SET online_at=? WHERE id=?', (now, uid))
    db.commit()
    return jsonify({'ok': True})


@bp.route('/api/online/status', methods=['POST'])
@login_required
def toggle_online_status():
    db   = get_db()
    uid  = session['user_id']
    data = request.get_json(silent=True) or {}
    show = 1 if data.get('show', True) else 0
    db.execute('UPDATE users SET show_online=? WHERE id=?', (show, uid))
    db.commit()
    return jsonify({'show_online': bool(show)})


@bp.route('/api/online/check/<username>')
@login_required
def check_online(username):
    db  = get_db()
    udb   = get_user_db()
    row = db.execute('SELECT online_at, show_online FROM users WHERE username=?', (username,)).fetchone()
    if not row or not row['show_online'] or not row['online_at']:
        return jsonify({'online': False})
    try:
        last = datetime.fromisoformat(row['online_at'].replace('Z', ''))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return jsonify({'online': (datetime.now(timezone.utc) - last).total_seconds() < 90,
                        'last_seen': row['online_at'][:16]})
    except Exception:
        return jsonify({'online': False})


# ── Web Push subscriptions ────────────────────────────────────────────────────

@bp.route('/api/settings/notifications', methods=['GET', 'POST'])
@login_required
@csrf_exempt
def notification_settings():
    db  = get_db()
    uid = session['user_id']
    DEFAULTS = {'likes': True, 'follows': True, 'mentions': True,
                'dms': True, 'boosts': True, 'tips': True, 'system': True}
    if request.method == 'GET':
        row = db.execute('SELECT notif_prefs FROM users WHERE id=?', (uid,)).fetchone()
        try:
            prefs = json.loads((row['notif_prefs'] or '{}') if row else '{}')
        except Exception:
            prefs = {}
        return jsonify({**DEFAULTS, **prefs})

    data = request.get_json(silent=True) or {}
    # Only allow valid keys
    valid = {k: bool(data[k]) for k in DEFAULTS if k in data}
    db.execute('UPDATE users SET notif_prefs=? WHERE id=?', (json.dumps(valid), uid))
    db.commit()
    return jsonify({'success': True})


@bp.route('/api/push/subscribe', methods=['POST'])
@login_required
@csrf_exempt
def push_subscribe():
    db   = get_db()
    uid  = session['user_id']
    data = request.get_json(silent=True) or {}
    endpoint = (data.get('endpoint') or '').strip()
    if not endpoint:
        return jsonify({'success': False, 'error': 'Missing endpoint'}), 400
    import json as _json
    sub_json = _json.dumps(data)
    db.execute(
        'INSERT INTO push_subscriptions (user_id, endpoint, subscription_json) VALUES (?,?,?) '
        'ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, subscription_json=excluded.subscription_json',
        (uid, endpoint, sub_json)
    )
    db.commit()
    return jsonify({'success': True})


@bp.route('/api/push/unsubscribe', methods=['POST'])
@login_required
@csrf_exempt
def push_unsubscribe():
    db   = get_db()
    uid  = session['user_id']
    data = request.get_json(silent=True) or {}
    endpoint = (data.get('endpoint') or '').strip()
    if endpoint:
        db.execute('DELETE FROM push_subscriptions WHERE user_id=? AND endpoint=?', (uid, endpoint))
    else:
        db.execute('DELETE FROM push_subscriptions WHERE user_id=?', (uid,))
    db.commit()
    return jsonify({'success': True})


# ── Direct Messages ───────────────────────────────────────────────────────────

def _get_or_create_conversation(db, uid, other_id):
    """
    Get or create a conversation. Uses db for global fallback;
    uses the per-user DB (get_user_db) for the actual conversation record.
    """
    from helpers import get_user_db as _udb_fn
    udb = _udb_fn()
    a, b = min(uid, other_id), max(uid, other_id)
    conv = udb.execute('SELECT * FROM conversations WHERE user_a=? AND user_b=?', (a, b)).fetchone()
    if not conv:
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc).strftime('%Y-%m-%d %H:%M:%S')
        udb.execute(
            'INSERT OR IGNORE INTO conversations (user_a, user_b, last_msg_at) VALUES (?,?,?)',
            (a, b, now)
        )
        udb.commit()
        conv = udb.execute('SELECT * FROM conversations WHERE user_a=? AND user_b=?', (a, b)).fetchone()
    return conv





def _format_conversation(row, uid, db, udb=None):
    """Enrich a conversations row with the other user's profile info, last message and unread count."""
    try:
        d = dict(row)
        other_uid = d['user_b'] if d.get('user_a') == uid else d.get('user_a')
        other = db.execute('SELECT * FROM users WHERE id=?', (other_uid,)).fetchone()
        if not other:
            return None
        od = dict(other)
        od.setdefault('is_verified', 0)
        od.setdefault('verified_tier', 'blue')
        od.setdefault('avatar_url', None)
        od.setdefault('display_name', od.get('username', ''))
        d['other'] = od

        # Fetch last message and unread count from personal DB
        d['last_msg']  = None
        d['unread']    = 0
        if udb is not None:
            try:
                last = udb.execute(
                    'SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1',
                    (d['id'],)
                ).fetchone()
                if last:
                    ld = dict(last)
                    ld.setdefault('msg_type', 'text')
                    ld.setdefault('file_name', None)
                    ld.setdefault('body', None)
                    d['last_msg'] = ld
                unread_row = udb.execute(
                    'SELECT COUNT(*) FROM messages WHERE conversation_id=? AND sender_id!=? AND is_read=0',
                    (d['id'], uid)
                ).fetchone()
                d['unread'] = unread_row[0] if unread_row else 0
            except Exception:
                pass
        return d
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning('_format_conversation error: %s', _e)
        return None


@bp.route('/messages')
@login_required
def messages_inbox():
    db  = get_db()      # global
    udb = get_user_db() # personal (conversations, messages)
    uid = session['user_id']
    tab = request.args.get('tab', 'all').lower()
    if tab not in ('all', 'chats', 'groups'):
        tab = 'all'

    # ── Chats (1-to-1 DMs) ───────────────────────────────────────────────────
    chat_rows = udb.execute(
        'SELECT * FROM conversations WHERE user_a=? OR user_b=? '
        'ORDER BY last_msg_at DESC',
        (uid, uid)
    ).fetchall()
    chats = [c for c in [_format_conversation(r, uid, db, udb) for r in chat_rows] if c is not None]

    # ── Groups the user is a member of ───────────────────────────────────────
    group_rows = db.execute("""
        SELECT g.* FROM groups g
        JOIN group_members gm ON gm.group_id = g.id
        WHERE gm.user_id = ?
        ORDER BY g.created_at DESC
    """, (uid,)).fetchall()
    groups = [_format_group(r, uid, db) for r in group_rows]

    db.execute('UPDATE users SET unread_dm_count=0 WHERE id=?', (uid,))
    db.commit()

    return render_template('messages.html',
                           conversations=chats,
                           groups=groups,
                           tab=tab,
                           now_str=datetime.now(timezone.utc).isoformat()[:10])


@bp.route('/messages/<username>')
@login_required
def message_thread(username):
    db    = get_db()
    udb   = get_user_db()
    uid   = session['user_id']
    other = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not other:
        return render_template('error.html', code=404, message='User not found.'), 404
    if other['id'] == uid:
        return redirect(url_for('social.messages_inbox'))

    conv = _get_or_create_conversation(db, uid, other['id'])
    if not conv:
        return render_template('error.html', code=500,
                               message='Could not open conversation. Please try again.'), 500
    msgs = [dict(m) for m in udb.execute("""
        SELECT m.*, u.username as sender_username, u.avatar_url as sender_avatar
        FROM messages m JOIN users u ON (
            SELECT username FROM users WHERE id=m.sender_id LIMIT 1
        )=u.username
        WHERE m.conversation_id=? ORDER BY m.created_at ASC LIMIT 100
    """, (conv['id'],)).fetchall() if False] or []
    # Simpler join using global db for usernames:
    raw_msgs = udb.execute(
        'SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at ASC LIMIT 100',
        (conv['id'],)
    ).fetchall()
    msgs = []
    for m in raw_msgs:
        md = dict(m)
        sender = db.execute('SELECT username, avatar_url FROM users WHERE id=?',
                             (md['sender_id'],)).fetchone()
        md['sender_username'] = sender['username'] if sender else ''
        md['sender_avatar']   = sender['avatar_url'] if sender else None
        md.setdefault('edited_at', None)
        md.setdefault('reactions', None)
        md.setdefault('is_pinned', 0)
        md.setdefault('reply_to_id', None)
        md.setdefault('deleted_at', None)
        md.setdefault('view_once', 0)
        md.setdefault('view_once_opened', 0)
        md.setdefault('is_read', 1)
        md.setdefault('file_url', None)
        md.setdefault('file_mime', None)
        md.setdefault('file_name', None)
        msgs.append(md)

    udb.execute('UPDATE messages SET is_read=1 WHERE conversation_id=? AND sender_id!=?',
               (conv['id'], uid))
    db.execute('UPDATE users SET unread_dm_count=0 WHERE id=?', (uid,))
    db.commit()

    return render_template('message_thread.html', other=dict(other), messages=msgs,
                           conv_id=conv['id'])


@bp.route('/messages/<username>/send', methods=['POST'])
@login_required
@limiter.limit(LIMIT_DM)
@csrf_exempt   # JSON POST
def send_message(username):
    db  = get_db()
    udb   = get_user_db()
    uid = session['user_id']
    other = db.execute('SELECT id, username FROM users WHERE username=?', (username,)).fetchone()
    if not other or other['id'] == uid:
        return jsonify({'success': False, 'error': 'Invalid recipient.'}), 400

    ct = request.content_type or ''
    if 'application/json' in ct:
        _d        = request.get_json(silent=True) or {}
        body      = (_d.get('body') or '').strip() or None
        msg_type  = (_d.get('msg_type') or 'text').strip().lower()
        file_name = (_d.get('file_name') or '') or None
        file_mime = (_d.get('file_mime') or '') or None
        file_data = _d.get('file_data') or None
    else:
        body      = (request.form.get('body') or '').strip() or None
        msg_type  = (request.form.get('msg_type') or 'text').strip().lower()
        file_name = (request.form.get('file_name') or '') or None
        file_mime = (request.form.get('file_mime') or '') or None
        file_data = request.form.get('file_data') or None

    if msg_type not in ('text', 'image', 'file', 'voice', 'video'):
        msg_type = 'text'
    if msg_type == 'text' and not body:
        return jsonify({'success': False, 'error': 'Message cannot be empty.'}), 400
    if msg_type != 'text' and not file_data:
        return jsonify({'success': False, 'error': 'No file data received.'}), 400
    if body and len(body) > 2000:
        return jsonify({'success': False, 'error': 'Message too long (max 2000 chars).'}), 400

    conv = _get_or_create_conversation(db, uid, other['id'])
    if not conv:
        return jsonify({'success': False, 'error': 'Could not create conversation.'}), 500
    now  = datetime.now(timezone.utc).isoformat()

    # Upload file attachment to R2 if present
    file_url = None
    if file_data:
        try:
            file_url = storage.upload_message_file(conv['id'], file_data)
        except (ValueError, RuntimeError) as _e:
            return jsonify({'success': False, 'error': f'File upload failed: {_e}'}), 400

    # Messages and conversations are personal data → udb
    _msg_data = _d if 'application/json' in (request.content_type or '') else {}
    view_once = int(bool(_msg_data.get('view_once', 0)))
    udb.execute(
        'INSERT INTO messages '
        '(conversation_id,sender_id,body,msg_type,file_url,file_name,file_mime,view_once,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (conv['id'], uid, body, msg_type, file_url, file_name, file_mime, view_once, now)
    )
    msg_id = udb.lastrowid
    udb.execute('UPDATE conversations SET last_msg_at=? WHERE id=?', (now, conv['id']))
    udb.commit()
    # Global DB: non-critical side-effects — wrap so a DB lock doesn't kill the response
    # after the message is already durably saved in udb
    try:
        db.execute('UPDATE users SET unread_dm_count=unread_dm_count+1 WHERE id=?', (other['id'],))
        db.execute('UPDATE users SET online_at=? WHERE id=?', (now, uid))
        db.commit()
    except Exception:
        pass
    try:
        me = db.execute('SELECT username, avatar_url FROM users WHERE id=?', (uid,)).fetchone()
    except Exception:
        me = None
    return jsonify({'success': True, 'message': {
        'id': msg_id, 'body': body, 'msg_type': msg_type,
        'file_url': file_url, 'file_name': file_name, 'file_mime': file_mime,
        'view_once': view_once, 'view_once_opened': 0,
        'reply_to_id': _msg_data.get('reply_to_id') if 'application/json' in (request.content_type or '') else None,
        'sender_id': uid,
        'sender_username': me['username'] if me else '',
        'sender_avatar': me['avatar_url'] if me else None,
        'created_at': now, 'is_read': 0,
    }})


@bp.route('/api/messages/<username>/poll')
@login_required
def poll_messages(username):
    db    = get_db()
    udb   = get_user_db()
    uid   = session['user_id']
    after = request.args.get('after', 0, type=int)
    other = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if not other:
        return jsonify({'messages': []}), 404

    a, b = min(uid, other['id']), max(uid, other['id'])
    conv = udb.execute('SELECT id FROM conversations WHERE user_a=? AND user_b=?', (a, b)).fetchone()
    if not conv:
        return jsonify({'messages': []})

    # Messages live in the personal DB (udb); enrich sender info from global DB
    raw_rows = udb.execute(
        'SELECT * FROM messages WHERE conversation_id=? AND id > ? ORDER BY created_at ASC LIMIT 50',
        (conv['id'], after)
    ).fetchall()
    rows = []
    for _r in raw_rows:
        _rd = dict(_r)
        _sender = db.execute('SELECT username, avatar_url FROM users WHERE id=?',
                             (_rd['sender_id'],)).fetchone()
        _rd['sender_username'] = _sender['username'] if _sender else ''
        _rd['sender_avatar']   = _sender['avatar_url'] if _sender else None
        _rd.setdefault('file_url',  None)
        _rd.setdefault('file_name', None)
        _rd.setdefault('file_mime', None)
        _rd.setdefault('is_read',   1)
        rows.append(_rd)

    if rows:
        udb.execute('UPDATE messages SET is_read=1 WHERE conversation_id=? AND sender_id!=? AND id > ?',
                   (conv['id'], uid, after))
        udb.commit()
        try:
            _tu = udb.execute(
                'SELECT COUNT(*) FROM messages WHERE sender_id!=? AND is_read=0', (uid,)
            ).fetchone()
            db.execute('UPDATE users SET unread_dm_count=? WHERE id=?',
                       (_tu[0] if _tu else 0, uid))
            db.commit()
        except Exception:
            pass

    return jsonify({'messages': rows})


@bp.route('/api/messages/<int:conv_id>/read', methods=['POST'])
@login_required
@csrf_exempt
def mark_conversation_read(conv_id):
    """Mark all messages in a conversation as read by the current user."""
    udb = get_user_db()
    db  = get_db()
    uid = session['user_id']
    udb.execute(
        'UPDATE messages SET is_read=1 WHERE conversation_id=? AND sender_id!=?',
        (conv_id, uid)
    )
    udb.commit()
    # Recalculate total unread
    try:
        unread_rows = udb.execute(
            "SELECT COUNT(*) FROM messages m "
            "JOIN conversations c ON c.id=m.conversation_id "
            "WHERE m.sender_id!=? AND m.is_read=0", (uid,)
        ).fetchone()
        total_unread = unread_rows[0] if unread_rows else 0
        db.execute('UPDATE users SET unread_dm_count=? WHERE id=?', (total_unread, uid))
        db.commit()
    except Exception:
        pass
    return jsonify({'ok': True})


@bp.route('/api/messages/unread')
@login_required
def api_unread_dms():
    db  = get_db()
    uid = session['user_id']
    _dm_row = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    count = _dm_row
    return jsonify({'count': int((count['unread_dm_count'] or 0)) if count else 0})


@bp.route('/api/messages/<username>/typing', methods=['POST'])
@login_required
@limiter.limit(LIMIT_POLL)
@csrf_exempt
def set_typing(username):
    uid = session['user_id']
    now = datetime.now(timezone.utc).timestamp()
    _typing_state[(uid, username)] = now
    # Prune entries older than TTL to prevent unbounded memory growth
    stale = [k for k, ts in _typing_state.items() if now - ts > _TYPING_TTL]
    for k in stale:
        _typing_state.pop(k, None)
    return jsonify({'ok': True})


@bp.route('/api/messages/<username>/typing/stop', methods=['POST'])
@login_required
@csrf_exempt
def stop_typing(username):
    uid = session['user_id']
    _typing_state.pop((uid, username), None)
    return jsonify({'ok': True})


@bp.route('/api/messages/<username>/is-typing')
@login_required
def is_typing(username):
    db  = get_db()
    uid = session['user_id']
    other = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if not other:
        return jsonify({'typing': False})
    me_row = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
    key    = (other['id'], me_row['username'] if me_row else '')
    ts     = _typing_state.get(key, 0)
    return jsonify({'typing': (datetime.now(timezone.utc).timestamp() - ts) < 3})


@bp.route('/api/messages/edit/<int:msg_id>', methods=['POST'])
@login_required
def edit_message(msg_id):
    db  = get_db()
    udb   = get_user_db()
    uid = session['user_id']
    msg = udb.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not msg:
        return jsonify({'success': False, 'error': 'Message not found.'}), 404
    if msg['sender_id'] != uid:
        return jsonify({'success': False, 'error': 'You can only edit your own messages.'}), 403
    if (msg['msg_type'] if 'msg_type' in msg.keys() else 'text') != 'text':
        return jsonify({'success': False, 'error': 'Only text messages can be edited.'}), 400

    data = request.get_json(silent=True) or {}
    body = (data.get('body') or '').strip()
    if not body or len(body) > 2000:
        return jsonify({'success': False, 'error': 'Invalid message body.'}), 400

    now = datetime.now(timezone.utc).isoformat()
    udb.execute('UPDATE messages SET body=?, edited_at=? WHERE id=?', (body, now, msg_id))
    udb.commit()
    return jsonify({'success': True, 'body': body, 'edited_at': now})


@bp.route('/api/messages/delete/<int:msg_id>', methods=['POST'])
@login_required
def delete_message(msg_id):
    db  = get_db()
    udb   = get_user_db()
    uid = session['user_id']
    msg = udb.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not msg or msg['sender_id'] != uid:
        return jsonify({'success': False, 'error': 'Not found or not authorized.'}), 404
    now = datetime.now(timezone.utc).isoformat()
    udb.execute("UPDATE messages SET body='(deleted)',msg_type='text',file_url=NULL,"
               "file_name=NULL,file_mime=NULL,deleted_at=? WHERE id=?", (now, msg_id))
    udb.commit()
    return jsonify({'success': True})


@bp.route('/api/messages/react/<int:msg_id>', methods=['POST'])
@login_required
def react_message(msg_id):
    db    = get_db()
    udb   = get_user_db()
    uid   = session['user_id']
    data  = request.get_json(silent=True) or {}
    emoji = (data.get('emoji') or '').strip()
    if not emoji or len(emoji) > 8:
        return jsonify({'success': False, 'error': 'Invalid emoji.'}), 400

    msg = udb.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not msg:
        return jsonify({'success': False, 'error': 'Message not found.'}), 404

    conv = udb.execute('SELECT * FROM conversations WHERE id=?', (msg['conversation_id'],)).fetchone()
    if not conv or (conv['user_a'] != uid and conv['user_b'] != uid):
        return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    try:
        reactions = json.loads(msg['reactions']) if msg['reactions'] else {}
    except Exception:
        reactions = {}

    users = reactions.get(emoji, [])
    if uid in users:
        users.remove(uid)
    else:
        users.append(uid)
    if users:
        reactions[emoji] = users
    else:
        reactions.pop(emoji, None)

    udb.execute('UPDATE messages SET reactions=? WHERE id=?', (json.dumps(reactions), msg_id))
    udb.commit()
    return jsonify({'success': True, 'reactions': reactions})


@bp.route('/api/messages/pin/<int:msg_id>', methods=['POST'])
@login_required
def pin_message(msg_id):
    db  = get_db()
    udb   = get_user_db()
    uid = session['user_id']
    msg = udb.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not msg:
        return jsonify({'success': False, 'error': 'Message not found.'}), 404
    conv = udb.execute('SELECT * FROM conversations WHERE id=?', (msg['conversation_id'],)).fetchone()
    if not conv or (conv['user_a'] != uid and conv['user_b'] != uid):
        return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    new_state = 0 if (msg['is_pinned'] if 'is_pinned' in msg.keys() else 0) else 1
    udb.execute('UPDATE messages SET is_pinned=? WHERE id=?', (new_state, msg_id))
    udb.commit()
    return jsonify({'success': True, 'pinned': bool(new_state)})


@bp.route('/api/messages/info/<int:msg_id>')
@login_required
def message_info(msg_id):
    db  = get_db()
    udb   = get_user_db()
    uid = session['user_id']
    msg = udb.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not msg:
        return jsonify({'success': False}), 404
    # Enrich with sender info from global DB
    sender = db.execute('SELECT username, display_name FROM users WHERE id=?',
                        (msg['sender_id'],)).fetchone()
    conv = udb.execute('SELECT * FROM conversations WHERE id=?', (msg['conversation_id'],)).fetchone()
    if not conv or (conv['user_a'] != uid and conv['user_b'] != uid):
        return jsonify({'success': False}), 403
    keys = msg.keys()
    return jsonify({'success': True,
                    'sender':   sender['username'] if sender else '',
                    'sent_at':  msg['created_at'],
                    'is_read':  bool(msg['is_read']),
                    'edited_at': msg['edited_at'] if 'edited_at' in keys else None,
                    'msg_type':  msg['msg_type']  if 'msg_type'  in keys else 'text',
                    'pinned':    bool(msg['is_pinned']) if 'is_pinned' in keys else False})


@bp.route('/api/messages/forward', methods=['POST'])
@login_required
def forward_message():
    db         = get_db()
    udb   = get_user_db()
    uid        = session['user_id']
    data       = request.get_json(silent=True) or {}
    msg_id     = data.get('msg_id')
    recipients = data.get('recipients') or []
    if not msg_id or not recipients:
        return jsonify({'success': False, 'error': 'Missing data.'}), 400

    src      = udb.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not src:
        return jsonify({'success': False, 'error': 'Source message not found.'}), 404
    src_conv = udb.execute('SELECT * FROM conversations WHERE id=?', (src['conversation_id'],)).fetchone()
    if not src_conv or (src_conv['user_a'] != uid and src_conv['user_b'] != uid):
        return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    keys      = src.keys()
    body      = src['body']
    msg_type  = src['msg_type']  if 'msg_type'  in keys else 'text'
    file_data = src['file_url'] if 'file_url' in keys else None  # B2 URL
    file_name = src['file_name'] if 'file_name' in keys else None
    file_mime = src['file_mime'] if 'file_mime' in keys else None

    sent = 0
    now  = datetime.now(timezone.utc).isoformat()
    for username in recipients[:10]:
        u = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if not u or u['id'] == uid:
            continue
        conv = _get_or_create_conversation(db, uid, u['id'])
        udb.execute('INSERT INTO messages (conversation_id,sender_id,body,msg_type,'
                   'file_url,file_name,file_mime,created_at) VALUES (?,?,?,?,?,?,?,?)',
                   (conv['id'], uid, body, msg_type, file_data, file_name, file_mime, now))
        udb.execute('UPDATE conversations SET last_msg_at=? WHERE id=?', (now, conv['id']))
        db.execute('UPDATE users SET unread_dm_count=unread_dm_count+1 WHERE id=?', (u['id'],))
        sent += 1
    udb.commit()
    db.commit()
    return jsonify({'success': True, 'sent': sent})


@bp.route('/api/users/search')
@login_required
def search_users_for_dm():
    db  = get_db()
    uid = session['user_id']
    q   = (request.args.get('q') or '').strip()
    if len(q) < 1:
        return jsonify({'users': []})
    like = f'%{q}%'
    rows = db.execute(
        'SELECT username,display_name,avatar_url,is_verified,verified_tier,follower_count '
        'FROM users WHERE (username LIKE ? OR display_name LIKE ?) AND id != ? '
        'ORDER BY follower_count DESC LIMIT 10', (like, like, uid)
    ).fetchall()
    return jsonify({'users': [dict(u) for u in rows]})


# ── Channels ──────────────────────────────────────────────────────────────────

def _format_channel(ch, uid, db):
    row = dict(ch)
    row['is_member'] = bool(db.execute(
        'SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',
        (ch['id'], uid)
    ).fetchone())
    row['is_owner'] = ch['owner_id'] == uid
    return row


@bp.route('/channels')
@login_required
def channels_browse():
    db  = get_db()
    uid = session['user_id']
    q   = (request.args.get('q') or '').strip()
    tab = request.args.get('tab', 'discover')

    if tab == 'joined':
        rows = db.execute(
            'SELECT c.* FROM channels c JOIN channel_members cm ON cm.channel_id=c.id '
            'WHERE cm.user_id=? ORDER BY c.member_count DESC, c.created_at DESC LIMIT 40',
            (uid,)
        ).fetchall()
    elif tab == 'owned':
        rows = db.execute(
            'SELECT * FROM channels WHERE owner_id=? ORDER BY created_at DESC LIMIT 40',
            (uid,)
        ).fetchall()
    else:
        if q:
            rows = db.execute(
                'SELECT * FROM channels WHERE name LIKE ? OR description LIKE ? '
                'ORDER BY member_count DESC LIMIT 30',
                (f'%{q}%', f'%{q}%')
            ).fetchall()
        else:
            rows = db.execute(
                'SELECT * FROM channels ORDER BY member_count DESC, created_at DESC LIMIT 40'
            ).fetchall()

    return render_template('channels.html',
                           channels=[_format_channel(r, uid, db) for r in rows],
                           tab=tab, q=q)


@bp.route('/channel/create', methods=['GET', 'POST'])
@login_required
def channel_create():
    db  = get_db()
    uid = session['user_id']
    if request.method == 'POST':
        name        = (request.form.get('name') or '').strip()[:60]
        description = (request.form.get('description') or '').strip()[:300]
        is_public   = 1 if request.form.get('is_public', '1') != '0' else 0
        if not name:
            return jsonify({'success': False, 'error': 'Channel name is required.'}), 400

        slug = re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')
        slug = re.sub(r'-+', '-', slug)[:50] or f'channel-{uid}'
        base_slug = slug
        for i in range(1, 10):
            if not db.execute('SELECT 1 FROM channels WHERE slug=?', (slug,)).fetchone():
                break
            slug = f'{base_slug}-{i}'
        # Find a unique slug — append timestamp suffix if needed
        existing = db.execute('SELECT 1 FROM channels WHERE slug=?', (slug,)).fetchone()
        if existing:
            import time as _t
            slug = f'{base_slug}-{int(_t.time()) % 100000}'
        try:
            cur = db.execute(
                'INSERT INTO channels (name,slug,description,owner_id,is_public,member_count) '
                'VALUES (?,?,?,?,?,1)',
                (name, slug, description or None, uid, is_public)
            )
            ch_id = cur.lastrowid
            if not ch_id:
                return jsonify({'success': False, 'error': 'Could not create channel.'}), 500
            db.execute(
                'INSERT OR IGNORE INTO channel_members (channel_id,user_id,role) '
                'VALUES (?,?,?)',
                (ch_id, uid, 'owner')
            )
            db.commit()
            return jsonify({'success': True,
                           'redirect': url_for('social.channel_detail', slug=slug)})
        except Exception as _e:
            import logging as _log
            _log.getLogger(__name__).error('Channel create error: %s', _e)
            return jsonify({'success': False,
                           'error': f'Could not create channel: {str(_e)[:80]}'}), 400
    return render_template('channel_create.html')


@bp.route('/channel/<slug>')
@login_required
def channel_detail(slug):
    db  = get_db()
    uid = session['user_id']
    ch  = db.execute('SELECT * FROM channels WHERE slug=?', (slug,)).fetchone()
    if not ch:
        return render_template('error.html', code=404, message='Channel not found.'), 404

    member_row = db.execute('SELECT role FROM channel_members WHERE channel_id=? AND user_id=?',
                            (ch['id'], uid)).fetchone()
    is_member  = bool(member_row)
    user_role  = member_row['role'] if member_row else None
    can_post   = user_role in ('owner', 'admin', 'mod')

    if not ch['is_public'] and not is_member:
        return render_template('error.html', code=403, message='This channel is private.'), 403

    post_rows = db.execute('SELECT p.* FROM posts p JOIN channel_posts cp ON cp.post_id=p.id '
                           'WHERE cp.channel_id=? ORDER BY p.created_at DESC LIMIT 40',
                           (ch['id'],)).fetchall()
    posts   = [format_post_with_poll(r, uid, db) for r in post_rows]
    members = [dict(m) for m in db.execute("""
        SELECT u.username, u.display_name, u.avatar_url, u.is_verified, cm.role
        FROM channel_members cm JOIN users u ON u.id=cm.user_id WHERE cm.channel_id=?
        ORDER BY CASE cm.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 WHEN 'mod' THEN 2 ELSE 3 END, cm.joined_at
        LIMIT 40
    """, (ch['id'],)).fetchall()]

    return render_template('channel_detail.html', ch=dict(ch), posts=posts, members=members,
                           is_member=is_member, is_owner=ch['owner_id']==uid,
                           can_post=can_post, user_role=user_role)


@bp.route('/channel/<slug>/join', methods=['POST'])
@login_required
@csrf_exempt
def channel_join(slug):
    db  = get_db()
    uid = session['user_id']
    ch  = db.execute('SELECT * FROM channels WHERE slug=?', (slug,)).fetchone()
    if not ch:
        return jsonify({'success': False, 'error': 'Channel not found.'}), 404
    if db.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',
                  (ch['id'], uid)).fetchone():
        return jsonify({'success': False, 'error': 'Already a member.'}), 400
    db.execute('INSERT OR IGNORE INTO channel_members (channel_id,user_id,role) VALUES (?,?,?) ',
               (ch['id'], uid, 'member'))
    db.execute('UPDATE channels SET member_count=member_count+1 WHERE id=?', (ch['id'],))
    db.commit()
    _mc_row = db.execute('SELECT member_count FROM channels WHERE id=?', (ch['id'],)).fetchone()
    return jsonify({'success': True, 'member_count': _mc_row[0] if _mc_row else 0})


@bp.route('/channel/<slug>/leave', methods=['POST'])
@login_required
@csrf_exempt
def channel_leave(slug):
    db  = get_db()
    uid = session['user_id']
    ch  = db.execute('SELECT * FROM channels WHERE slug=?', (slug,)).fetchone()
    if not ch:
        return jsonify({'success': False, 'error': 'Channel not found.'}), 404
    if ch['owner_id'] == uid:
        return jsonify({'success': False, 'error': 'Owner cannot leave.'}), 400
    db.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?', (ch['id'], uid))
    db.execute('UPDATE channels SET member_count=MAX(0, member_count-1) WHERE id=?', (ch['id'],))
    db.commit()
    return jsonify({'success': True})


@bp.route('/channel/<slug>/edit', methods=['POST'])
@login_required
@csrf_exempt
def channel_edit(slug):
    """Owner can update channel name, description and avatar."""
    import storage as _st
    db  = get_db()
    uid = session['user_id']
    ch  = db.execute('SELECT * FROM channels WHERE slug=?', (slug,)).fetchone()
    if not ch or ch['owner_id'] != uid:
        return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    name        = (request.form.get('name') or '').strip()[:60]
    description = (request.form.get('description') or '').strip()[:300]
    avatar_data = (request.form.get('avatar_data') or '').strip() or None

    if not name:
        return jsonify({'success': False, 'error': 'Channel name is required.'}), 400

    avatar_url = ch.get('avatar_url')
    if avatar_data:
        try:
            new_url = _st.upload_data_uri(avatar_data, f'channels/{ch["id"]}')
            if avatar_url and avatar_url.startswith('http'):
                try: _st.delete_object(avatar_url)
                except Exception: pass
            avatar_url = new_url
        except (ValueError, RuntimeError) as e:
            return jsonify({'success': False, 'error': str(e)}), 400

    db.execute(
        'UPDATE channels SET name=?, description=?, avatar_url=? WHERE id=?',
        (name, description or None, avatar_url, ch['id'])
    )
    db.commit()
    return jsonify({'success': True, 'name': name, 'description': description,
                    'avatar_url': avatar_url})


@bp.route('/channel/<slug>/promote', methods=['POST'])
@login_required
@csrf_exempt
def channel_promote(slug):
    db  = get_db()
    uid = session['user_id']
    ch  = db.execute('SELECT * FROM channels WHERE slug=?', (slug,)).fetchone()
    if not ch or ch['owner_id'] != uid:
        return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    data     = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    new_role = (data.get('role') or 'member').strip()
    if new_role not in ('admin', 'mod', 'member'):
        return jsonify({'success': False, 'error': 'Invalid role.'}), 400

    target = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if not target or target['id'] == uid:
        return jsonify({'success': False, 'error': 'User not found or invalid.'}), 404
    if not db.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',
                      (ch['id'], target['id'])).fetchone():
        return jsonify({'success': False, 'error': 'User is not a member.'}), 400

    db.execute('UPDATE channel_members SET role=? WHERE channel_id=? AND user_id=?',
               (new_role, ch['id'], target['id']))
    db.commit()
    return jsonify({'success': True, 'username': username, 'role': new_role})


# ── Groups ────────────────────────────────────────────────────────────────────

def _format_group(row, uid, db):
    g      = dict(row)
    member = db.execute('SELECT role FROM group_members WHERE group_id=? AND user_id=?',
                        (row['id'], uid)).fetchone()
    g['is_member'] = bool(member)
    g['user_role'] = member['role'] if member else None
    g['is_owner']  = row['owner_id'] == uid
    last = db.execute('SELECT gm.*, u.username as sender_name FROM group_messages gm '
                      'JOIN users u ON u.id=gm.sender_id '
                      'WHERE gm.group_id=? ORDER BY gm.created_at DESC LIMIT 1', (row['id'],)).fetchone()
    g['last_msg'] = dict(last) if last else None
    try:
        unread_row = db.execute(
            "SELECT COUNT(*) FROM group_messages WHERE group_id=? AND sender_id!=? "
            "AND created_at > COALESCE((SELECT last_read_at FROM group_members "
            "WHERE group_id=? AND user_id=?), '1970-01-01')",
            (row['id'], uid, row['id'], uid)
        ).fetchone()
        g['unread'] = unread_row[0] if unread_row else 0
    except Exception:
        g['unread'] = 0
    return g


@bp.route('/groups')
@login_required
def groups_list():
    db  = get_db()
    uid = session['user_id']
    tab = request.args.get('tab', 'my')

    if tab == 'discover':
        rows = db.execute("""
            SELECT g.* FROM groups g
            WHERE g.is_public=1 AND g.id NOT IN (SELECT group_id FROM group_members WHERE user_id=?)
            ORDER BY g.member_count DESC, g.created_at DESC LIMIT 40
        """, (uid,)).fetchall()
    else:
        rows = db.execute("""
            SELECT g.* FROM groups g JOIN group_members gm ON gm.group_id=g.id
            WHERE gm.user_id=? ORDER BY g.created_at DESC LIMIT 40
        """, (uid,)).fetchall()

    return render_template('groups.html', groups=[_format_group(r, uid, db) for r in rows], tab=tab)


@bp.route('/group/create', methods=['GET', 'POST'])
@login_required
def group_create():
    db  = get_db()
    uid = session['user_id']
    if request.method == 'POST':
        name        = (request.form.get('name') or '').strip()[:60]
        description = (request.form.get('description') or '').strip()[:300]
        is_public   = 1 if request.form.get('is_public', '1') != '0' else 0
        if not name:
            return jsonify({'success': False, 'error': 'Group name required.'}), 400

        slug = re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')
        slug = re.sub(r'-+', '-', slug)[:50] or f'group-{uid}'
        base = slug
        for i in range(1, 20):
            if not db.execute('SELECT 1 FROM groups WHERE slug=?', (slug,)).fetchone():
                break
            slug = f'{base}-{i}'
        existing = db.execute('SELECT 1 FROM groups WHERE slug=?', (slug,)).fetchone()
        if existing:
            import time as _t
            slug = f'{base}-{int(_t.time()) % 100000}'
        try:
            cur = db.execute(
                'INSERT INTO groups (name,slug,description,owner_id,is_public,member_count) '
                'VALUES (?,?,?,?,?,1)',
                (name, slug, description or None, uid, is_public)
            )
            gid = cur.lastrowid
            if not gid:
                return jsonify({'success': False, 'error': 'Could not create group.'}), 500
            db.execute(
                'INSERT OR IGNORE INTO group_members (group_id,user_id,role) VALUES (?,?,?)',
                (gid, uid, 'owner')
            )
            db.commit()
            return jsonify({'success': True,
                           'redirect': url_for('social.group_detail', slug=slug)})
        except Exception as _e:
            import logging as _log
            _log.getLogger(__name__).error('Group create error: %s', _e)
            return jsonify({'success': False,
                           'error': f'Could not create group: {str(_e)[:80]}'}), 400
    return render_template('group_create.html')


@bp.route('/group/<slug>')
@login_required
def group_detail(slug):
    db  = get_db()
    uid = session['user_id']
    g   = db.execute('SELECT * FROM groups WHERE slug=?', (slug,)).fetchone()
    if not g:
        return render_template('error.html', code=404, message='Group not found.'), 404

    member    = db.execute('SELECT role FROM group_members WHERE group_id=? AND user_id=?',
                           (g['id'], uid)).fetchone()
    is_member = bool(member)
    user_role = member['role'] if member else None

    if not g['is_public'] and not is_member:
        return render_template('error.html', code=403, message='This group is private.'), 403

    if is_member:
        now = datetime.now(timezone.utc).isoformat()
        db.execute('UPDATE group_members SET last_read_at=? WHERE group_id=? AND user_id=?',
                   (now, g['id'], uid))
        db.execute('UPDATE users SET unread_group_count=('
                   'SELECT COUNT(DISTINCT gm2.group_id) FROM group_messages gm2 '
                   'JOIN group_members gmp ON gmp.group_id=gm2.group_id AND gmp.user_id=? '
                   'WHERE gm2.sender_id!=? AND gm2.created_at > COALESCE(gmp.last_read_at,\'1970-01-01\')'
                   ') WHERE id=?', (uid, uid, uid))
        db.commit()

    msgs = [dict(m) for m in db.execute("""
        SELECT gm.*, u.username as sender_username, u.display_name as sender_display,
               u.avatar_url as sender_avatar
        FROM group_messages gm JOIN users u ON u.id=gm.sender_id
        WHERE gm.group_id=? AND gm.deleted_at IS NULL ORDER BY gm.created_at ASC LIMIT 100
    """, (g['id'],)).fetchall()]
    members = [dict(m) for m in db.execute("""
        SELECT u.username, u.display_name, u.avatar_url,
               u.is_verified, u.verified_tier, gm.role, gm.joined_at
        FROM group_members gm JOIN users u ON u.id=gm.user_id WHERE gm.group_id=?
        ORDER BY CASE gm.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 WHEN 'mod' THEN 2 ELSE 3 END, gm.joined_at
        LIMIT 50
    """, (g['id'],)).fetchall()]

    return render_template('group_detail.html', g=dict(g), messages=msgs, members=members,
                           is_member=is_member, is_owner=g['owner_id']==uid, user_role=user_role)


@bp.route('/group/<slug>/send', methods=['POST'])
@login_required
@limiter.limit(LIMIT_DM)
@csrf_exempt   # JSON POST
def group_send(slug):
    db  = get_db()
    uid = session['user_id']
    g   = db.execute('SELECT * FROM groups WHERE slug=?', (slug,)).fetchone()
    if not g:
        return jsonify({'success': False, 'error': 'Group not found.'}), 404
    if not db.execute('SELECT 1 FROM group_members WHERE group_id=? AND user_id=?',
                      (g['id'], uid)).fetchone():
        return jsonify({'success': False, 'error': 'You are not a member.'}), 403

    ct = request.content_type or ''
    if 'application/json' in ct:
        _d = request.get_json(silent=True) or {}
        body      = (_d.get('body') or '').strip() or None
        msg_type  = (_d.get('msg_type') or 'text').lower()
        file_data = _d.get('file_data') or None
        file_name = (_d.get('file_name') or '') or None
        file_mime = (_d.get('file_mime') or '') or None
        reply_to  = _d.get('reply_to_id') or None
    else:
        body      = (request.form.get('body') or '').strip() or None
        msg_type  = (request.form.get('msg_type') or 'text').lower()
        file_data = request.form.get('file_data') or None
        file_name = (request.form.get('file_name') or '') or None
        file_mime = (request.form.get('file_mime') or '') or None
        reply_to  = safe_int(request.form.get('reply_to_id'), 0) or None

    if msg_type == 'text' and not body:
        return jsonify({'success': False, 'error': 'Message cannot be empty.'}), 400
    if msg_type != 'text' and not file_data:
        return jsonify({'success': False, 'error': 'No file data.'}), 400

    view_once = int(bool(_d.get('view_once', 0) if 'application/json' in (request.content_type or '') else request.form.get('view_once', 0)))

    now = datetime.now(timezone.utc).isoformat()

    # Upload file attachment to B2 if present
    file_url = None
    if file_data:
        try:
            file_url = storage.upload_group_file(g['id'], file_data)
        except (ValueError, RuntimeError) as _e:
            return jsonify({'success': False, 'error': f'File upload failed: {_e}'}), 400

    _cur = db.execute(
        'INSERT INTO group_messages '
        '(group_id,sender_id,body,msg_type,file_url,file_name,file_mime,reply_to_id,view_once,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (g['id'], uid, body, msg_type, file_url, file_name, file_mime, reply_to, view_once, now)
    )
    new_msg_id = _cur.lastrowid
    db.execute('UPDATE users SET unread_group_count=unread_group_count+1 WHERE id IN '
               '(SELECT user_id FROM group_members WHERE group_id=? AND user_id!=?)', (g['id'], uid))
    db.commit()
    try:
        me = db.execute('SELECT username, avatar_url, display_name FROM users WHERE id=?', (uid,)).fetchone()
    except Exception:
        me = None

    return jsonify({'success': True, 'message': {
        'id': new_msg_id, 'body': body, 'msg_type': msg_type,
        'file_url': file_url, 'file_name': file_name, 'file_mime': file_mime,
        'view_once': view_once,
        'sender_id': uid,
        'sender_username': me['username'] if me else '',
        'sender_display': me['display_name'] if me else '',
        'sender_avatar': me['avatar_url'] if me else None,
        'reply_to_id': reply_to, 'created_at': now,
    }})


@bp.route('/api/group/<slug>/poll')
@login_required
def group_poll_messages(slug):
    db    = get_db()
    uid   = session['user_id']
    after = request.args.get('after', 0, type=int)
    g     = db.execute('SELECT * FROM groups WHERE slug=?', (slug,)).fetchone()
    if not g:
        return jsonify({'messages': []}), 404
    if not db.execute('SELECT 1 FROM group_members WHERE group_id=? AND user_id=?',
                      (g['id'], uid)).fetchone():
        return jsonify({'messages': []}), 403

    rows = db.execute("""
        SELECT gm.*, u.username as sender_username, u.display_name as sender_display,
               u.avatar_url as sender_avatar
        FROM group_messages gm JOIN users u ON u.id=gm.sender_id
        WHERE gm.group_id=? AND gm.id > ? AND gm.deleted_at IS NULL
        ORDER BY gm.created_at ASC LIMIT 50
    """, (g['id'], after)).fetchall()

    if rows:
        now = datetime.now(timezone.utc).isoformat()
        db.execute('UPDATE group_members SET last_read_at=? WHERE group_id=? AND user_id=?',
                   (now, g['id'], uid))
        db.commit()

    return jsonify({'messages': [dict(r) for r in rows]})


@bp.route('/group/<slug>/edit', methods=['POST'])
@login_required
@csrf_exempt
def group_edit(slug):
    """Owner can update group name, description and avatar."""
    import storage as _st
    db  = get_db()
    uid = session['user_id']
    g   = db.execute('SELECT * FROM groups WHERE slug=?', (slug,)).fetchone()
    if not g or g['owner_id'] != uid:
        return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    name        = (request.form.get('name') or '').strip()[:60]
    description = (request.form.get('description') or '').strip()[:300]
    avatar_data = (request.form.get('avatar_data') or '').strip() or None

    if not name:
        return jsonify({'success': False, 'error': 'Group name is required.'}), 400

    avatar_url = g.get('avatar_url')
    if avatar_data:
        try:
            new_url = _st.upload_data_uri(avatar_data, f'groups/{g["id"]}')
            if avatar_url and avatar_url.startswith('http'):
                try: _st.delete_object(avatar_url)
                except Exception: pass
            avatar_url = new_url
        except (ValueError, RuntimeError) as e:
            return jsonify({'success': False, 'error': str(e)}), 400

    db.execute(
        'UPDATE groups SET name=?, description=?, avatar_url=? WHERE id=?',
        (name, description or None, avatar_url, g['id'])
    )
    db.commit()
    return jsonify({'success': True, 'name': name, 'description': description,
                    'avatar_url': avatar_url})


@bp.route('/group/<slug>/join', methods=['POST'])
@login_required
@csrf_exempt
def group_join(slug):
    db  = get_db()
    uid = session['user_id']
    g   = db.execute('SELECT * FROM groups WHERE slug=?', (slug,)).fetchone()
    if not g:
        return jsonify({'success': False, 'error': 'Group not found.'}), 404
    if not g['is_public']:
        return jsonify({'success': False, 'error': 'This group is private.'}), 403
    if db.execute('SELECT 1 FROM group_members WHERE group_id=? AND user_id=?',
                  (g['id'], uid)).fetchone():
        return jsonify({'success': False, 'error': 'Already a member.'}), 400
    db.execute('INSERT INTO group_members (group_id,user_id,role) VALUES (?,?,?) ', (g['id'], uid, 'member'))
    db.execute('UPDATE groups SET member_count=member_count+1 WHERE id=?', (g['id'],))
    db.commit()
    return jsonify({'success': True})


@bp.route('/group/<slug>/leave', methods=['POST'])
@login_required
@csrf_exempt
def group_leave(slug):
    db  = get_db()
    uid = session['user_id']
    g   = db.execute('SELECT * FROM groups WHERE slug=?', (slug,)).fetchone()
    if not g:
        return jsonify({'success': False, 'error': 'Not found.'}), 404
    if g['owner_id'] == uid:
        return jsonify({'success': False, 'error': 'Owner cannot leave.'}), 400
    db.execute('DELETE FROM group_members WHERE group_id=? AND user_id=?', (g['id'], uid))
    db.execute('UPDATE groups SET member_count=MAX(0, member_count-1) WHERE id=?', (g['id'],))
    db.commit()
    return jsonify({'success': True})


@bp.route('/api/groups/unread')
@login_required
def api_group_unread():
    db  = get_db()
    uid = session['user_id']
    row = db.execute('SELECT unread_group_count FROM users WHERE id=?', (uid,)).fetchone()
    return jsonify({'count': int(row['unread_group_count'] or 0) if row else 0})


@bp.route('/messages/<int:msg_id>/view-once-open', methods=['POST'])
@login_required
@csrf_exempt
def view_once_open(msg_id):
    """Mark view-once message as opened, wipe file from R2 and DB."""
    import storage as _st
    udb = get_user_db()
    uid = session['user_id']
    msg = udb.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not msg: return jsonify({'success': False, 'error': 'Not found.'}), 404
    if msg['sender_id'] == uid:
        return jsonify({'success': False, 'error': 'Cannot open your own view-once.'}), 400
    if msg.get('view_once_opened'):
        return jsonify({'success': True, 'already_opened': True})
    if msg.get('file_url'):
        try: _st.delete_object(msg['file_url'])
        except Exception: pass
    udb.execute(
        'UPDATE messages SET view_once_opened=1, file_url=NULL WHERE id=?', (msg_id,)
    )
    udb.commit()
    return jsonify({'success': True})


# ── Group view-once ───────────────────────────────────────────────────────────

@bp.route('/api/group-message/<int:msg_id>/view-once-open', methods=['POST'])
@login_required
@csrf_exempt
def group_view_once_open(msg_id):
    """Mark group view-once message as opened and wipe its file from R2."""
    import storage as _st
    db  = get_db()
    uid = session['user_id']
    msg = db.execute('SELECT * FROM group_messages WHERE id=?', (msg_id,)).fetchone()
    if not msg:
        return jsonify({'success': False, 'error': 'Not found.'}), 404
    if msg['sender_id'] == uid:
        return jsonify({'success': False, 'error': 'Cannot open your own view-once.'}), 400
    if msg.get('view_once_opened'):
        return jsonify({'success': True, 'already_opened': True})
    if msg.get('file_url'):
        try:
            _st.delete_object(msg['file_url'])
        except Exception:
            pass
    db.execute('UPDATE group_messages SET view_once_opened=1, file_url=NULL WHERE id=?', (msg_id,))
    db.commit()
    return jsonify({'success': True})


# ── Post edit history ─────────────────────────────────────────────────────────

@bp.route('/api/post/<int:post_id>/edits')
@login_required
def post_edit_history(post_id):
    db = get_db()
    edits = db.execute(
        'SELECT body, edited_at FROM post_edits WHERE post_id=? ORDER BY edited_at DESC LIMIT 20',
        (post_id,)
    ).fetchall()
    return jsonify({'success': True, 'edits': [dict(e) for e in edits]})


# ── Message read receipts ─────────────────────────────────────────────────────

@bp.route('/api/messages/<int:conv_id>/read', methods=['POST'])
@login_required
@csrf_exempt
def mark_messages_read(conv_id):
    udb = get_user_db()
    uid = session['user_id']
    udb.execute(
        'UPDATE messages SET is_read=1 WHERE conversation_id=? AND sender_id!=?',
        (conv_id, uid)
    )
    udb.commit()
    return jsonify({'ok': True})


# ── Verification badge applications ──────────────────────────────────────────

@bp.route('/verify/apply', methods=['GET', 'POST'])
@login_required
def verify_apply():
    db  = get_db()
    uid = session['user_id']
    existing = db.execute('SELECT status FROM verification_requests WHERE user_id=?', (uid,)).fetchone()
    if request.method == 'POST':
        if existing and existing['status'] == 'pending':
            return jsonify({'success': False, 'error': 'Request already pending.'})
        reason = request.form.get('reason', '').strip()[:500]
        evidence_url = request.form.get('evidence_url', '').strip()[:500]
        if not reason:
            return jsonify({'success': False, 'error': 'Reason is required.'})
        if existing:
            db.execute(
                "UPDATE verification_requests SET reason=?,evidence_url=?,status=?,"
                "reviewed_by=NULL,reviewed_at=NULL,created_at=datetime('now') WHERE user_id=?",
                (reason, evidence_url, 'pending', uid)
            )
        else:
            db.execute(
                'INSERT INTO verification_requests (user_id,reason,evidence_url) VALUES (?,?,?)',
                (uid, reason, evidence_url)
            )
        db.commit()
        return jsonify({'success': True, 'message': 'Application submitted.'})
    user = db.execute('SELECT is_verified FROM users WHERE id=?', (uid,)).fetchone()
    return render_template('verify_apply.html', existing=dict(existing) if existing else None,
                           is_verified=bool(user and user['is_verified']))


# ── Group invite links ────────────────────────────────────────────────────────

@bp.route('/group/<slug>/invite/create', methods=['POST'])
@login_required
@csrf_exempt
def create_group_invite(slug):
    import secrets as _sec
    db  = get_db()
    uid = session['user_id']
    group = db.execute('SELECT id, owner_id FROM groups WHERE slug=?', (slug,)).fetchone()
    if not group:
        return jsonify({'success': False, 'error': 'Group not found.'})
    member = db.execute('SELECT role FROM group_members WHERE group_id=? AND user_id=?',
                        (group['id'], uid)).fetchone()
    if not (member and member['role'] in ('admin', 'owner')) and group['owner_id'] != uid:
        return jsonify({'success': False, 'error': 'Not authorized.'})
    token = _sec.token_urlsafe(12)
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    expires_at = (_dt.now(_tz.utc) + _td(days=7)).isoformat()
    db.execute('INSERT INTO group_invites (group_id,token,created_by,expires_at) VALUES (?,?,?,?)',
               (group['id'], token, uid, expires_at))
    db.commit()
    return jsonify({'success': True, 'token': token,
                    'link': request.host_url.rstrip('/') + '/join/' + token})


@bp.route('/join/<token>')
def join_group_by_invite(token):
    db  = get_db()
    uid = session.get('user_id')
    if not uid:
        from flask import redirect, url_for as _uf
        return redirect(_uf('auth.login'))
    invite = db.execute(
        "SELECT * FROM group_invites WHERE token=? AND "
        "(expires_at IS NULL OR expires_at > datetime('now')) AND uses < max_uses",
        (token,)
    ).fetchone()
    if not invite:
        return render_template('error.html', message='Invite link is invalid or expired.'), 404
    existing = db.execute('SELECT id FROM group_members WHERE group_id=? AND user_id=?',
                          (invite['group_id'], uid)).fetchone()
    if not existing:
        db.execute('INSERT INTO group_members (group_id, user_id, role) VALUES (?,?,?)',
                   (invite['group_id'], uid, 'member'))
        db.execute('UPDATE group_invites SET uses=uses+1 WHERE id=?', (invite['id'],))
        db.commit()
    group = db.execute('SELECT slug FROM groups WHERE id=?', (invite['group_id'],)).fetchone()
    from flask import redirect, url_for as _uf2
    if group:
        return redirect(_uf2('social.group_detail', slug=group['slug']))
    return redirect(_uf2('social.feed'))

