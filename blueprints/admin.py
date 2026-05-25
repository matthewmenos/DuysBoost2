"""blueprints/admin.py — admin panel and management routes."""
from datetime import datetime, timezone
from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for, current_app
from helpers import (
    get_db, login_required, admin_required,
    safe_float, safe_int, add_notification, add_transaction
)

bp = Blueprint('admin', __name__)


@bp.route('/admin')
@login_required
def admin():
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not user['is_admin']:
        return redirect(url_for('boost.dashboard'))

    users           = db.execute('SELECT * FROM users ORDER BY created_at DESC').fetchall()
    wdrs            = db.execute('SELECT w.*, u.username FROM withdrawals w JOIN users u ON w.user_id=u.id '
                                 'WHERE w.status IN ("pending","failed") ORDER BY w.created_at DESC').fetchall()
    recent_wdrs     = db.execute('SELECT w.*, u.username FROM withdrawals w JOIN users u ON w.user_id=u.id '
                                 'WHERE w.status IN ("approved","rejected","processing") '
                                 'ORDER BY w.created_at DESC LIMIT 20').fetchall()
    all_ads         = db.execute('SELECT a.*, u.username as owner_name FROM ads a '
                                 'JOIN users u ON a.user_id=u.id ORDER BY a.created_at DESC').fetchall()
    pending_deposits = db.execute('SELECT cd.*, u.username FROM crypto_deposits cd '
                                  'JOIN users u ON cd.user_id=u.id '
                                  'ORDER BY cd.created_at DESC LIMIT 50').fetchall()
    total_users     = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    total_ads_count = db.execute('SELECT COUNT(*) FROM ads').fetchone()[0]
    total_vol       = db.execute('SELECT COALESCE(SUM(amount),0) FROM transactions').fetchone()[0]

    return render_template('admin.html',
                           users=users, withdrawals=wdrs, recent_withdrawals=recent_wdrs,
                           ads=all_ads, pending_deposits=pending_deposits,
                           total_users=total_users, total_ads=total_ads_count, total_vol=total_vol)


@bp.route('/admin/withdrawal/<int:wdr_id>/<action>', methods=['POST'])
@admin_required
def process_withdrawal(wdr_id, action):
    CURRENCY_SYMBOL = current_app.config['CURRENCY_SYMBOL']
    db = get_db()
    if action not in ('approve', 'reject'):
        return jsonify({'success': False, 'error': 'Invalid action.'}), 400
    wr = db.execute('SELECT * FROM withdrawals WHERE id=?', (wdr_id,)).fetchone()
    if not wr:
        return jsonify({'success': False, 'error': 'Not found.'}), 404
    if wr['status'] not in ('pending', 'failed'):
        return jsonify({'success': False, 'error': 'Already processed.'})

    now = datetime.now(timezone.utc).isoformat()
    if action == 'reject':
        db.execute('UPDATE withdrawals SET status=?, processed_at=? WHERE id=?', ('rejected', now, wdr_id))
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (wr['amount'], wr['user_id']))
        add_notification(db, wr['user_id'],
            f'❌ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} rejected. Amount refunded.')
        db.commit()
        return jsonify({'success': True, 'status': 'rejected'})

    db.execute('UPDATE withdrawals SET status=?, processed_at=? WHERE id=?', ('approved', now, wdr_id))
    add_notification(db, wr['user_id'],
        f'✅ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} USDT approved. '
        f'Payment will be sent to your crypto address.')
    db.commit()
    return jsonify({'success': True, 'status': 'approved'})


@bp.route('/admin/deposit_user', methods=['POST'])
@bp.route('/admin/deposit', methods=['POST'])
@admin_required
def admin_deposit():
    CURRENCY_SYMBOL = current_app.config['CURRENCY_SYMBOL']
    db      = get_db()
    user_id = safe_int(request.form.get('user_id'), 0)
    amount  = safe_float(request.form.get('amount'), 0)
    if user_id <= 0 or amount <= 0:
        return jsonify({'success': False, 'error': 'Invalid input.'}), 400
    target = db.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone()
    if not target:
        return jsonify({'success': False, 'error': 'User not found.'}), 404

    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))
    add_transaction(db, user_id, 'deposit', amount, 'Admin deposit')
    add_notification(db, user_id, f'💰 Admin credited {CURRENCY_SYMBOL}{amount:.2f} to your account!')
    db.commit()
    return jsonify({'success': True})


@bp.route('/admin/send_notification', methods=['POST'])
@admin_required
def send_notification():
    db      = get_db()
    message = request.form.get('message', '').strip()
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
