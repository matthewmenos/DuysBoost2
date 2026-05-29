"""
blueprints/admin.py — Full Admin Dashboard

Sections:
  /admin                  — Overview (stats, charts data, recent activity)
  /admin/users            — User management (list, search, filter)
  /admin/users/<id>       — Individual user profile + actions
  /admin/posts            — Post moderation
  /admin/reports          — User reports queue
  /admin/withdrawals      — Withdrawal approvals
  /admin/deposits         — Deposit history
  /admin/reviews          — Platform reviews
  /admin/audit            — Audit log

Actions (POST):
  /admin/user/<id>/ban            — Ban / unban user
  /admin/user/<id>/adjust-balance — Credit / debit balance
  /admin/user/<id>/reset-password — Reset password
  /admin/user/<id>/delete         — Hard-delete account
  /admin/user/<id>/notify         — Send notification
  /admin/post/<id>/delete         — Delete post
  /admin/post/<id>/toggle-boost   — Force-boost / remove boost
  /admin/report/<id>/action       — Resolve report
  /admin/withdrawal/<id>/<action> — Approve / reject withdrawal
  /admin/deposit                  — Manual credit deposit
  /admin/review/<id>/action       — Hide / feature / reply to review
  /admin/broadcast                — Broadcast notification to all users
"""

import json
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (
    Blueprint, jsonify, redirect, render_template,
    request, session, url_for, current_app
)

from helpers import (
    get_db, get_user_db, login_required, admin_required,
    safe_float, safe_int, add_notification, add_transaction, hash_password
)

bp = Blueprint('admin', __name__)


# ── Audit logging helper ──────────────────────────────────────────────────────

def _audit(db, action, target_type=None, target_id=None, details=None):
    uid = session.get('user_id')
    ip  = request.remote_addr
    db.execute(
        'INSERT INTO admin_audit_log (admin_id,action,target_type,target_id,details,ip_address) '
        'VALUES (?,?,?,?,?,?)',
        (uid, action, target_type, target_id,
         json.dumps(details) if isinstance(details, dict) else details, ip)
    )


# ── Overview ──────────────────────────────────────────────────────────────────

@bp.route('/admin')
@admin_required
def admin():
    db  = get_db()

    # ── Summary stats ─────────────────────────────────────────────────────────
    total_users    = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    active_users   = db.execute(
        "SELECT COUNT(*) FROM users WHERE online_at >= datetime('now','-7 days')"
    ).fetchone()[0]
    try:
        banned_users = db.execute('SELECT COUNT(*) FROM users WHERE is_banned=1').fetchone()[0]
    except Exception:
        banned_users = 0  # Column missing — migration will run on next request
    total_posts    = db.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
    total_ads      = db.execute('SELECT COUNT(*) FROM ads').fetchone()[0]
    total_vol      = db.execute('SELECT COALESCE(SUM(amount),0) FROM transactions').fetchone()[0]
    pending_wdrs   = db.execute(
        "SELECT COUNT(*) FROM withdrawals WHERE status='pending'"
    ).fetchone()[0]
    open_reports   = db.execute(
        "SELECT COUNT(*) FROM reports WHERE status='open'"
    ).fetchone()[0]
    total_reviews  = db.execute('SELECT COUNT(*) FROM platform_reviews').fetchone()[0]
    avg_rating     = db.execute(
        "SELECT ROUND(AVG(rating),1) FROM platform_reviews WHERE status='published'"
    ).fetchone()[0] or 0

    # ── Recent signups (last 7 days by day) ───────────────────────────────────
    signup_rows = db.execute("""
        SELECT DATE(created_at) as day, COUNT(*) as cnt
        FROM users
        WHERE created_at >= datetime('now','-7 days')
        GROUP BY day ORDER BY day
    """).fetchall()
    signup_chart = [dict(r) for r in signup_rows]

    # ── Revenue last 7 days ───────────────────────────────────────────────────
    revenue_rows = db.execute("""
        SELECT DATE(created_at) as day, ROUND(SUM(amount),2) as total
        FROM transactions
        WHERE type='deposit' AND created_at >= datetime('now','-7 days')
        GROUP BY day ORDER BY day
    """).fetchall()
    revenue_chart = [dict(r) for r in revenue_rows]

    # ── Daily posts last 7 days ───────────────────────────────────────────────
    posts_chart = [dict(r) for r in db.execute("""
        SELECT DATE(created_at) as day, COUNT(*) as cnt
        FROM posts
        WHERE created_at >= datetime('now','-7 days')
        GROUP BY day ORDER BY day
    """).fetchall()]

    # ── Top earners (by balance) ──────────────────────────────────────────────
    top_earners = [dict(r) for r in db.execute("""
        SELECT username, display_name, balance FROM users
        WHERE balance > 0 ORDER BY balance DESC LIMIT 5
    """).fetchall()]

    # ── Top posters ───────────────────────────────────────────────────────────
    top_posters = [dict(r) for r in db.execute("""
        SELECT username, display_name, post_count FROM users
        WHERE post_count > 0 ORDER BY post_count DESC LIMIT 5
    """).fetchall()]

    # ── Recent activity feed ──────────────────────────────────────────────────
    recent_users = db.execute(
        'SELECT id,username,display_name,avatar_url,created_at,is_banned '
        'FROM users ORDER BY created_at DESC LIMIT 8'
    ).fetchall()
    recent_reports = db.execute("""
        SELECT r.*, u.username as reporter_name
        FROM reports r JOIN users u ON u.id=r.reporter_id
        WHERE r.status='open' ORDER BY r.created_at DESC LIMIT 5
    """).fetchall()
    recent_wdrs = db.execute("""
        SELECT w.*, u.username FROM withdrawals w
        JOIN users u ON w.user_id=u.id
        WHERE w.status='pending' ORDER BY w.created_at DESC LIMIT 5
    """).fetchall()

    return render_template('admin/overview.html',
        total_users=total_users, active_users=active_users,
        banned_users=banned_users, total_posts=total_posts,
        total_ads=total_ads, total_vol=total_vol,
        pending_wdrs=pending_wdrs, open_reports=open_reports,
        total_reviews=total_reviews, avg_rating=avg_rating,
        signup_chart=signup_chart, revenue_chart=revenue_chart,
        posts_chart=posts_chart, top_earners=top_earners, top_posters=top_posters,
        recent_users=recent_users, recent_reports=recent_reports,
        recent_wdrs=recent_wdrs,
    )


# ── Users ─────────────────────────────────────────────────────────────────────

@bp.route('/admin/users')
@admin_required
def admin_users():
    db     = get_db()
    q      = (request.args.get('q') or '').strip()
    status = request.args.get('status', 'all')   # all | active | banned | admin
    sort   = request.args.get('sort', 'newest')   # newest | oldest | balance | posts
    page   = max(1, safe_int(request.args.get('page'), 1))
    per    = 30
    offset = (page - 1) * per

    conditions = []
    params     = []

    if q:
        conditions.append('(u.username LIKE ? OR u.email LIKE ? OR u.display_name LIKE ?)')
        params += [f'%{q}%', f'%{q}%', f'%{q}%']
    if status == 'banned':
        conditions.append('u.is_banned=1')
    elif status == 'active':
        conditions.append('u.is_banned=0')
    elif status == 'admin':
        conditions.append('u.is_admin=1')

    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

    sort_map = {
        'newest':  'u.created_at DESC',
        'oldest':  'u.created_at ASC',
        'balance': 'u.balance DESC',
        'posts':   'u.post_count DESC',
    }
    order = sort_map.get(sort, 'u.created_at DESC')

    total = db.execute(f'SELECT COUNT(*) FROM users u {where}', params).fetchone()[0]
    users = db.execute(
        f'SELECT u.* FROM users u {where} ORDER BY {order} LIMIT ? OFFSET ?',
        params + [per, offset]
    ).fetchall()

    return render_template('admin/users.html',
        users=users, q=q, status=status, sort=sort,
        page=page, total=total, per=per,
    )


@bp.route('/admin/users/<int:user_id>')
@admin_required
def admin_user_detail(user_id):
    from db import _open_personal_db, _upload_personal_db
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        return render_template('error.html', code=404, message='User not found'), 404

    # Open that user's personal DB for wallet/inbox data
    try:
        pudb, pudb_path = _open_personal_db(user_id)
    except Exception:
        pudb = db  # fallback to global if personal unavailable
        pudb_path = None

    posts = db.execute(
        'SELECT * FROM posts WHERE user_id=? ORDER BY created_at DESC LIMIT 20', (user_id,)
    ).fetchall()
    transactions = pudb.execute(
        'SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 20', (user_id,)
    ).fetchall()
    withdrawals = pudb.execute(
        'SELECT * FROM withdrawals WHERE user_id=? ORDER BY created_at DESC LIMIT 10', (user_id,)
    ).fetchall()
    reports_against = db.execute(
        "SELECT * FROM reports WHERE target_type='user' AND target_id=? ORDER BY created_at DESC LIMIT 10",
        (user_id,)
    ).fetchall()
    ban_record = db.execute(
        'SELECT * FROM user_bans WHERE user_id=? AND is_active=1', (user_id,)
    ).fetchone()
    audit_entries = db.execute(
        "SELECT * FROM admin_audit_log WHERE target_type='user' AND target_id=? "
        'ORDER BY created_at DESC LIMIT 20', (user_id,)
    ).fetchall()

    stats = {
        'follower_count':  db.execute('SELECT COUNT(*) FROM follows WHERE following_id=?', (user_id,)).fetchone()[0],
        'following_count': db.execute('SELECT COUNT(*) FROM follows WHERE follower_id=?',  (user_id,)).fetchone()[0],
        'post_count':      db.execute('SELECT COUNT(*) FROM posts WHERE user_id=?',         (user_id,)).fetchone()[0],
        'like_count':      db.execute('SELECT COALESCE(SUM(like_count),0) FROM posts WHERE user_id=?', (user_id,)).fetchone()[0],
        'total_earned':    db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND type='earning'",
            (user_id,)
        ).fetchone()[0],
        'total_spent':     db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND type IN ('ad_spend','boost_spend')",
            (user_id,)
        ).fetchone()[0],
    }

    # Upload personal DB if we opened it
    if pudb_path:
        try:
            pudb.close()
        except Exception:
            pass
        _upload_personal_db(user_id, pudb_path)

    return render_template('admin/user_detail.html',
        user=dict(user), posts=posts, transactions=transactions,
        withdrawals=withdrawals, reports_against=reports_against,
        ban_record=ban_record, audit_entries=audit_entries, stats=stats,
    )


# ── Posts ─────────────────────────────────────────────────────────────────────

@bp.route('/admin/posts')
@admin_required
def admin_posts():
    db      = get_db()
    q       = (request.args.get('q') or '').strip()
    flagged = request.args.get('flagged', '0') == '1'
    page    = max(1, safe_int(request.args.get('page'), 1))
    per     = 30
    offset  = (page - 1) * per

    if q:
        rows = db.execute("""
            SELECT p.*, u.username, u.avatar_url
            FROM posts p JOIN users u ON u.id=p.user_id
            WHERE p.body LIKE ?
            ORDER BY p.created_at DESC LIMIT ? OFFSET ?
        """, (f'%{q}%', per, offset)).fetchall()
        total = db.execute("SELECT COUNT(*) FROM posts WHERE body LIKE ?", (f'%{q}%',)).fetchone()[0]
    elif flagged:
        rows = db.execute("""
            SELECT p.*, u.username, u.avatar_url
            FROM posts p JOIN users u ON u.id=p.user_id
            WHERE p.id IN (SELECT target_id FROM reports WHERE target_type='post' AND status='open')
            ORDER BY p.created_at DESC LIMIT ? OFFSET ?
        """, (per, offset)).fetchall()
        total = db.execute(
            "SELECT COUNT(*) FROM posts WHERE id IN "
            "(SELECT target_id FROM reports WHERE target_type='post' AND status='open')"
        ).fetchone()[0]
    else:
        rows = db.execute("""
            SELECT p.*, u.username, u.avatar_url
            FROM posts p JOIN users u ON u.id=p.user_id
            ORDER BY p.created_at DESC LIMIT ? OFFSET ?
        """, (per, offset)).fetchall()
        total = db.execute('SELECT COUNT(*) FROM posts').fetchone()[0]

    return render_template('admin/posts.html',
        posts=rows, q=q, flagged=flagged, page=page, total=total, per=per,
    )


# ── Reports ───────────────────────────────────────────────────────────────────

@bp.route('/admin/reports')
@admin_required
def admin_reports():
    db     = get_db()
    status = request.args.get('status', 'open')
    page   = max(1, safe_int(request.args.get('page'), 1))
    per    = 25
    offset = (page - 1) * per

    rows = db.execute("""
        SELECT r.*, u.username as reporter_name, u.avatar_url as reporter_avatar
        FROM reports r JOIN users u ON u.id=r.reporter_id
        WHERE r.status=?
        ORDER BY r.created_at DESC LIMIT ? OFFSET ?
    """, (status, per, offset)).fetchall()
    total = db.execute('SELECT COUNT(*) FROM reports WHERE status=?', (status,)).fetchone()[0]

    counts = {
        'open':      db.execute("SELECT COUNT(*) FROM reports WHERE status='open'").fetchone()[0],
        'reviewing': db.execute("SELECT COUNT(*) FROM reports WHERE status='reviewing'").fetchone()[0],
        'reviewed':  db.execute("SELECT COUNT(*) FROM reports WHERE status='reviewed'").fetchone()[0],
        'dismissed': db.execute("SELECT COUNT(*) FROM reports WHERE status='dismissed'").fetchone()[0],
        'actioned':  db.execute("SELECT COUNT(*) FROM reports WHERE status='actioned'").fetchone()[0],
    }

    return render_template('admin/reports.html',
        reports=rows, status=status, counts=counts, page=page, total=total, per=per,
    )


# ── Withdrawals ───────────────────────────────────────────────────────────────

@bp.route('/admin/withdrawals')
@admin_required
def admin_withdrawals():
    db     = get_db()
    status = request.args.get('status', 'pending')
    page   = max(1, safe_int(request.args.get('page'), 1))
    per    = 25
    offset = (page - 1) * per

    # pending_withdrawals mirrors withdrawal data from personal DBs into global DB
    rows = db.execute("""
        SELECT pw.*, u.username, u.crypto_address, u.crypto_network
        FROM pending_withdrawals pw JOIN users u ON pw.user_id=u.id
        WHERE pw.status=?
        ORDER BY pw.created_at DESC LIMIT ? OFFSET ?
    """, (status, per, offset)).fetchall()
    total = db.execute(
        'SELECT COUNT(*) FROM pending_withdrawals WHERE status=?', (status,)
    ).fetchone()[0]

    counts = {s: db.execute(
        'SELECT COUNT(*) FROM pending_withdrawals WHERE status=?', (s,)
    ).fetchone()[0] for s in ('pending', 'approved', 'rejected', 'failed', 'processing')}

    return render_template('admin/withdrawals.html',
        withdrawals=rows, status=status, counts=counts,
        pending_deposits=[], page=page, total=total, per=per,
    )


# ── Reviews ───────────────────────────────────────────────────────────────────

@bp.route('/admin/reviews')
@admin_required
def admin_reviews():
    db     = get_db()
    status = request.args.get('status', 'published')
    page   = max(1, safe_int(request.args.get('page'), 1))
    per    = 25
    offset = (page - 1) * per

    rows = db.execute("""
        SELECT r.*, u.username, u.avatar_url, u.display_name
        FROM platform_reviews r JOIN users u ON u.id=r.user_id
        WHERE r.status=?
        ORDER BY r.created_at DESC LIMIT ? OFFSET ?
    """, (status, per, offset)).fetchall()
    total = db.execute('SELECT COUNT(*) FROM platform_reviews WHERE status=?', (status,)).fetchone()[0]

    counts = {s: db.execute('SELECT COUNT(*) FROM platform_reviews WHERE status=?', (s,)).fetchone()[0]
              for s in ('published', 'hidden', 'flagged')}
    rating_dist = {i: db.execute(
        "SELECT COUNT(*) FROM platform_reviews WHERE rating=? AND status='published'", (i,)
    ).fetchone()[0] for i in range(1, 6)}
    avg = db.execute(
        "SELECT ROUND(AVG(rating),1) FROM platform_reviews WHERE status='published'"
    ).fetchone()[0] or 0

    return render_template('admin/reviews.html',
        reviews=rows, status=status, counts=counts,
        rating_dist=rating_dist, avg_rating=avg,
        page=page, total=total, per=per,
    )


# ── Audit log ─────────────────────────────────────────────────────────────────

@bp.route('/admin/audit')
@admin_required
def admin_audit():
    db     = get_db()
    page   = max(1, safe_int(request.args.get('page'), 1))
    per    = 50
    offset = (page - 1) * per

    rows = db.execute("""
        SELECT a.*, u.username as admin_name
        FROM admin_audit_log a JOIN users u ON u.id=a.admin_id
        ORDER BY a.created_at DESC LIMIT ? OFFSET ?
    """, (per, offset)).fetchall()
    total = db.execute('SELECT COUNT(*) FROM admin_audit_log').fetchone()[0]

    return render_template('admin/audit.html',
        entries=rows, page=page, total=total, per=per,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ACTION ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@bp.route('/admin/user/<int:user_id>/ban', methods=['POST'])
@admin_required
def ban_user(user_id):
    db         = get_db()
    admin_id   = session['user_id']
    action     = request.json.get('action', 'ban')   # ban | unban
    reason     = (request.json.get('reason') or '').strip()
    duration   = request.json.get('duration')         # days, None=permanent

    user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404
    if user['is_admin']:
        return jsonify({'success': False, 'error': 'Cannot ban an admin account'}), 403

    now = datetime.now(timezone.utc).isoformat()

    if action == 'unban':
        db.execute(
            'UPDATE user_bans SET is_active=0, lifted_at=?, lifted_by=? WHERE user_id=? AND is_active=1',
            (now, admin_id, user_id)
        )
        db.execute('UPDATE users SET is_banned=0, ban_reason=NULL WHERE id=?', (user_id,))
        add_notification(db, user_id, '✅ Your account ban has been lifted. Welcome back!')
        _audit(db, 'unban_user', 'user', user_id, {'reason': 'lifted by admin'})
        db.commit()
        return jsonify({'success': True, 'status': 'unbanned'})

    if not reason:
        return jsonify({'success': False, 'error': 'Reason is required'}), 400

    expires_at = None
    if duration:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=int(duration))).isoformat()

    # Upsert ban record
    db.execute('DELETE FROM user_bans WHERE user_id=?', (user_id,))
    db.execute(
        'INSERT INTO user_bans (user_id,banned_by,reason,expires_at,is_active,created_at) '
        'VALUES (?,?,?,?,1,?)',
        (user_id, admin_id, reason, expires_at, now)
    )
    db.execute('UPDATE users SET is_banned=1, ban_reason=? WHERE id=?', (reason, user_id))
    add_notification(db, user_id,
        f'🚫 Your account has been {"temporarily " if expires_at else "permanently "}suspended. '
        f'Reason: {reason}')
    _audit(db, 'ban_user', 'user', user_id, {'reason': reason, 'duration': duration})
    db.commit()
    return jsonify({'success': True, 'status': 'banned', 'expires_at': expires_at})


@bp.route('/admin/user/<int:user_id>/adjust-balance', methods=['POST'])
@admin_required
def adjust_balance(user_id):
    CURRENCY_SYMBOL = current_app.config['CURRENCY_SYMBOL']
    db     = get_db()
    amount = safe_float(request.json.get('amount'), 0)
    note   = (request.json.get('note') or 'Admin adjustment').strip()

    if amount == 0:
        return jsonify({'success': False, 'error': 'Amount cannot be zero'}), 400

    user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404

    new_balance = max(0, (float(user['balance'] or 0)) + amount)
    db.execute('UPDATE users SET balance=? WHERE id=?', (new_balance, user_id))

    tx_type = 'deposit' if amount > 0 else 'deduction'
    add_transaction(db, user_id, tx_type, abs(amount), f'Admin: {note}')
    add_notification(db, user_id,
        f'{"💰 Admin credited" if amount > 0 else "💸 Admin deducted"} '
        f'{CURRENCY_SYMBOL}{abs(amount):.2f}. Note: {note}')
    _audit(db, 'adjust_balance', 'user', user_id, {'amount': amount, 'note': note})
    db.commit()
    return jsonify({'success': True, 'new_balance': new_balance})


@bp.route('/admin/user/<int:user_id>/reset-password', methods=['POST'])
@admin_required
def reset_user_password(user_id):
    import secrets as _sec
    db       = get_db()
    new_pw   = request.json.get('password') or _sec.token_urlsafe(12)
    user     = db.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404

    db.execute('UPDATE users SET password=? WHERE id=?', (hash_password(new_pw), user_id))
    add_notification(db, user_id,
        '🔑 Your password has been reset by admin. Please log in with your new password.')
    _audit(db, 'reset_password', 'user', user_id)
    db.commit()
    return jsonify({'success': True, 'new_password': new_pw})


@bp.route('/admin/user/<int:user_id>/verify', methods=['POST'])
@admin_required
def toggle_verify(user_id):
    """
    Set/remove verification badge with a specific tier.
    Accepts JSON: { action: 'set' | 'remove', tier: 'gold' | 'blue' | 'grey' }
    Default (no body): toggle on/off using 'blue' tier.
    """
    db   = get_db()
    user = db.execute('SELECT id, is_verified, verified_tier FROM users WHERE id=?',
                      (user_id,)).fetchone()
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404

    data   = request.get_json(silent=True) or {}
    action = (data.get('action') or '').strip().lower()
    tier   = (data.get('tier')   or '').strip().lower()

    ALLOWED_TIERS = {'gold', 'blue', 'grey'}

    if action == 'remove':
        new_verified = 0
        new_tier     = 'blue'   # reset
        msg          = '❌ Verification badge removed.'
    elif action == 'set':
        if tier not in ALLOWED_TIERS:
            return jsonify({'success': False,
                           'error': 'tier must be gold, blue, or grey'}), 400
        new_verified = 1
        new_tier     = tier
        tier_label   = {'gold':'Gold','blue':'Blue','grey':'Government'}[tier]
        msg          = f'✅ Your account has been verified ({tier_label} badge)!'
    else:
        # Legacy toggle behaviour
        new_verified = 0 if user['is_verified'] else 1
        new_tier     = user['verified_tier'] or 'blue'
        msg = '✅ Your account has been verified!' if new_verified else '❌ Verification badge removed.'

    db.execute(
        'UPDATE users SET is_verified=?, verified_tier=? WHERE id=?',
        (new_verified, new_tier, user_id)
    )
    if new_verified:
        add_notification(db, user_id, msg)
    else:
        add_notification(db, user_id, msg)
    _audit(db, 'set_verify', 'user', user_id,
           {'is_verified': new_verified, 'tier': new_tier})
    db.commit()
    return jsonify({
        'success':     True,
        'is_verified': new_verified,
        'tier':        new_tier
    })




@bp.route('/admin/channel/<int:channel_id>/verify', methods=['POST'])
@admin_required
def admin_verify_channel(channel_id):
    """Set or remove verification badge for a channel (gold/blue/grey)."""
    db = get_db()
    ch = db.execute('SELECT id, is_verified FROM channels WHERE id=?', (channel_id,)).fetchone()
    if not ch:
        return jsonify({'success': False, 'error': 'Channel not found'}), 404
    data   = request.get_json(silent=True) or {}
    action = (data.get('action') or '').strip().lower()
    tier   = (data.get('tier') or 'gold').strip().lower()
    if tier not in {'gold','blue','grey'}:
        tier = 'gold'
    if action == 'remove':
        db.execute('UPDATE channels SET is_verified=0 WHERE id=?', (channel_id,))
    else:
        db.execute(
            'UPDATE channels SET is_verified=1, verified_tier=? WHERE id=?',
            (tier, channel_id)
        )
    _audit(db, 'channel_verify', 'channel', channel_id,
           {'action': action or 'set', 'tier': tier})
    db.commit()
    return jsonify({'success': True})


@bp.route('/admin/group/<int:group_id>/verify', methods=['POST'])
@admin_required
def admin_verify_group(group_id):
    """Set or remove verification badge for a group."""
    db = get_db()
    g = db.execute('SELECT id, is_verified FROM groups WHERE id=?', (group_id,)).fetchone()
    if not g:
        return jsonify({'success': False, 'error': 'Group not found'}), 404
    data   = request.get_json(silent=True) or {}
    action = (data.get('action') or '').strip().lower()
    tier   = (data.get('tier') or 'gold').strip().lower()
    if tier not in {'gold','blue','grey'}:
        tier = 'gold'
    if action == 'remove':
        db.execute('UPDATE groups SET is_verified=0 WHERE id=?', (group_id,))
    else:
        db.execute(
            'UPDATE groups SET is_verified=1, verified_tier=? WHERE id=?',
            (tier, group_id)
        )
    _audit(db, 'group_verify', 'group', group_id,
           {'action': action or 'set', 'tier': tier})
    db.commit()
    return jsonify({'success': True})

@bp.route('/admin/user/<int:user_id>/notify', methods=['POST'])
@admin_required
def notify_user(user_id):
    db      = get_db()
    message = (request.json.get('message') or '').strip()
    if not message:
        return jsonify({'success': False, 'error': 'Message required'}), 400
    user = db.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404
    add_notification(db, user_id, f'📢 Admin: {message}')
    _audit(db, 'notify_user', 'user', user_id, {'message': message})
    db.commit()
    return jsonify({'success': True})


@bp.route('/admin/user/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404
    if user['is_admin']:
        return jsonify({'success': False, 'error': 'Cannot delete an admin account'}), 403

    _audit(db, 'delete_user', 'user', user_id,
           {'username': user['username'], 'email': user['email']})

    # Cascade delete — stories get R2 cleanup before DB row removal
    _ustories = db.execute('SELECT * FROM stories WHERE user_id=?', (user_id,)).fetchall()
    for _us in _ustories:
        _admin_delete_story_media(_us)

    for table, col in [
        ('notifications', 'user_id'), ('transactions', 'user_id'),
        ('withdrawals', 'user_id'), ('task_completions', 'worker_id'),
        ('post_likes', 'user_id'), ('bookmarks', 'user_id'),
        ('follows', 'follower_id'), ('follows', 'following_id'),
        ('search_history', 'user_id'), ('post_views', 'user_id'),
        ('poll_votes', 'user_id'), ('stories', 'user_id'),
        ('user_bans', 'user_id'), ('reports', 'reporter_id'),
    ]:
        db.execute(f'DELETE FROM {table} WHERE {col}=?', (user_id,))

    # Delete all user posts — R2 media + cascade tables
    _uposts = db.execute('SELECT * FROM posts WHERE user_id=?', (user_id,)).fetchall()
    for _up in _uposts:
        _admin_delete_post_media(_up)
        _pid = _up['id']
        for t in ('post_likes', 'bookmarks', 'poll_options', 'poll_votes',
                  'post_hashtags', 'channel_posts', 'post_views',
                  'boost_engagements', 'post_boosts'):
            try:
                db.execute(f'DELETE FROM {t} WHERE post_id=?', (_pid,))
            except Exception:
                pass
    db.execute('DELETE FROM posts WHERE user_id=?', (user_id,))
    db.execute('DELETE FROM channel_members WHERE user_id=?', (user_id,))
    db.execute('DELETE FROM group_members WHERE user_id=?', (user_id,))

    convs = [r[0] for r in db.execute(
        'SELECT id FROM conversations WHERE user_a=? OR user_b=?', (user_id, user_id)
    ).fetchall()]
    for cid in convs:
        db.execute('DELETE FROM messages WHERE conversation_id=?', (cid,))
        db.execute('DELETE FROM conversations WHERE id=?', (cid,))

    db.execute('DELETE FROM users WHERE id=?', (user_id,))
    db.commit()
    return jsonify({'success': True})


@bp.route('/admin/post/<int:post_id>/delete', methods=['POST'])
@admin_required
def admin_delete_post(post_id):
    db   = get_db()
    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Post not found'}), 404

    # Delete R2 media first (while URL is still available)
    _admin_delete_post_media(post)

    # Cascade DB tables
    for t in ('post_likes', 'bookmarks', 'poll_options', 'poll_votes',
              'post_hashtags', 'channel_posts', 'post_views',
              'boost_engagements', 'post_boosts'):
        try:
            db.execute(f'DELETE FROM {t} WHERE post_id=?', (post_id,))
        except Exception:
            pass

    # Delete replies to this post (and their media)
    reply_rows = db.execute('SELECT * FROM posts WHERE reply_to_id=?', (post_id,)).fetchall()
    for rp in reply_rows:
        _admin_delete_post_media(rp)
        for t in ('post_likes', 'bookmarks', 'post_views', 'post_hashtags'):
            try:
                db.execute(f'DELETE FROM {t} WHERE post_id=?', (rp['id'],))
            except Exception:
                pass
        db.execute('DELETE FROM posts WHERE id=?', (rp['id'],))

    db.execute('DELETE FROM posts WHERE id=?', (post_id,))
    db.execute('UPDATE users SET post_count=MAX(0,post_count-1) WHERE id=?',
               (post['user_id'],))
    add_notification(db, post['user_id'],
        'Your post was removed by a moderator for violating community guidelines.',
        icon='system')
    _audit(db, 'delete_post', 'post', post_id, {'user_id': post['user_id']})
    db.commit()
    return jsonify({'success': True})


@bp.route('/admin/report/<int:report_id>/action', methods=['POST'])
@admin_required
def action_report(report_id):
    db          = get_db()
    admin_id    = session['user_id']
    action      = request.json.get('action')   # reviewing | dismiss | warn | delete | ban
    note        = (request.json.get('note') or '').strip()

    report = db.execute('SELECT * FROM reports WHERE id=?', (report_id,)).fetchone()
    if not report:
        return jsonify({'success': False, 'error': 'Report not found'}), 404

    now = datetime.now(timezone.utc).isoformat()

    if action == 'reviewing':
        db.execute(
            'UPDATE reports SET status=\'reviewing\', reviewed_by=?, reviewed_at=? WHERE id=?',
            (admin_id, now, report_id)
        )
        if note:
            db.execute('UPDATE reports SET notes=? WHERE id=?', (note, report_id))
        db.commit()
        return jsonify({'success': True, 'new_status': 'reviewing'})

    new_status = 'dismissed' if action == 'dismiss' else 'actioned'
    db.execute(
        'UPDATE reports SET status=?, reviewed_by=?, reviewed_at=?, action_taken=?, notes=? WHERE id=?',
        (new_status, admin_id, now, action, note or None, report_id)
    )

    if action == 'delete' and report['target_type'] == 'post':
        post = db.execute('SELECT * FROM posts WHERE id=?', (report['target_id'],)).fetchone()
        if post:
            for t in ('post_likes', 'bookmarks', 'poll_options', 'poll_votes',
                      'post_hashtags', 'channel_posts'):
                db.execute(f'DELETE FROM {t} WHERE post_id=?', (report['target_id'],))
            _rpt_post = db.execute('SELECT * FROM posts WHERE id=?', (report['target_id'],)).fetchone()
            if _rpt_post:
                _admin_delete_post_media(_rpt_post)
                for _t in ('post_likes', 'bookmarks', 'poll_options', 'poll_votes',
                           'post_hashtags', 'channel_posts', 'post_views'):
                    try:
                        db.execute(f'DELETE FROM {_t} WHERE post_id=?', (report['target_id'],))
                    except Exception:
                        pass
            db.execute('DELETE FROM posts WHERE id=?', (report['target_id'],))
            add_notification(db, post['user_id'],
                '🗑️ Your post was removed following a community report.')

    elif action == 'warn' and report['target_type'] in ('user', 'post'):
        target_user_id = (report['target_id'] if report['target_type'] == 'user'
                          else db.execute('SELECT user_id FROM posts WHERE id=?',
                                          (report['target_id'],)).fetchone()['user_id'])
        add_notification(db, target_user_id,
            f'⚠️ You received a community guideline warning. {note or "Please review our terms."}')

    elif action == 'ban':
        target_user_id = (report['target_id'] if report['target_type'] == 'user'
                          else db.execute('SELECT user_id FROM posts WHERE id=?',
                                          (report['target_id'],)).fetchone()['user_id'])
        ban_reason = note or 'Violation of community guidelines'
        db.execute('DELETE FROM user_bans WHERE user_id=?', (target_user_id,))
        db.execute(
            'INSERT INTO user_bans (user_id,banned_by,reason,is_active,created_at) VALUES (?,?,?,1,?)',
            (target_user_id, admin_id, ban_reason, now)
        )
        db.execute('UPDATE users SET is_banned=1, ban_reason=? WHERE id=?',
                   (ban_reason, target_user_id))
        add_notification(db, target_user_id, f'🚫 Account suspended: {ban_reason}')

    _audit(db, f'report_{action}', report['target_type'], report['target_id'],
           {'report_id': report_id, 'note': note})
    db.commit()
    return jsonify({'success': True, 'new_status': new_status})


@bp.route('/admin/withdrawal/<int:wdr_id>/<action>', methods=['POST'])
@admin_required
def process_withdrawal(wdr_id, action):
    CURRENCY_SYMBOL = current_app.config['CURRENCY_SYMBOL']
    db = get_db()
    if action not in ('approve', 'reject'):
        return jsonify({'success': False, 'error': 'Invalid action.'}), 400

    # Read from the global mirror table
    wr = db.execute('SELECT * FROM pending_withdrawals WHERE id=?', (wdr_id,)).fetchone()
    if not wr:
        return jsonify({'success': False, 'error': 'Not found.'}), 404
    if wr['status'] not in ('pending', 'failed', 'processing'):
        return jsonify({'success': False, 'error': 'Already processed.'})

    now = datetime.now(timezone.utc).isoformat()

    # Open the user's personal DB to update the canonical withdrawal record
    from db import _open_personal_db, _upload_personal_db
    personal_conn, personal_path = _open_personal_db(wr['user_id'])
    try:
        if action == 'reject':
            reason = (request.json or {}).get('reason', '')
            db.execute(
                'UPDATE pending_withdrawals SET status=?, processed_at=?, failure_reason=? WHERE id=?',
                ('rejected', now, reason, wdr_id)
            )
            personal_conn.execute(
                'UPDATE withdrawals SET status=?, processed_at=?, failure_reason=? WHERE id=?',
                ('rejected', now, reason, wr['personal_wdr_id'])
            )
            personal_conn.commit()
            db.execute('UPDATE users SET balance=balance+? WHERE id=?', (wr['amount'], wr['user_id']))
            add_notification(db, wr['user_id'],
                f'❌ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} rejected. Amount refunded.'
                + (f' Reason: {reason}' if reason else ''))
            _audit(db, 'reject_withdrawal', 'withdrawal', wdr_id,
                   {'amount': wr['amount'], 'reason': reason})
        else:
            db.execute(
                'UPDATE pending_withdrawals SET status=?, processed_at=? WHERE id=?',
                ('approved', now, wdr_id)
            )
            personal_conn.execute(
                'UPDATE withdrawals SET status=?, processed_at=? WHERE id=?',
                ('approved', now, wr['personal_wdr_id'])
            )
            personal_conn.commit()
            add_notification(db, wr['user_id'],
                f'✅ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} approved and sent!')
            _audit(db, 'approve_withdrawal', 'withdrawal', wdr_id, {'amount': wr['amount']})

        db.commit()
    finally:
        personal_conn.close()
        _upload_personal_db(wr['user_id'], personal_path)

    return jsonify({'success': True, 'status': 'approved' if action == 'approve' else 'rejected'})


@bp.route('/admin/deposit', methods=['POST'])
@bp.route('/admin/deposit_user', methods=['POST'])
@admin_required
def admin_deposit():
    CURRENCY_SYMBOL = current_app.config['CURRENCY_SYMBOL']
    db      = get_db()
    user_id = safe_int((request.json or request.form).get('user_id'), 0)
    amount  = safe_float((request.json or request.form).get('amount'), 0)
    note    = ((request.json or request.form).get('note') or 'Admin deposit').strip()

    if user_id <= 0 or amount <= 0:
        return jsonify({'success': False, 'error': 'Invalid input.'}), 400
    target = db.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone()
    if not target:
        return jsonify({'success': False, 'error': 'User not found.'}), 404

    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))
    add_transaction(db, user_id, 'deposit', amount, f'Admin: {note}')
    add_notification(db, user_id, f'💰 Admin credited {CURRENCY_SYMBOL}{amount:.2f}. Note: {note}')
    _audit(db, 'manual_deposit', 'user', user_id, {'amount': amount, 'note': note})
    db.commit()
    return jsonify({'success': True})


@bp.route('/admin/review/<int:review_id>/action', methods=['POST'])
@admin_required
def action_review(review_id):
    db     = get_db()
    action = request.json.get('action')   # hide | feature | unfeature | reply
    reply  = (request.json.get('reply') or '').strip()

    review = db.execute('SELECT * FROM platform_reviews WHERE id=?', (review_id,)).fetchone()
    if not review:
        return jsonify({'success': False, 'error': 'Review not found'}), 404

    now = datetime.now(timezone.utc).isoformat()
    if action == 'hide':
        db.execute("UPDATE platform_reviews SET status='hidden', updated_at=? WHERE id=?",
                   (now, review_id))
    elif action == 'flag':
        db.execute("UPDATE platform_reviews SET status='flagged', updated_at=? WHERE id=?",
                   (now, review_id))
    elif action in ('feature', 'unfeature'):
        db.execute('UPDATE platform_reviews SET is_featured=?, updated_at=? WHERE id=?',
                   (1 if action == 'feature' else 0, now, review_id))
    elif action == 'reply' and reply:
        db.execute('UPDATE platform_reviews SET admin_reply=?, updated_at=? WHERE id=?',
                   (reply, now, review_id))
    elif action == 'restore':
        db.execute("UPDATE platform_reviews SET status='published', updated_at=? WHERE id=?",
                   (now, review_id))

    _audit(db, f'review_{action}', 'review', review_id)
    db.commit()
    return jsonify({'success': True})


@bp.route('/admin/broadcast', methods=['POST'])
@admin_required
def broadcast():
    db      = get_db()
    message = (request.json.get('message') or '').strip()
    segment = request.json.get('segment', 'all')   # all | active | banned
    if not message:
        return jsonify({'success': False, 'error': 'Message is required'}), 400

    if segment == 'active':
        users = db.execute("SELECT id FROM users WHERE is_banned=0").fetchall()
    elif segment == 'banned':
        users = db.execute("SELECT id FROM users WHERE is_banned=1").fetchall()
    else:
        users = db.execute("SELECT id FROM users").fetchall()

    for u in users:
        add_notification(db, u['id'], f'📢 {message}')

    _audit(db, 'broadcast', details={'message': message, 'segment': segment,
                                      'count': len(users)})
    db.commit()
    return jsonify({'success': True, 'sent_to': len(users)})


@bp.route('/admin/send_notification', methods=['POST'])
@admin_required
def send_notification():
    """Legacy endpoint kept for compatibility."""
    db      = get_db()
    message = (request.form.get('message') or '').strip()
    user_id = safe_int(request.form.get('user_id'), 0)
    if not message:
        return jsonify({'success': False, 'error': 'Message cannot be empty.'}), 400
    if user_id:
        user = db.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found.'}), 404
        add_notification(db, user_id, f'📢 {message}')
    else:
        for u in db.execute('SELECT id FROM users').fetchall():
            add_notification(db, u['id'], f'📢 {message}')
    db.commit()
    return jsonify({'success': True})
