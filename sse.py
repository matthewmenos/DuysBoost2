"""
sse.py — Server-Sent Events for DUYS Boost.

Replaces all setInterval() polling with a single persistent HTTP connection
per user. The browser opens one SSE stream at /api/stream and receives
push notifications for:

  • notifications   — unread count + latest message text
  • dm_unread       — total unread DM count
  • group_unread    — total unread group message count
  • online_ping     — server heartbeat (keeps connection alive, updates presence)
  • activity        — latest marketplace task completions (dashboard)

Per-conversation message streams:
  • /api/messages/<username>/stream  — new DMs in a specific thread
  • /api/group/<slug>/stream         — new messages in a group chat

SSE vs WebSocket for this use case:
  • SSE is unidirectional (server → browser) — perfect here since all
    user actions are already REST POST requests
  • Works over HTTP/1.1, no protocol upgrade, no separate library needed
  • Browsers reconnect automatically on disconnect (built-in)
  • Much simpler to deploy — works with any WSGI server, no asyncio needed

Threading note:
  Each SSE connection runs a generator in a background thread (via
  Flask's streaming response). This is safe with sqlite3 because each
  generator opens its own short-lived DB connection, separate from the
  per-request g.db connection.

Production note:
  With multiple Gunicorn workers, each worker maintains its own SSE
  connections. This is fine — the client reconnects to any worker and
  the DB is the shared source of truth. For >10k concurrent users,
  consider an async framework (Quart/FastAPI) for the SSE endpoints only.
"""

import json
import time
import logging
from datetime import datetime, timezone

from flask import Blueprint, Response, session, request, stream_with_context
import os

logger = logging.getLogger(__name__)

bp = Blueprint('sse', __name__)

# How often the generator polls the DB (seconds)
_GLOBAL_INTERVAL   = 8    # global stream (notifications, badges)
_MESSAGE_INTERVAL  = 1.5  # DM thread stream
_GROUP_INTERVAL    = 1.5  # group chat stream
_KEEPALIVE_EVERY   = 25   # send a comment ping to prevent proxy timeout


def _get_db_conn(user_id: int = None):
    """
    Open a short-lived SQLite connection for use inside an SSE generator.
    For SSE generators we can't use Flask's g-based connection because generators
    run outside the normal request context. We download the user DB directly.
    """
    if user_id:
        from db import open_user_db
        conn, _ = open_user_db(user_id)
        return conn
    # Fall back to global.db for non-user-specific queries
    import sqlite3 as _sqlite
    db_path = os.path.join(os.path.dirname(__file__), 'global.db')
    conn = _sqlite.connect(db_path, check_same_thread=False)
    conn.row_factory = _sqlite.Row
    conn.execute('PRAGMA journal_mode = WAL')
    return conn


def _sse_event(event: str, data: dict) -> str:
    """Format a single SSE message."""
    return f'event: {event}\ndata: {json.dumps(data)}\n\n'


def _sse_comment(text: str = '') -> str:
    """SSE comment line — keeps the connection alive through proxies."""
    return f': {text}\n\n'


# ── Global stream — notifications, badge counts, presence, activity ───────────

def _global_generator(uid: int):
    """
    Yields SSE events for the logged-in user.
    Runs in a streaming thread — uses its own DB connection.
    """
    try:
        conn = _get_db_conn(uid)
    except Exception as e:
        logger.error('SSE global: DB connect failed for uid=?: ?', uid, e)
        return

    last_ping   = time.time()
    last_notif  = 0          # last notification id sent
    last_active = 0          # last activity row id sent

    try:
        # Send an initial "connected" event so the client knows the stream is live
        yield _sse_event('connected', {'uid': uid})

        while True:
            now = time.time()

            # ── Keepalive comment every 25 s ────────────────────────────────
            if now - last_ping >= _KEEPALIVE_EVERY:
                yield _sse_comment('keepalive')
                last_ping = now

            try:
                cur = conn.cursor()

                # ── Update online presence ───────────────────────────────────
                ts = datetime.now(timezone.utc).isoformat()
                cur.execute('UPDATE users SET online_at=? WHERE id=?', (ts, uid))

                # ── Notification count + latest unseen ───────────────────────
                cur.execute(
                    'SELECT COUNT(*) as cnt FROM notifications '
                    'WHERE user_id=? AND read=0',
                    (uid,)
                )
                notif_count = cur.fetchone()['cnt']

                cur.execute(
                    'SELECT id, message, created_at FROM notifications '
                    'WHERE user_id=? AND id > ? '
                    'ORDER BY id DESC LIMIT 3',
                    (uid, last_notif)
                )
                new_notifs = cur.fetchall()
                if new_notifs:
                    last_notif = new_notifs[0]['id']
                    yield _sse_event('notifications', {
                        'count':  notif_count,
                        'recent': [
                            {'msg': n['message'], 'time': str(n['created_at'])[:16]}
                            for n in new_notifs
                        ],
                    })
                elif notif_count == 0:
                    # Badge cleared (user read notifications)
                    yield _sse_event('notifications', {'count': 0, 'recent': []})

                # ── DM unread count ──────────────────────────────────────────
                cur.execute(
                    'SELECT unread_dm_count FROM users WHERE id=?', (uid,)
                )
                dm_row = cur.fetchone()
                dm_count = int(dm_row['unread_dm_count'] or 0) if dm_row else 0
                yield _sse_event('dm_unread', {'count': dm_count})

                # ── Group unread count ───────────────────────────────────────
                cur.execute("""
                    SELECT COUNT(DISTINCT gm.group_id) AS cnt
                    FROM group_messages gm
                    JOIN group_members gmp
                      ON gmp.group_id = gm.group_id AND gmp.user_id = ?
                    WHERE gm.sender_id != ?
                      AND gm.created_at > COALESCE(gmp.last_read_at, '1970-01-01')
                """, (uid, uid))
                grp_row = cur.fetchone()
                grp_count = int(grp_row['cnt'] or 0) if grp_row else 0
                yield _sse_event('group_unread', {'count': grp_count})

                # ── Activity feed (dashboard recent completions) ─────────────
                cur.execute("""
                    SELECT tc.id, tc.reward, tc.submitted_at,
                           u.username as worker, a.title as ad
                    FROM task_completions tc
                    JOIN users u ON u.id = tc.worker_id
                    JOIN ads   a ON a.id = tc.ad_id
                    WHERE tc.id > ?
                    ORDER BY tc.submitted_at DESC LIMIT 5
                """, (last_active,))
                new_activity = cur.fetchall()
                if new_activity:
                    last_active = new_activity[0]['id']
                    yield _sse_event('activity', {
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

                conn.commit()
                cur.close()

            except Exception as e:
                logger.warning('SSE global DB error uid=?: ?', uid, e)
                try:
                    conn = _get_db_conn(uid)
                except Exception:
                    break

            time.sleep(_GLOBAL_INTERVAL)

    except GeneratorExit:
        pass  # client disconnected cleanly
    finally:
        try:
            conn.commit()
            conn.close()
        except Exception:
            pass


@bp.route('/api/stream')
def global_stream():
    """
    Global SSE endpoint — one connection per logged-in user.
    Pushes: notifications, dm_unread, group_unread, activity, keepalive.
    """
    uid = session.get('user_id')
    if not uid:
        return Response('data: {"error":"not authenticated"}\n\n',
                        status=401, mimetype='text/event-stream')

    return Response(
        stream_with_context(_global_generator(uid)),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':    'no-cache',
            'X-Accel-Buffering':'no',     # disable nginx buffering
            'Connection':       'keep-alive',
        },
    )


# ── DM thread stream ──────────────────────────────────────────────────────────

def _dm_generator(uid: int, other_username: str, after: int):
    """
    Yields new messages in a specific DM conversation.
    Also marks messages as read and updates the unread badge.
    """
    try:
        conn = _get_db_conn(uid)
    except Exception as e:
        logger.error('SSE DM: DB connect failed: ?', e)
        return

    last_id    = after
    last_ping  = time.time()

    try:
        cur = conn.cursor()
        cur.execute('SELECT id FROM users WHERE username=?', (other_username,))
        other = cur.fetchone()
        if not other:
            yield _sse_event('error', {'message': 'User not found'})
            return

        a, b = min(uid, other['id']), max(uid, other['id'])
        cur.execute(
            'SELECT id FROM conversations WHERE user_a=? AND user_b=?', (a, b)
        )
        conv = cur.fetchone()
        if not conv:
            yield _sse_event('ready', {'conv_id': None})
            return

        conv_id = conv['id']
        cur.close()
        yield _sse_event('ready', {'conv_id': conv_id})

        while True:
            now = time.time()

            if now - last_ping >= _KEEPALIVE_EVERY:
                yield _sse_comment('keepalive')
                last_ping = now

            try:
                cur = conn.cursor()

                cur.execute("""
                    SELECT m.*, u.username AS sender_username,
                           u.avatar_url AS sender_avatar
                    FROM messages m
                    JOIN users u ON u.id = m.sender_id
                    WHERE m.conversation_id = ? AND m.id > ?
                    ORDER BY m.created_at ASC LIMIT 50
                """, (conv_id, last_id))
                rows = cur.fetchall()

                if rows:
                    last_id = rows[-1]['id']
                    # Mark received messages as read
                    cur.execute(
                        'UPDATE messages SET is_read=1 '
                        'WHERE conversation_id=? AND sender_id!=? AND id <= ?',
                        (conv_id, uid, last_id)
                    )
                    # Recalculate total unread
                    cur.execute("""
                        SELECT COUNT(*) AS cnt
                        FROM messages m
                        JOIN conversations c ON c.id = m.conversation_id
                        WHERE (c.user_a=? OR c.user_b=?)
                          AND m.sender_id != ? AND m.is_read = 0
                    """, (uid, uid, uid))
                    total_unread = cur.fetchone()['cnt']
                    cur.execute(
                        'UPDATE users SET unread_dm_count=? WHERE id=?',
                        (total_unread, uid)
                    )
                    conn.commit()
                    cur.close()

                    yield _sse_event('messages', {
                        'messages': [dict(r) for r in rows]
                    })
                else:
                    cur.close()

            except Exception as e:
                logger.warning('SSE DM DB error: ?', e)
                try:
                    conn = _get_db_conn(uid)
                except Exception:
                    break

            time.sleep(_MESSAGE_INTERVAL)

    except GeneratorExit:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


@bp.route('/api/messages/<username>/stream')
def dm_stream(username):
    """
    SSE stream for a specific DM conversation.
    Replaces the /api/messages/<username>/poll endpoint.
    """
    uid = session.get('user_id')
    if not uid:
        return Response('data: {"error":"not authenticated"}\n\n',
                        status=401, mimetype='text/event-stream')

    after = request.args.get('after', 0, type=int)

    return Response(
        stream_with_context(_dm_generator(uid, username, after)),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':    'no-cache',
            'X-Accel-Buffering':'no',
            'Connection':       'keep-alive',
        },
    )


# ── Group chat stream ─────────────────────────────────────────────────────────

def _group_generator(uid: int, slug: str, after: int):
    """Yields new messages in a group chat."""
    try:
        conn = _get_db_conn(uid)
    except Exception as e:
        logger.error('SSE group: DB connect failed: ?', e)
        return

    last_id   = after
    last_ping = time.time()

    try:
        cur = conn.cursor()
        cur.execute('SELECT id FROM groups WHERE slug=?', (slug,))
        grp = cur.fetchone()
        if not grp:
            yield _sse_event('error', {'message': 'Group not found'})
            return

        group_id = grp['id']

        # Verify membership
        cur.execute(
            'SELECT 1 FROM group_members WHERE group_id=? AND user_id=?',
            (group_id, uid)
        )
        if not cur.fetchone():
            yield _sse_event('error', {'message': 'Not a member'})
            return

        cur.close()
        yield _sse_event('ready', {'group_id': group_id})

        while True:
            now = time.time()

            if now - last_ping >= _KEEPALIVE_EVERY:
                yield _sse_comment('keepalive')
                last_ping = now

            try:
                cur = conn.cursor()

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
                    # Update last_read_at
                    ts = datetime.now(timezone.utc).isoformat()
                    cur.execute(
                        'UPDATE group_members SET last_read_at=? '
                        'WHERE group_id=? AND user_id=?',
                        (ts, group_id, uid)
                    )
                    conn.commit()
                    cur.close()
                    yield _sse_event('messages', {
                        'messages': [dict(r) for r in rows]
                    })
                else:
                    cur.close()

            except Exception as e:
                logger.warning('SSE group DB error: ?', e)
                try:
                    conn = _get_db_conn(uid)
                except Exception:
                    break

            time.sleep(_GROUP_INTERVAL)

    except GeneratorExit:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


@bp.route('/api/group/<slug>/stream')
def group_stream(slug):
    """
    SSE stream for a specific group chat.
    Replaces the /api/group/<slug>/poll endpoint.
    """
    uid = session.get('user_id')
    if not uid:
        return Response('data: {"error":"not authenticated"}\n\n',
                        status=401, mimetype='text/event-stream')

    after = request.args.get('after', 0, type=int)

    return Response(
        stream_with_context(_group_generator(uid, slug, after)),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':    'no-cache',
            'X-Accel-Buffering':'no',
            'Connection':       'keep-alive',
        },
    )
