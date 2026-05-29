"""
helpers.py — shared utilities used across all blueprints.

Extracted from the monolithic app.py so each blueprint can import
from here instead of referencing a single huge file.
"""
import hashlib
import hmac
import secrets
import re
import math
from datetime import datetime, timezone
from functools import wraps

import markupsafe
from flask import g, session, redirect, url_for, jsonify, request

# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_db() -> 'sqlite3.Connection':
    """Return the global DB (social data — feeds, profiles, posts, channels)."""
    from db import get_db as _db_get
    return _db_get()


def get_user_db() -> 'sqlite3.Connection':
    """Return the personal DB (wallet, DMs, notifications) for the logged-in user."""
    from db import get_user_db as _udb_get
    return _udb_get()


# ─────────────────────────────────────────────────────────────────────────────
# Password hashing (salted PBKDF2-SHA256)
# ─────────────────────────────────────────────────────────────────────────────

PBKDF2_ITERATIONS = 120_000


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith('pbkdf2_sha256$'):
        try:
            _, iter_s, salt_hex, hash_hex = stored.split('$', 3)
            iters = int(iter_s)
            dk = hashlib.pbkdf2_hmac(
                'sha256', pw.encode('utf-8'), bytes.fromhex(salt_hex), iters
            )
            return hmac.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)


def maybe_upgrade_password_hash(db, user_id: int, plaintext: str, stored: str):
    if stored and not stored.startswith('pbkdf2_sha256$'):
        db.execute('UPDATE users SET password=? WHERE id=?',
                   (hash_password(plaintext), user_id))
        db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Decorators
# ─────────────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            # Non-GET requests are AJAX/fetch — return JSON so clients can handle gracefully
            # instead of silently following an HTML redirect and showing a JSON parse error
            if request.method != 'GET':
                return jsonify({'success': False, 'error': 'Session expired. Please log in again.', 'login_required': True}), 401
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        user = get_db().execute(
            'SELECT is_admin FROM users WHERE id=?', (session['user_id'],)
        ).fetchone()
        if not user or not user['is_admin']:
            return jsonify({'success': False, 'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────────────────────
# Notification / transaction helpers
# ─────────────────────────────────────────────────────────────────────────────

def add_notification(db, user_id, message, *, icon=None, link=None):
    """
    Insert a notification into the target user's personal DB (udb).
    notifications table lives in the per-user personal DB, NOT in global.db.
    db arg is kept for backward-compatibility but ignored for the INSERT.
    We open the target user's personal DB directly for the write.
    """
    # Check notification preferences before inserting
    try:
        import json as _json
        _pref_row = db.execute('SELECT notif_prefs FROM users WHERE id=?', (user_id,)).fetchone()
        if _pref_row and _pref_row['notif_prefs']:
            _prefs = _json.loads(_pref_row['notif_prefs'] or '{}')
            # Map icon to pref key
            _ICON_MAP = {'like': 'likes', 'follow': 'follows', 'mention': 'mentions',
                         'message': 'dms', 'boost': 'boosts', 'tip': 'tips'}
            _pref_key = _ICON_MAP.get(icon or '', 'system')
            if not _prefs.get(_pref_key, True):
                return  # user has disabled this notification type
    except Exception:
        pass

    # Auto-detect icon from message emoji if not given
    if icon is None:
        if message.startswith('❤️') or 'liked' in message.lower():       icon = 'like'
        elif message.startswith('💬') or 'replied' in message.lower():   icon = 'reply'
        elif message.startswith('👤') or 'followed' in message.lower(): icon = 'follow'
        elif message.startswith('🔁') or 'reposted' in message.lower(): icon = 'repost'
        elif message.startswith('📣') or 'boost' in message.lower():     icon = 'boost'
        elif message.startswith('📡') or 'channel' in message.lower():   icon = 'channel'
        elif message.startswith('💰') or 'tip' in message.lower():       icon = 'tip'
        elif message.startswith('💳') or 'wallet' in message.lower():   icon = 'wallet'
        elif message.startswith('✅') or 'verif' in message.lower():     icon = 'verify'
        elif message.startswith('❌'):                                    icon = 'system'
        else:                                                             icon = 'system'

    # Notifications live in the target user's personal DB
    # Open their personal DB directly without affecting the current request's udb
    try:
        from db import _download_personal_db, _upload_personal_db, _open_personal_db
        from flask import g as _g
        # If this is the current user's own notification and udb is already open, reuse it
        udb_conn = _g.get('udb') if hasattr(_g, 'udb') else None
        udb_uid  = _g.get('udb_uid') if hasattr(_g, 'udb_uid') else None
        if udb_conn and udb_uid == user_id:
            # Write to the already-open personal DB
            try:
                udb_conn.execute(
                    'INSERT INTO notifications (user_id, message, icon, link) VALUES (?,?,?,?)',
                    (user_id, message, icon, link)
                )
            except Exception:
                udb_conn.execute(
                    'INSERT INTO notifications (user_id, message) VALUES (?,?)',
                    (user_id, message)
                )
        else:
            # Cross-user write — run in background thread to avoid blocking the HTTP request
            # (synchronous R2 download + upload adds 200–800ms per notification)
            import threading as _threading

            def _bg_notify(uid, msg, icn, lnk):
                try:
                    from db import _open_personal_db, _upload_personal_db, get_db as _gdb_bg
                    _cn, _pt = _open_personal_db(uid)
                    try:
                        try:
                            _cn.execute(
                                'INSERT INTO notifications (user_id, message, icon, link) '
                                'VALUES (?,?,?,?)',
                                (uid, msg, icn, lnk)
                            )
                        except Exception:
                            _cn.execute(
                                'INSERT INTO notifications (user_id, message) VALUES (?,?)',
                                (uid, msg)
                            )
                        _cn.commit()
                    finally:
                        _cn.close()
                        _upload_personal_db(uid, _pt)
                    # Fire push notification via global db (push_subscriptions lives there)
                    try:
                        import sqlite3 as _sq3
                        from flask import current_app as _ca
                        _gdb = _sq3.connect(_ca.config['DB_PATH'])
                        _gdb.row_factory = _sq3.Row
                        _send_push(_gdb, uid, 'DUYS Boost', msg, lnk or '/feed')
                        _gdb.close()
                    except Exception:
                        pass
                except Exception as _e2:
                    import logging as _l2
                    _l2.getLogger(__name__).warning(
                        'bg_notify uid=%s: %s', uid, _e2
                    )

            _threading.Thread(
                target=_bg_notify,
                args=(user_id, message, icon, link),
                daemon=True,
            ).start()
    except Exception as _e:
        import logging as _log
        _log.getLogger(__name__).warning('add_notification failed uid=%s: %s', user_id, _e)


def _send_push(db, user_id, title, body, url='/feed'):
    """Fire-and-forget Web Push notification. Silently no-ops if pywebpush/VAPID not configured."""
    try:
        import os as _os, json as _json
        private_key   = _os.environ.get('VAPID_PRIVATE_KEY', '')
        claims_email  = _os.environ.get('VAPID_CLAIMS_EMAIL', '')
        if not private_key:
            return
        subs = db.execute(
            'SELECT id, endpoint, subscription_json FROM push_subscriptions WHERE user_id=?',
            (user_id,)
        ).fetchall()
        if not subs:
            return
        from pywebpush import webpush, WebPushException
        payload = _json.dumps({'title': title, 'body': body, 'url': url})
        for sub in subs:
            try:
                sub_info = _json.loads(sub['subscription_json'])
                webpush(
                    subscription_info=sub_info,
                    data=payload,
                    vapid_private_key=private_key,
                    vapid_claims={'sub': f'mailto:{claims_email or "push@duysboost.com"}'}
                )
            except WebPushException as _we:
                if _we.response and _we.response.status_code in (404, 410):
                    db.execute('DELETE FROM push_subscriptions WHERE id=?', (sub['id'],))
                    try:
                        db.commit()
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass


def add_transaction(_db, user_id, type_, amount, description, status='completed'):
    """
    Insert a transaction into the target user's personal DB.
    _db is accepted for API compatibility but ignored — routing is automatic.
    If the current request's personal DB belongs to user_id, writes are in-request.
    For other users, writes happen in a background thread (like add_notification).
    """
    _row = (user_id, type_, amount, description, status)
    _sql = ('INSERT INTO transactions (user_id,type,amount,description,status) '
            'VALUES (?,?,?,?,?)')

    udb_conn = g.get('udb') if hasattr(g, 'udb') else None
    udb_uid  = g.get('udb_uid') if hasattr(g, 'udb_uid') else None

    if udb_conn and udb_uid == user_id:
        udb_conn.execute(_sql, _row)
        return

    import threading as _threading

    def _bg(uid, row):
        try:
            from db import _open_personal_db, _upload_personal_db
            _cn, _pt = _open_personal_db(uid)
            try:
                _cn.execute(_sql, row)
                _cn.commit()
            finally:
                _cn.close()
                _upload_personal_db(uid, _pt)
        except Exception:
            pass

    _threading.Thread(target=_bg, args=(user_id, _row), daemon=True).start()


def check_and_award_referral_bonus(db, user_id):
    from flask import current_app
    REFERRAL_BONUS = current_app.config['REFERRAL_BONUS']
    REFERRAL_ACTIVATION_FEE = current_app.config['REFERRAL_ACTIVATION_FEE']
    CURRENCY_SYMBOL = current_app.config['CURRENCY_SYMBOL']

    user = db.execute(
        'SELECT referred_by, referral_bonus_awarded FROM users WHERE id=?', (user_id,)
    ).fetchone()
    if not user or not user['referred_by'] or user['referral_bonus_awarded']:
        return

    balance_row = db.execute('SELECT balance FROM users WHERE id=?', (user_id,)).fetchone()
    if not balance_row or balance_row['balance'] < REFERRAL_ACTIVATION_FEE:
        return

    admin = db.execute('SELECT id FROM users WHERE is_admin=1 LIMIT 1').fetchone()
    if not admin:
        return

    db.execute('UPDATE users SET balance=balance-? WHERE id=?',
               (REFERRAL_ACTIVATION_FEE, user_id))
    db.execute('UPDATE users SET balance=balance+? WHERE id=?',
               (REFERRAL_ACTIVATION_FEE, admin['id']))
    referred_user = db.execute('SELECT username FROM users WHERE id=?', (user_id,)).fetchone()
    add_transaction(db, user_id, 'activation_fee', REFERRAL_ACTIVATION_FEE,
                   f'Referral activation fee for {referred_user["username"]}')
    add_transaction(db, admin['id'], 'earn', REFERRAL_ACTIVATION_FEE,
                   f'Referral activation fee from {referred_user["username"]}')

    db.execute('UPDATE users SET balance=balance+? WHERE id=?',
               (REFERRAL_BONUS, user['referred_by']))
    db.execute('UPDATE users SET referral_bonus_awarded=1 WHERE id=?', (user_id,))
    add_notification(
        db, user['referred_by'],
        f'🎉 {referred_user["username"]} activated their account! '
        f'+{CURRENCY_SYMBOL}{REFERRAL_BONUS:.2f} referral bonus earned.'
    )
    add_transaction(db, user['referred_by'], 'earn', REFERRAL_BONUS,
                   f'Referral bonus from {referred_user["username"]}')


# ─────────────────────────────────────────────────────────────────────────────
# Misc helpers
# ─────────────────────────────────────────────────────────────────────────────

def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_current_user():
    """Return the logged-in user row from global.db, or None."""
    if 'user_id' not in session:
        return None
    try:
        return get_db().execute(
            'SELECT * FROM users WHERE id=?', (session['user_id'],)
        ).fetchone()
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Social helpers shared between social & boost blueprints
# ─────────────────────────────────────────────────────────────────────────────

def format_post(row, current_uid, db):
    """Convert a DB row to a plain dict enriched with viewer-specific flags."""
    if row is None:
        return None
    p = dict(row)
    p['liked'] = bool(db.execute(
        'SELECT 1 FROM post_likes WHERE user_id=? AND post_id=?',
        (current_uid, p['id'])).fetchone())
    p['bookmarked'] = bool(db.execute(
        'SELECT 1 FROM bookmarks WHERE user_id=? AND post_id=?',
        (current_uid, p['id'])).fetchone())
    # Reactions: current user's reaction + per-type counts
    try:
        _ur = db.execute(
            'SELECT reaction_type FROM post_reactions WHERE user_id=? AND post_id=?',
            (current_uid, p['id'])
        ).fetchone()
        p['user_reaction'] = _ur['reaction_type'] if _ur else None
        _rc = db.execute(
            'SELECT reaction_type, COUNT(*) as cnt FROM post_reactions WHERE post_id=? GROUP BY reaction_type',
            (p['id'],)
        ).fetchall()
        p['reaction_counts'] = {r['reaction_type']: r['cnt'] for r in _rc}
        p['reaction_total']  = sum(p['reaction_counts'].values())
    except Exception:
        p['user_reaction']   = None
        p['reaction_counts'] = {}
        p['reaction_total']  = 0
    # Whether the current user has reposted or quoted this post
    p['reposted'] = bool(db.execute(
        'SELECT 1 FROM posts WHERE user_id=? AND repost_of_id=? LIMIT 1',
        (current_uid, p['id'])).fetchone())
    try:
        author = db.execute('SELECT * FROM users WHERE id=?', (p['user_id'],)).fetchone()
        p['author'] = dict(author) if author else {}
    except Exception:
        p['author'] = {}

    if p.get('repost_of_id'):
        orig_row = db.execute('SELECT * FROM posts WHERE id=?', (p['repost_of_id'],)).fetchone()
        p['repost_of'] = format_post(orig_row, current_uid, db) if orig_row else None
    else:
        p['repost_of'] = None

    if p.get('reply_to_id'):
        parent = db.execute(
            'SELECT p.id, u.username FROM posts p JOIN users u ON p.user_id=u.id WHERE p.id=?',
            (p['reply_to_id'],)
        ).fetchone()
        p['reply_to_username'] = parent['username'] if parent else None
    else:
        p['reply_to_username'] = None

    try:
        boost = db.execute(
            """SELECT pb.* FROM post_boosts pb
               WHERE pb.post_id=? AND pb.status='active'
                 AND pb.budget_spent < pb.budget
                 AND pb.user_id != ?
                 AND NOT EXISTS (
                   SELECT 1 FROM boost_engagements be
                   WHERE be.boost_id=pb.id AND be.worker_id=?
                 )
               ORDER BY pb.created_at DESC LIMIT 1""",
            (p['id'], current_uid, current_uid)
        ).fetchone()
        p['active_boost'] = dict(boost) if boost else None
    except Exception:
        p['active_boost'] = None

    # OG / link preview fields
    for _og_key in ('og_url', 'og_title', 'og_description', 'og_image'):
        if _og_key not in p:
            p[_og_key] = None

    # Sensitive content
    p['is_sensitive'] = p.get('is_sensitive', 0) or 0
    try:
        viewer = db.execute('SELECT auto_show_sensitive FROM users WHERE id=?', (current_uid,)).fetchone()
        p['auto_show_sensitive'] = int(viewer['auto_show_sensitive'] or 0) if viewer else 0
    except Exception:
        p['auto_show_sensitive'] = 0

    if 'media_url' not in p:
        p['media_url'] = None
    if 'media_mime' not in p:
        p['media_mime'] = None
    if p.get('media_url') and not p.get('media_mime'):
        url_lower = p['media_url'].lower()
        if any(url_lower.endswith(e) for e in ('.mp4', '.webm', '.ogv', '.mov')):
            p['media_mime'] = 'video/mp4'
        else:
            p['media_mime'] = 'image/jpeg'

    try:
        if p.get('is_subscriber_only') and p['user_id'] != current_uid:
            is_subscribed = bool(db.execute(
                "SELECT 1 FROM subscriptions WHERE subscriber_id=? AND creator_id=? AND status='active'",
                (current_uid, p['user_id'])
            ).fetchone())
            viewer = db.execute('SELECT is_admin FROM users WHERE id=?', (current_uid,)).fetchone()
            is_admin = bool(viewer and viewer['is_admin'])
            p['locked'] = not (is_subscribed or is_admin)
        else:
            p['locked'] = False
    except Exception:
        p['locked'] = False

    return p


def format_post_with_poll(row, uid, db):
    """format_post plus inline poll data. Sets flat keys used by post_card.html."""
    p = format_post(row, uid, db)
    if p and p.get('post_type') == 'poll':
        options = db.execute(
            'SELECT * FROM poll_options WHERE post_id=? ORDER BY id', (p['id'],)
        ).fetchall()
        total_votes = sum(o['votes'] for o in options)
        user_vote_row = db.execute(
            'SELECT option_id FROM poll_votes WHERE post_id=? AND user_id=?',
            (p['id'], uid)
        ).fetchone()
        user_vote_id  = user_vote_row['option_id'] if user_vote_row else None
        now_iso = datetime.now(timezone.utc).isoformat()
        expired = bool(p.get('poll_expires_at') and p['poll_expires_at'] < now_iso)

        formatted_options = [
            {
                'id':    o['id'],
                'label': o['label'],
                'votes': o['votes'],
                'pct':   round(o['votes'] / total_votes * 100, 1) if total_votes else 0,
            }
            for o in options
        ]
        # Flat keys expected by post_card.html
        p['poll_options']   = formatted_options
        p['poll_user_vote'] = user_vote_id
        p['poll_ended']     = expired
        p['poll_total']     = total_votes
        # Countdown: seconds until expiry (or 0 if expired/no expiry)
        if p.get('poll_expires_at') and not expired:
            try:
                _exp = datetime.fromisoformat(p['poll_expires_at'].replace('Z', ''))
                if _exp.tzinfo is None:
                    _exp = _exp.replace(tzinfo=timezone.utc)
                p['poll_expires_in'] = max(0, int((_exp - datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                p['poll_expires_in'] = 0
        else:
            p['poll_expires_in'] = 0
    else:
        if p:
            p['poll_options']   = None
            p['poll_user_vote'] = None
            p['poll_ended']     = False
            p['poll_total']     = 0
            p['poll_expires_in'] = 0
    return p


def update_counts(db, user_id):
    """Sync follower/following/post counts for a user from live data."""
    db.execute("""UPDATE users SET
        follower_count  = (SELECT COUNT(*) FROM follows WHERE following_id=?),
        following_count = (SELECT COUNT(*) FROM follows WHERE follower_id=?),
        post_count      = (SELECT COUNT(*) FROM posts WHERE user_id=? AND reply_to_id IS NULL)
        WHERE id=?""", (user_id, user_id, user_id, user_id))


def recalc_post_score(db, post_id):
    """Hacker-News-style score, stored on the row for cheap ORDER BY."""
    row = db.execute(
        'SELECT like_count,reply_count,repost_count,view_count,is_boosted,created_at '
        'FROM posts WHERE id=?', (post_id,)
    ).fetchone()
    if not row:
        return
    try:
        ts = row['created_at']
        if ts is None:
            age_h = 1.0
        else:
            ts_str = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)
            ts_str = ts_str.replace('Z', '').replace('+00:00', '').strip()
            posted = datetime.fromisoformat(ts_str)
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            age_h = max(0.1, (datetime.now(timezone.utc) - posted).total_seconds() / 3600)
    except Exception:
        age_h = 1.0
    gravity = 1.8
    interactions = (
        float(row['like_count'] or 0) * 2 +
        float(row['reply_count'] or 0) * 1.5 +
        float(row['repost_count'] or 0) * 1.5 +
        float(row['view_count'] or 0) * 0.05 +
        (20.0 if row['is_boosted'] else 0)
    )
    score = interactions / math.pow(age_h + 2, gravity)
    db.execute('UPDATE posts SET score=? WHERE id=?', (round(score, 6), post_id))


def get_personalized_post_ids(db, uid, limit=20, offset=0):
    """Personalised For-You feed with weighted scoring."""
    seen = {r[0] for r in db.execute(
        'SELECT post_id FROM post_views WHERE user_id=?', (uid,)
    ).fetchall()}
    liked = {r[0] for r in db.execute(
        'SELECT post_id FROM post_likes WHERE user_id=?', (uid,)
    ).fetchall()}
    exclude = seen | liked

    following_ids = [r[0] for r in db.execute(
        'SELECT following_id FROM follows WHERE follower_id=?', (uid,)
    ).fetchall()]

    results = {}

    if following_ids:
        ph = ','.join(['?'] * len(following_ids))
        rows = db.execute(
            f'SELECT id, score FROM posts '
            f'WHERE user_id IN ({ph}) AND reply_to_id IS NULL '
            f'AND (status IS NULL OR status=\'published\') '
            f'AND id NOT IN (SELECT post_id FROM channel_posts) '
            f'ORDER BY score DESC LIMIT 60',
            following_ids
        ).fetchall()
        for r in rows:
            if r['id'] not in exclude:
                results[r['id']] = float(r['score'] or 0) * 1.6

    if following_ids:
        ph = ','.join(['?'] * len(following_ids))
        rows = db.execute(
            f'SELECT DISTINCT p.id, p.score FROM posts p '
            f'JOIN post_likes l ON l.post_id=p.id '
            f'WHERE l.user_id IN ({ph}) AND p.user_id != ? '
            f'AND p.reply_to_id IS NULL AND (p.status IS NULL OR p.status=\'published\') '
            f'ORDER BY p.score DESC LIMIT 40',
            following_ids + [uid]
        ).fetchall()
        for r in rows:
            if r['id'] not in exclude:
                existing = results.get(r['id'], 0)
                results[r['id']] = max(existing, float(r['score'] or 0) * 1.2)

    need = max(0, limit + offset - len(results))
    if need > 0:
        known = list(results.keys()) + list(exclude) + [0]
        ph = ','.join(['?'] * len(known))
        rows = db.execute(
            f'SELECT id, score FROM posts '
            f'WHERE id NOT IN ({ph}) AND reply_to_id IS NULL AND user_id != ? '
            f'AND (status IS NULL OR status=\'published\') '
            f'AND id NOT IN (SELECT post_id FROM channel_posts) '
            f'ORDER BY score DESC LIMIT ?',
            known + [uid, need + 20]
        ).fetchall()
        for r in rows:
            results[r['id']] = float(r['score'] or 0)

    ranked = sorted(results.items(), key=lambda x: -x[1])
    return [pid for pid, _ in ranked[offset: offset + limit]]


# ─────────────────────────────────────────────────────────────────────────────
# Task verification helpers
# ─────────────────────────────────────────────────────────────────────────────

def verify_task_completion(ad, proof_link, user_id):
    platform = ad['platform'].lower()
    task_type = ad['task_type'].lower()

    if not proof_link or not proof_link.startswith(('http://', 'https://')):
        return {'valid': False, 'error': 'Please provide a valid URL as proof.'}

    if task_type == 'follow':
        return _verify_follow_task(platform, proof_link, ad['target_url'])
    elif task_type in ('like', 'comment', 'share'):
        return _verify_follow_task(platform, proof_link, '')
    return {'valid': True, 'error': ''}


def _verify_follow_task(platform, proof_link, target_url):
    domain_map = {
        'instagram': 'instagram.com/',
        'tiktok': 'tiktok.com/',
        'twitter': ('twitter.com/', 'x.com/'),
        'x': ('twitter.com/', 'x.com/'),
        'facebook': 'facebook.com/',
        'youtube': ('youtube.com/', 'youtu.be/'),
    }
    expected = domain_map.get(platform)
    if not expected:
        return {'valid': True, 'error': ''}
    domains = (expected,) if isinstance(expected, str) else expected
    if any(d in proof_link for d in domains):
        return {'valid': True, 'error': ''}
    platform_label = platform.title()
    return {'valid': False, 'error': f'Please provide a {platform_label} URL as proof.'}
