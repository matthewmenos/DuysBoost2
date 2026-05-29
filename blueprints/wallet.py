"""blueprints/wallet.py — deposit, withdraw, crypto address, referrals, notifications."""
from datetime import datetime, timezone
from flask import (
    Blueprint, jsonify, redirect, render_template,
    request, session, url_for, current_app
)
from helpers import (
    get_db, get_user_db, login_required, safe_float, safe_int,
    add_notification, add_transaction, check_and_award_referral_bonus
)
from security import limiter, csrf_exempt, LIMIT_WITHDRAW, LIMIT_DEPOSIT

bp = Blueprint('wallet', __name__)


@bp.route('/wallet')
@login_required
def wallet():
    db  = get_db()     # global (user balance)
    udb = get_user_db()  # personal (transactions, withdrawals)
    uid = session['user_id']
    txs = udb.execute(
        'SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC', (uid,)
    ).fetchall()
    wdrs = udb.execute(
        'SELECT * FROM withdrawals WHERE user_id=? ORDER BY created_at DESC', (uid,)
    ).fetchall()
    pending_deposits = udb.execute(
        'SELECT * FROM crypto_deposits WHERE user_id=? ORDER BY created_at DESC LIMIT 10', (uid,)
    ).fetchall()
    return render_template('wallet.html', transactions=txs, withdrawals=wdrs,
                           pending_deposits=pending_deposits)


@bp.route('/wallet/deposit', methods=['POST'])
@login_required
@limiter.limit(LIMIT_DEPOSIT)
@csrf_exempt   # JSON POST — protected by SameSite=Lax
def deposit():
    """Auto on-chain deposit verification — no admin needed."""
    from crypto_engine import verify_deposit as _chain_verify_deposit
    db  = get_db()
    udb = get_user_db()
    uid = session['user_id']

    CRYPTO_NETWORKS = current_app.config['CRYPTO_NETWORKS']
    CRYPTO_WALLETS  = current_app.config['CRYPTO_WALLETS']

    payload  = request.get_json(silent=True) or {}
    network  = (payload.get('network') or '').strip().lower()
    tx_hash  = (payload.get('tx_hash') or '').strip()

    if network not in CRYPTO_NETWORKS:
        return jsonify({'success': False, 'error': 'Invalid network selected.'}), 400
    if not tx_hash or len(tx_hash) < 10:
        return jsonify({'success': False, 'error': 'Please enter a valid transaction hash.'}), 400

    # crypto_deposits lives in personal DB
    existing = udb.execute(
        'SELECT id, status FROM crypto_deposits WHERE tx_hash=?', (tx_hash,)
    ).fetchone()
    if existing:
        if existing['status'] == 'confirmed':
            return jsonify({'success': False,
                            'error': 'This transaction has already been credited.'}), 400
        dep_id = existing['id']
    else:
        dep_id = None

    platform_wallet = CRYPTO_WALLETS.get(network, '')
    if not platform_wallet:
        return jsonify({'success': False,
                        'error': f'Platform wallet not configured for {network}.'}), 500

    net_label = CRYPTO_NETWORKS[network]['label']
    now = datetime.now(timezone.utc).isoformat()

    if dep_id:
        udb.execute('UPDATE crypto_deposits SET status=? WHERE id=?', ('verifying', dep_id))
    else:
        udb.execute(
            'INSERT INTO crypto_deposits (user_id, network, tx_hash, amount, status, created_at) '
            'VALUES (?,?,?,0,?,?)',
            (uid, network, tx_hash, 'verifying', now)
        )
        dep_id = udb.execute('SELECT last_insert_rowid()').fetchone()[0]
    udb.commit()

    result = _chain_verify_deposit(
        network=network,
        tx_hash=tx_hash,
        expected_recipient=platform_wallet,
        min_amount_usd=0.01,
    )

    if not result['ok']:
        udb.execute('UPDATE crypto_deposits SET status=? WHERE id=?', ('failed', dep_id))
        udb.commit()
        return jsonify({'success': False, 'error': result['error']}), 400

    verified_amount = round(result['amount'], 6)

    udb.execute(
        'UPDATE crypto_deposits SET status=?, amount=?, confirmed_at=? WHERE id=?',
        ('confirmed', verified_amount, datetime.now(timezone.utc).isoformat(), dep_id)
    )
    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (verified_amount, uid))
    add_transaction(db, uid, 'deposit', verified_amount,
                    f'USDT deposit via {net_label} — TX: {tx_hash[:24]}...')
    check_and_award_referral_bonus(db, uid)
    add_notification(db, uid,
        f'✅ Deposit confirmed on-chain! '
        f'${verified_amount:.2f} USDT via {net_label} added to your balance.')
    db.commit()
    udb.commit()

    updated_balance = db.execute(
        'SELECT balance FROM users WHERE id=?', (uid,)
    ).fetchone()['balance']

    return jsonify({
        'success': True,
        'message': f'${verified_amount:.2f} USDT confirmed and credited to your balance!',
        'amount': verified_amount,
        'balance': updated_balance,
    })


@bp.route('/wallet/crypto_address', methods=['POST'])
@login_required
@csrf_exempt
def save_crypto_address():
    CRYPTO_NETWORKS = current_app.config['CRYPTO_NETWORKS']
    db      = get_db()
    uid     = session['user_id']
    network = request.form.get('network', '').strip().lower()
    address = request.form.get('address', '').strip()
    name    = request.form.get('name', '').strip()

    if network not in CRYPTO_NETWORKS:
        return jsonify({'success': False, 'error': 'Invalid network selected.'})
    if not address or len(address) < 10:
        return jsonify({'success': False, 'error': 'Please enter a valid wallet address.'})
    if not name or len(name) < 2:
        return jsonify({'success': False, 'error': 'Please enter the account holder name.'})

    db.execute(
        'UPDATE users SET crypto_network=?, crypto_address=?, crypto_name=? WHERE id=?',
        (network, address, name, uid)
    )
    add_notification(db, uid,
        f'✅ Withdrawal address saved: {CRYPTO_NETWORKS[network]["label"]} • {address[:12]}...')
    db.commit()
    return jsonify({
        'success': True,
        'network': network,
        'network_label': CRYPTO_NETWORKS[network]['label'],
        'address': address,
        'name': name,
    })


@bp.route('/wallet/crypto_address', methods=['DELETE'])
@login_required
def remove_crypto_address():
    db = get_db()
    db.execute(
        'UPDATE users SET crypto_network=NULL, crypto_address=NULL, crypto_name=NULL WHERE id=?',
        (session['user_id'],)
    )
    db.commit()
    return jsonify({'success': True})


@bp.route('/wallet/withdraw', methods=['POST'])
@login_required
@limiter.limit(LIMIT_WITHDRAW)
@csrf_exempt
def withdraw():
    """Automatic on-chain withdrawal."""
    from crypto_engine import send_usdt as _chain_send_usdt
    db  = get_db()
    udb = get_user_db()
    uid = session['user_id']

    CRYPTO_NETWORKS = current_app.config['CRYPTO_NETWORKS']
    WITHDRAWAL_KEYS = current_app.config['WITHDRAWAL_KEYS']
    CURRENCY_SYMBOL = current_app.config['CURRENCY_SYMBOL']

    amount = safe_float(request.form.get('amount'), 0)
    user   = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

    if amount <= 0:
        return jsonify({'success': False, 'error': 'Enter a valid amount.'})
    if amount < 1:
        return jsonify({'success': False,
                        'error': f'Minimum withdrawal is {CURRENCY_SYMBOL}1.00.'})
    if not user['crypto_address']:
        return jsonify({'success': False,
                        'error': 'Please add a crypto withdrawal address first.'}), 400
    if amount > user['balance']:
        return jsonify({'success': False, 'error': 'Insufficient balance.'})

    network       = user['crypto_network'] or ''
    network_label = CRYPTO_NETWORKS.get(network, {}).get('label', 'Crypto') if network else 'Crypto'
    to_address    = user['crypto_address'] or ''

    if network not in CRYPTO_NETWORKS:
        return jsonify({'success': False, 'error': 'Invalid withdrawal network on your account.'})

    private_key = WITHDRAWAL_KEYS.get(network, '')
    if not private_key:
        return jsonify({'success': False,
                        'error': (f'Automatic withdrawals via {network_label} are temporarily '
                                  'unavailable. Please contact support.')}), 503

    method = f'USDT ({network_label})'
    db.execute('UPDATE users SET balance=balance-? WHERE id=?', (amount, uid))

    # Write to user's personal DB (withdrawals lives here)
    wdr_cur = udb.execute(
        'INSERT INTO withdrawals (user_id,amount,method,account,network,status) '
        'VALUES (?,?,?,?,?,?)',
        (uid, amount, method, to_address, network, 'processing')
    )
    wdr_id = wdr_cur.lastrowid

    # Mirror to global DB so admin can see it without opening every personal DB
    db.execute(
        'INSERT INTO pending_withdrawals '
        '(user_id,personal_wdr_id,amount,method,account,network,status) '
        'VALUES (?,?,?,?,?,?,?)',
        (uid, wdr_id, amount, method, to_address, network, 'processing')
    )

    add_transaction(udb, uid, 'withdrawal', amount,
                    f'Withdrawal via USDT {network_label}', status='processing')
    add_notification(db, uid,
        f'⏳ Sending {CURRENCY_SYMBOL}{amount:.2f} USDT via {network_label} to your wallet…')
    db.commit()
    udb.commit()

    result = _chain_send_usdt(
        network=network,
        private_key=private_key,
        to_address=to_address,
        amount_usd=amount,
    )

    now = datetime.now(timezone.utc).isoformat()

    if result['ok']:
        tx_hash = result['tx_hash']
        udb.execute(
            'UPDATE withdrawals SET status=?, tx_hash=?, processed_at=? WHERE id=?',
            ('approved', tx_hash, now, wdr_id)
        )
        db.execute(
            'UPDATE pending_withdrawals SET status=?, tx_hash=?, processed_at=? '
            'WHERE personal_wdr_id=? AND user_id=?',
            ('approved', tx_hash, now, wdr_id, uid)
        )
        udb.execute(
            "UPDATE transactions SET status='completed' "
            "WHERE user_id=? AND type='withdrawal' AND status='processing' "
            "ORDER BY id DESC LIMIT 1",
            (uid,)
        )
        add_notification(db, uid,
            f'✅ {CURRENCY_SYMBOL}{amount:.2f} USDT sent on {network_label}! '
            f'TX: {tx_hash[:24]}...')
        db.commit()
        udb.commit()

        updated_balance = db.execute(
            'SELECT balance FROM users WHERE id=?', (uid,)
        ).fetchone()['balance']
        return jsonify({
            'success': True,
            'message': f'{CURRENCY_SYMBOL}{amount:.2f} USDT sent successfully!',
            'tx_hash': tx_hash,
            'balance': updated_balance,
        })
    else:
        udb.execute(
            'UPDATE withdrawals SET status=?, failure_reason=?, processed_at=? WHERE id=?',
            ('failed', result['error'], now, wdr_id)
        )
        db.execute(
            'UPDATE pending_withdrawals SET status=?, failure_reason=?, processed_at=? '
            'WHERE personal_wdr_id=? AND user_id=?',
            ('failed', result['error'], now, wdr_id, uid)
        )
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, uid))
        udb.execute(
            "UPDATE transactions SET status='failed' "
            "WHERE user_id=? AND type='withdrawal' AND status='processing' "
            "ORDER BY id DESC LIMIT 1",
            (uid,)
        )
        add_notification(db, uid,
            f'❌ Withdrawal of {CURRENCY_SYMBOL}{amount:.2f} USDT failed: {result["error"][:80]}. '
            f'Your balance has been refunded.')
        db.commit()
        udb.commit()
        return jsonify({
            'success': False,
            'error': f'On-chain transfer failed: {result["error"]}',
        }), 502


@bp.route('/referral')
@login_required
def referral():
    db  = get_db()
    uid = session['user_id']
    udb = get_user_db()
    REFERRAL_BONUS = current_app.config['REFERRAL_BONUS']

    referred_users = [dict(u) for u in db.execute(
        'SELECT id, username, display_name, avatar_url, created_at, referral_bonus_awarded '
        'FROM users WHERE referred_by=? ORDER BY created_at DESC', (uid,)
    ).fetchall()]

    total_referred  = len(referred_users)
    total_activated = sum(1 for u in referred_users if u.get('referral_bonus_awarded'))
    total_earned    = total_activated * REFERRAL_BONUS

    # Referral bonus payout history from personal transactions
    try:
        payout_history = [dict(r) for r in udb.execute(
            "SELECT * FROM transactions WHERE type='referral_bonus' ORDER BY created_at DESC LIMIT 20"
        ).fetchall()]
    except Exception:
        payout_history = []

    # Click count
    me = db.execute('SELECT referral_click_count FROM users WHERE id=?', (uid,)).fetchone()
    click_count = me['referral_click_count'] if me and me['referral_click_count'] else 0

    return render_template('referral.html',
                           referred_users=referred_users,
                           total_earned=total_earned,
                           total_referred=total_referred,
                           total_activated=total_activated,
                           payout_history=payout_history,
                           click_count=click_count)


@bp.route('/notifications')
@login_required
def notifications():
    db  = get_db()
    uid = session['user_id']
    try:
        udb = get_user_db()
        rows = udb.execute(
            'SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC', (uid,)
        ).fetchall()
    except Exception:
        rows = []
    # Build clean dicts with stripped emoji
    notifs = []
    for n in rows:
        d = dict(n)
        d['clean_msg'] = _strip_leading_emoji(d.get('message') or '')
        d['icon'] = d.get('icon') or 'system'
        notifs.append(d)
    try:
        udb = get_user_db()
        udb.execute('UPDATE notifications SET read=1 WHERE user_id=?', (uid,))
        udb.commit()
    except Exception:
        pass
    return render_template('notifications.html', notifications=notifs)


@bp.route('/api/notifications/unread')
@login_required
def unread_count():
    uid = session['user_id']
    # notifications live in the personal DB (udb), NOT in global db
    count  = 0
    recent = []
    try:
        udb = get_user_db()
        _c = udb.execute(
            'SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0', (uid,)
        ).fetchone()
        count = _c[0] if _c else 0
        recent = udb.execute(
            'SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 8',
            (uid,)
        ).fetchall()
    except Exception:
        count  = 0
        recent = []
    out = []
    for n in recent:
        d = dict(n)
        out.append({
            'id':    d['id'],
            'msg':   _strip_leading_emoji(d['message']),
            'icon':  d.get('icon') or 'system',
            'link':  d.get('link'),
            'read':  d.get('read', 0),
            'time':  d['created_at'][:16] if d.get('created_at') else ''
        })
    return jsonify({'count': count, 'recent': out})


def _strip_leading_emoji(text):
    """Remove the first emoji + spaces from a notification message."""
    if not text: return text
    import re as _re
    return _re.sub(r'^[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]+\s*', '', text)


@bp.route('/api/notifications/<int:notif_id>/read', methods=['POST'])
@login_required
def mark_notif_read(notif_id):
    db  = get_db()
    uid = session['user_id']
    try:
        udb = get_user_db()
        udb.execute('UPDATE notifications SET read=1 WHERE id=? AND user_id=?',
                   (notif_id, uid))
        udb.commit()
    except Exception:
        pass
    return jsonify({'success': True})


@bp.route('/api/notifications/mark-all-read', methods=['POST'])
@login_required
def mark_all_notif_read():
    db  = get_db()
    uid = session['user_id']
    try:
        udb = get_user_db()
        udb.execute('UPDATE notifications SET read=1 WHERE user_id=? AND read=0', (uid,))
        udb.commit()
    except Exception:
        pass
    return jsonify({'success': True})


@bp.route('/api/theme', methods=['POST'])
@login_required
def toggle_theme():
    db  = get_db()
    uid = session['user_id']
    user = db.execute('SELECT theme FROM users WHERE id=?', (uid,)).fetchone()
    new_theme = 'light' if user['theme'] == 'dark' else 'dark'
    db.execute('UPDATE users SET theme=? WHERE id=?', (new_theme, uid))
    db.commit()
    return jsonify({'theme': new_theme})
