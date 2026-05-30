"""
sse.py — Server-Sent Events for DUYS Boost.

Architecture note — two SQLite files:
  global.db   → users, posts, groups, group_messages, task_completions, ads
  users/{n}.db → notifications, conversations, messages (personal/private)

Every SSE generator opens *both* connections where needed, because SSE runs
in a streaming thread outside Flask's request context — it cannot use the
g-based get_db() / get_user_db() helpers.
"""

import json
import os
import sqlite3
import time
import logging
from datetime import datetime, timezone

from flask import Blueprint, Response, session, request, stream_with_context

logger = logging.getLogger(__name__)

bp = Blueprint('sse', __name__)

# Polling intervals (seconds)
_GLOBAL_INTERVAL  = 8
_MESSAGE_INTERVAL = 1.5
_GROUP_INTERVAL   = 1.5
_KEEPALIVE_EVERY  = 25


# ── DB helpers (no Flask context needed) ─────────────────────────────────────

def _open_global() -> sqlite3.Connection:
    """Open global.db directly — safe to call from any thread."""
    path = os.path.join(os.path.dirname(__file__), 'global.db')
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous = NORMAL')
    return conn


def _open_personal(uid: int) -> sqlite3.Connection:
    """Download and open the per-user personal DB — safe to call from any thread."""
    from db import _open_personal_db
    conn, _path = _open_personal_db(uid)
    return conn


# ── SSE format helpers ────────────────────────────────────────────────────────

def _event(name: str, data: dict) -> str:
    return f'event: {name}\ndata: {json.dumps(data)}\n\n'


def _comment(text: str = '') -> str:
    return f': {text}\n\n'


# ── Global stream ─────────────────────────────────────────────────────────────

def _global_generator(uid: int):
    """
    Yields SSE events for one logged-in user.

    Uses two separate connections:
      gconn  → global.db  (users, groups, group_messages, task_completions)
      pconn  → personal DB (notifications)
    """
    try:
        gconn = _open_global()
    except Exception as e:
        logger.error('SSE global: global DB connect failed uid=%s: %s', uid, e)
        return

    try:
        pconn = _open_personal(uid)
    except Exception as e:
        logger.error('SSE global: personal DB connect failed uid=%s: %s', uid, e)
        try:
            gconn.close()
        except Exception:
            pass
        return

    last_ping   = time.time()
    last_notif  = 0
    last_active = 0

    try:
        yield _event('connected', {'uid': uid})

        while True:
            now = time.time()

            if now - last_ping >= _KEEPALIVE_EVERY:
                yield _comment('keepalive')
                last_ping = now

            # ── Global DB queries ─────────────────────────────────────────
            try:
                gcur = gconn.cursor()

                # Update online presence
                ts = datetime.now(timezone.utc).isoformat()
                gcur.execute('UPDATE users SET online_at=? WHERE id=?', (ts, uid))

                # DM unread count
                gcur.execute(
                    'SELECT unread_dm_count FROM users WHERE id=?', (uid,)
                )
                dm_row   = gcur.fetchone()
                dm_count = int(dm_row['unread_dm_count'] or 0) if dm_row else 0
                yield _event('dm_unread', {'count': dm_count})

                # Group unread count
                gcur.execute("""
                    SELECT COUNT(DISTINCT gm.group_id) AS cnt
                    FROM group_messages gm
                    JOIN group_members gmp
                      ON gmp.group_id = gm.group_id AND gmp.user_id = ?
                    WHERE gm.sender_id != ?
                      AND gm.created_at > COALESCE(gmp.last_read_at, '1970-01-01')
                """, (uid, uid))
                grp_row   = gcur.fetchone()
                grp_count = int(grp_row['cnt'] or 0) if grp_row else 0
                yield _event('group_unread', {'count': grp_count})

                # Activity feed (latest marketplace completions)
                gcur.execute("""
                    SELECT tc.id, tc.reward, tc.submitted_at,
                           u.username AS worker, a.title AS ad
                    FROM task_completions tc
                    JOIN users u ON u.id = tc.worker_id
                    JOIN ads   a ON a.id = tc.ad_id
                    WHERE tc.id > ?
                    ORDER BY tc.submitted_at DESC LIMIT 5
                """, (last_active,))
                new_activity = gcur.fetchall()
                if new_activity:
                    last_active = new_activity[0]['id']
                    yield _event('activity', {
                        'items': [
                            {
                                'worker': r['worker'],
                                'ad':     r['ad'],
                                'reward': float(r['reward'] or 0),
                                'time':   str(r['submitted_at'])[11:19],
                            }
                            for r in new_activity
                        ]
                    })

                gconn.commit()
                gcur.close()

            except Exception as e:
                logger.warning('SSE global gconn error uid=%s: %s', uid, e)
                try:
                    gconn.close()
                except Exception:
                    pass
                try:
                    gconn = _open_global()
                except Exception:
                    break

            # ── Personal DB queries (notifications) ───────────────────────
            try:
                pcur = pconn.cursor()

                pcur.execute(
                    'SELECT COUNT(*) AS cnt FROM notifications WHERE user_id=? AND read=0',
                    (uid,)
                )
                notif_count = pcur.fetchone()['cnt']

                pcur.execute(
                    'SELECT id, message, icon, link, created_at FROM notifications '
                    'WHERE user_id=? AND id > ? ORDER BY id DESC LIMIT 5',
                    (uid, last_notif)
                )
                new_notifs = pcur.fetchall()
                if new_notifs:
                    last_notif = new_notifs[0]['id']
                    yield _event('notifications', {
                        'count':  notif_count,
                        'recent': [
                            {
                                'id':   n['id'],
                                'msg':  n['message'],
                                'icon': n['icon'],
                                'link': n['link'],
                                'time': str(n['created_at'])[:16],
                            }
                            for n in new_notifs
                        ],
                    })
                elif notif_count == 0:
                    yield _event('notifications', {'count': 0, 'recent': []})

                pcur.close()

            except Exception as e:
                logger.warning('SSE global pconn error uid=%s: %s', uid, e)
                try:
                    pconn.close()
                except Exception:
                    pass
                try:
                    pconn = _open_personal(uid)
                except Exception:
                    pass  # non-fatal: notifications fail silently

            time.sleep(_GLOBAL_INTERVAL)

    except GeneratorExit:
        pass
    finally:
        for c in (gconn, pconn):
            try:
                c.commit()
                c.close()
            except Exception:
                pass


@bp.route('/api/stream')
def global_stream():
    uid = session.get('user_id')
    if not uid:
        return Response(
            'data: {"error":"not authenticated"}\n\n',
            status=401, mimetype='text/event-stream',
        )
    return Response(
        stream_with_context(_global_generator(uid)),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':     'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection':        'keep-alive',
        },
    )


# ── DM thread stream ──────────────────────────────────────────────────────────

def _dm_generator(uid: int, other_username: str, after: int):
    """
    Yields new messages in a specific DM thread.

    gconn → global.db  (find other user ID, fetch sender display info)
    pconn → personal DB (conversations, messages)
    """
    try:
        gconn = _open_global()
    except Exception as e:
        logger.error('SSE DM: global DB connect failed uid=%s: %s', uid, e)
        return

    try:
        pconn = _open_personal(uid)
    except Exception as e:
        logger.error('SSE DM: personal DB connect failed uid=%s: %s', uid, e)
        try:
            gconn.close()
        except Exception:
            pass
        return

    last_id   = after
    last_ping = time.time()

    try:
        # Resolve other user from global DB
        gcur = gconn.cursor()
        gcur.execute('SELECT id FROM users WHERE username=?', (other_username,))
        other = gcur.fetchone()
        gcur.close()

        if not other:
            yield _event('error', {'message': 'User not found'})
            return

        a, b = min(uid, other['id']), max(uid, other['id'])

        # Find or confirm conversation in personal DB
        pcur = pconn.cursor()
        pcur.execute(
            'SELECT id FROM conversations WHERE user_a=? AND user_b=?', (a, b)
        )
        conv = pcur.fetchone()
        pcur.close()

        if not conv:
            yield _event('ready', {'conv_id': None})
            return

        conv_id = conv['id']
        yield _event('ready', {'conv_id': conv_id})

        while True:
            now = time.time()

            if now - last_ping >= _KEEPALIVE_EVERY:
                yield _comment('keepalive')
                last_ping = now

            try:
                # Fetch new messages from personal DB
                pcur = pconn.cursor()
                pcur.execute(
                    'SELECT * FROM messages '
                    'WHERE conversation_id=? AND id > ? '
                    'ORDER BY created_at ASC LIMIT 50',
                    (conv_id, last_id)
                )
                rows = pcur.fetchall()

                if rows:
                    last_id = rows[-1]['id']

                    # Mark received messages as read
                    pcur.execute(
                        'UPDATE messages SET is_read=1 '
                        'WHERE conversation_id=? AND sender_id!=? AND id <= ?',
                        (conv_id, uid, last_id)
                    )

                    # Recalculate total unread
                    pcur.execute("""
                        SELECT COUNT(*) AS cnt
                        FROM messages m
                        JOIN conversations c ON c.id = m.conversation_id
                        WHERE (c.user_a=? OR c.user_b=?)
                          AND m.sender_id != ? AND m.is_read = 0
                    """, (uid, uid, uid))
                    total_unread = pcur.fetchone()['cnt']
                    pconn.commit()
                    pcur.close()

                    # Update unread count in global DB
                    try:
                        gcur = gconn.cursor()
                        gcur.execute(
                            'UPDATE users SET unread_dm_count=? WHERE id=?',
                            (total_unread, uid)
                        )
                        gconn.commit()
                        gcur.close()
                    except Exception:
                        pass

                    # Enrich messages with sender info from global DB
                    enriched = []
                    for r in rows:
                        msg = dict(r)
                        try:
                            gcur = gconn.cursor()
                            gcur.execute(
                                'SELECT username, avatar_url FROM users WHERE id=?',
                                (msg['sender_id'],)
                            )
                            sender = gcur.fetchone()
                            gcur.close()
                            msg['sender_username'] = sender['username'] if sender else ''
                            msg['sender_avatar']   = sender['avatar_url'] if sender else None
                        except Exception:
                            msg['sender_username'] = ''
                            msg['sender_avatar']   = None
                        enriched.append(msg)

                    yield _event('messages', {'messages': enriched})
                else:
                    pcur.close()

            except Exception as e:
                logger.warning('SSE DM DB error uid=%s: %s', uid, e)
                try:
                    pconn.close()
                except Exception:
                    pass
                try:
                    pconn = _open_personal(uid)
                except Exception:
                    break

            time.sleep(_MESSAGE_INTERVAL)

    except GeneratorExit:
        pass
    finally:
        for c in (gconn, pconn):
            try:
                c.close()
            except Exception:
                pass


@bp.route('/api/messages/<username>/stream')
def dm_stream(username):
    uid = session.get('user_id')
    if not uid:
        return Response(
            'data: {"error":"not authenticated"}\n\n',
            status=401, mimetype='text/event-stream',
        )
    after = request.args.get('after', 0, type=int)
    return Response(
        stream_with_context(_dm_generator(uid, username, after)),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':     'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection':        'keep-alive',
        },
    )


# ── Group chat stream ─────────────────────────────────────────────────────────

def _group_generator(uid: int, slug: str, after: int):
    """
    Yields new messages in a group chat.
    Groups and group_messages both live in global.db — only one connection needed.
    """
    try:
        gconn = _open_global()
    except Exception as e:
        logger.error('SSE group: DB connect failed uid=%s: %s', uid, e)
        return

    last_id   = after
    last_ping = time.time()

    try:
        cur = gconn.cursor()
        cur.execute('SELECT id FROM groups WHERE slug=?', (slug,))
        grp = cur.fetchone()
        if not grp:
            yield _event('error', {'message': 'Group not found'})
            return

        group_id = grp['id']

        cur.execute(
            'SELECT 1 FROM group_members WHERE group_id=? AND user_id=?',
            (group_id, uid)
        )
        if not cur.fetchone():
            yield _event('error', {'message': 'Not a member'})
            return

        cur.close()
        yield _event('ready', {'group_id': group_id})

        while True:
            now = time.time()

            if now - last_ping >= _KEEPALIVE_EVERY:
                yield _comment('keepalive')
                last_ping = now

            try:
                cur = gconn.cursor()
                cur.execute("""
                    SELECT gm.*,
                           u.username     AS sender_username,
                           u.display_name AS sender_display,
                           u.avatar_url   AS sender_avatar
                    FROM group_messages gm
                    JOIN users u ON u.id = gm.sender_id
                    WHERE gm.group_id = ?
                      AND gm.id > ?
                      AND gm.deleted_at IS NULL
                    ORDER BY gm.created_at ASC LIMIT 50
                """, (group_id, last_id))
                rows = cur.fetchall()

                if rows:
                    last_id = rows[-1]['id']
                    ts = datetime.now(timezone.utc).isoformat()
                    cur.execute(
                        'UPDATE group_members SET last_read_at=? '
                        'WHERE group_id=? AND user_id=?',
                        (ts, group_id, uid)
                    )
                    gconn.commit()
                    cur.close()
                    yield _event('messages', {'messages': [dict(r) for r in rows]})
                else:
                    cur.close()

            except Exception as e:
                logger.warning('SSE group DB error uid=%s: %s', uid, e)
                try:
                    gconn.close()
                except Exception:
                    pass
                try:
                    gconn = _open_global()
                except Exception:
                    break

            time.sleep(_GROUP_INTERVAL)

    except GeneratorExit:
        pass
    finally:
        try:
            gconn.commit()
            gconn.close()
        except Exception:
            pass


@bp.route('/api/group/<slug>/stream')
def group_stream(slug):
    uid = session.get('user_id')
    if not uid:
        return Response(
            'data: {"error":"not authenticated"}\n\n',
            status=401, mimetype='text/event-stream',
        )
    after = request.args.get('after', 0, type=int)
    return Response(
        stream_with_context(_group_generator(uid, slug, after)),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':     'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection':        'keep-alive',
        },
    )
