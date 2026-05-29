"""blueprints/auth.py — signup, login, logout, Google OAuth."""
import secrets
from flask import (
    Blueprint, redirect, render_template, request,
    session, url_for, jsonify, current_app
)
from helpers import (
    get_db, hash_password, verify_password, maybe_upgrade_password_hash,
    add_notification, login_required
)
from security import limiter, csrf_exempt, LIMIT_LOGIN, LIMIT_SIGNUP

bp = Blueprint('auth', __name__)


@bp.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('social.feed'))
    return render_template('index.html')


@bp.route('/signup', methods=['GET', 'POST'])
@limiter.limit(LIMIT_SIGNUP)
def signup():
    if request.method == 'POST':
        db           = get_db()
        username     = request.form.get('username', '').strip()
        display_name = request.form.get('display_name', '').strip()[:60]
        email        = request.form.get('email', '').strip().lower()
        password     = request.form.get('password', '')
        confirm      = request.form.get('confirm_password', '')
        ref_code     = request.form.get('referral_code', '').strip()

        errors = []
        if len(username) < 3:
            errors.append('Username must be at least 3 characters.')
        if not display_name or len(display_name) < 1:
            errors.append('Display name is required.')
        if not username.replace('_', '').replace('-', '').isalnum():
            errors.append('Username may only contain letters, numbers, _ and -.')
        if '@' not in email or '.' not in email.split('@')[-1]:
            errors.append('Please enter a valid email address.')
        if len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        if (not any(c.islower() for c in password)
                or not any(c.isupper() for c in password)
                or not any(c.isdigit() for c in password)):
            errors.append('Password must include upper, lower case letters and a number.')
        if password != confirm:
            errors.append('Passwords do not match.')
        if db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
            errors.append('Email already registered.')
        if db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
            errors.append('Username already taken.')
        if errors:
            return jsonify({'success': False, 'errors': errors})

        referrer = (
            db.execute('SELECT * FROM users WHERE referral_code=?', (ref_code,)).fetchone()
            if ref_code else None
        )
        db.execute(
            'INSERT INTO users (username,display_name,email,password,referred_by,referral_code) '
            'VALUES (?,?,?,?,?,?)',
            (username, display_name or username, email, hash_password(password),
             referrer['id'] if referrer else None, username)
        )
        db.commit()
        user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if referrer:
            CURRENCY_SYMBOL = current_app.config['CURRENCY_SYMBOL']
            add_notification(
                db, referrer['id'],
                f'👤 {username} signed up using your referral code! '
                f'Bonus will be awarded when they activate their account by spending {CURRENCY_SYMBOL}1.'
            )
        add_notification(db, user['id'], '👋 Welcome to DUYS Boost! Your account is ready.')
        db.commit()
        session['user_id'] = user['id']
        # New accounts via Google are never admin
        return jsonify({'success': True, 'redirect': url_for('social.feed')})
    # Track referral link click (GET with ?ref= param)
    ref_param = request.args.get('ref', '').strip()
    if ref_param and request.method == 'GET':
        try:
            db = get_db()
            db.execute(
                'UPDATE users SET referral_click_count=COALESCE(referral_click_count,0)+1 '
                'WHERE referral_code=?', (ref_param,)
            )
            db.commit()
        except Exception:
            pass
    return render_template('auth.html', mode='signup', prefill_ref=ref_param)


@bp.route('/login', methods=['GET', 'POST'])
@limiter.limit(LIMIT_LOGIN)
def login():
    if request.method == 'POST':
        db         = get_db()
        identifier = request.form.get('identifier', '').strip()
        password   = request.form.get('password', '')
        user = db.execute(
            'SELECT * FROM users WHERE email=? OR username=?',
            (identifier.lower(), identifier)
        ).fetchone()
        if not user or not verify_password(password, user['password']):
            return jsonify({'success': False, 'errors': ['Invalid credentials.']}), 401
        if user and dict(user).get('is_banned', 0):
            reason = dict(user).get('ban_reason', '') or 'Community guidelines violation'
            return jsonify({'success': False, 'errors': [f'Account suspended: {reason}']}), 403
        maybe_upgrade_password_hash(db, user['id'], password, user['password'])
        user_d = dict(user)
        # If 2FA is enabled, send to challenge page instead of logging in directly
        if user_d.get('totp_enabled') and user_d.get('totp_secret'):
            session.clear()
            session['2fa_pending_uid'] = user_d['id']
            return jsonify({'success': True, 'redirect': url_for('auth.two_fa_challenge')})
        session.clear()
        session['user_id'] = user['id']
        try:
            ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()[:45]
            ua = request.headers.get('User-Agent', '')[:300]
            db.execute('INSERT INTO login_history (user_id, ip_address, user_agent) VALUES (?,?,?)',
                       (user['id'], ip, ua))
            db.commit()
        except Exception:
            pass
        # Admin accounts land on the dashboard, not the social feed
        is_admin  = bool(user_d.get('is_admin', 0))
        redirect_to = url_for('admin.admin') if is_admin else url_for('social.feed')
        return jsonify({'success': True, 'redirect': redirect_to})
    return render_template('auth.html', mode='login')


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.index'))



# ── Forgot / Reset Password ───────────────────────────────────────────────────

@bp.route('/auth/forgot-password', methods=['POST'])
@limiter.limit('5 per hour')
@csrf_exempt
def forgot_password():
    """
    Accept an email address and generate a password-reset token.
    In production wire this to an email provider (SendGrid, Mailgun, etc.).
    For now: stores the token in the DB and returns it in the response
    (so admins can manually relay it, or you can add email sending here).
    """
    import time, hmac, hashlib
    data  = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()

    if not email:
        return jsonify({'success': False, 'error': 'Email is required.'}), 400

    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()

    # Always respond with success to prevent email enumeration
    if not user:
        return jsonify({
            'success': True,
            'message': 'If that email is registered you will receive reset instructions shortly.'
        })

    # Generate a short-lived token (1 hour)
    raw_token = secrets.token_urlsafe(32)
    expires   = int(time.time()) + 3600
    token_str = f'{raw_token}:{expires}:{user["id"]}'

    # Store token hash in the DB (add reset_token / reset_expires columns on the fly)
    try:
        db.execute('ALTER TABLE users ADD COLUMN reset_token TEXT')
        db.execute('ALTER TABLE users ADD COLUMN reset_expires INTEGER')
    except Exception:
        pass  # columns already exist

    db.execute(
        'UPDATE users SET reset_token=?, reset_expires=? WHERE id=?',
        (raw_token, expires, user['id'])
    )
    db.commit()

    # Build reset link
    scheme   = request.headers.get('X-Forwarded-Proto', request.scheme)
    base_url = f'{scheme}://{request.host}'
    link     = f'{base_url}/auth/reset-password?token={raw_token}&uid={user["id"]}'

    # ── TODO: Send email here ──────────────────────────────────────────────────
    # import sendgrid / mailgun / smtplib etc. and email the link.
    # For now, if there's a SUPPORT_EMAIL env var, log it clearly:
    import logging as _log
    _log.getLogger(__name__).info('PASSWORD RESET LINK for %s: %s', email, link)
    # ──────────────────────────────────────────────────────────────────────────

    return jsonify({
        'success': True,
        'message': 'Reset instructions sent. Check your email (and spam folder).',
        # Remove the line below in production — only for dev/testing:
        '_dev_link': link,
    })


@bp.route('/auth/reset-password', methods=['GET', 'POST'])
@limiter.limit('10 per hour')
def reset_password():
    """Password reset page — user arrives here from the emailed link."""
    import time
    token = request.args.get('token') or request.form.get('token') or ''
    uid   = safe_int(request.args.get('uid') or request.form.get('uid'), 0)

    if request.method == 'GET':
        return render_template('reset_password.html', token=token, uid=uid)

    # POST — apply new password
    new_pw  = request.form.get('password', '')
    confirm = request.form.get('confirm_password', '')

    errors = []
    if len(new_pw) < 8:
        errors.append('Password must be at least 8 characters.')
    if new_pw != confirm:
        errors.append('Passwords do not match.')
    if errors:
        return jsonify({'success': False, 'errors': errors})

    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not user:
        return jsonify({'success': False, 'errors': ['Invalid reset link.']}), 400

    # Validate token
    user_d = dict(user)
    stored_token   = user_d.get('reset_token')
    stored_expires = user_d.get('reset_expires', 0)

    if not stored_token or stored_token != token:
        return jsonify({'success': False, 'errors': ['Invalid or expired reset link.']}), 400
    if int(time.time()) > (stored_expires or 0):
        return jsonify({'success': False, 'errors': ['Reset link has expired. Please request a new one.']}), 400

    db.execute(
        'UPDATE users SET password=?, reset_token=NULL, reset_expires=NULL WHERE id=?',
        (hash_password(new_pw), uid)
    )
    db.commit()
    return jsonify({'success': True, 'redirect': url_for('auth.login')})


def safe_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# ── Google OAuth ─────────────────────────────────────────────────────────────

@bp.route('/auth/google')
def google_login_route():
    from flask import current_app
    oauth = current_app.extensions.get('authlib.integrations.flask_client')
    if not oauth or not getattr(oauth, 'google', None):
        return redirect(url_for('auth.login'))
    scheme = (
        'https'
        if request.headers.get('X-Forwarded-Proto', request.scheme) == 'https'
        else request.scheme
    )
    redirect_uri = url_for('auth.google_auth_callback', _external=True, _scheme=scheme)
    return oauth.google.authorize_redirect(redirect_uri)


@bp.route('/auth/google/callback')
@csrf_exempt
def google_auth_callback():
    import logging as _log
    _logger = _log.getLogger(__name__)
    from flask import current_app
    oauth = current_app.extensions.get('authlib.integrations.flask_client')
    if not oauth or not getattr(oauth, 'google', None):
        _logger.warning('Google OAuth not configured')
        return redirect(url_for('auth.login'))
    try:
        token = oauth.google.authorize_access_token()
    except Exception as e:
        _logger.error('Google authorize_access_token failed: %s', e)
        return redirect(url_for('auth.login'))

    # Fetch userinfo from the token or the userinfo endpoint
    userinfo = token.get('userinfo')
    if not userinfo:
        try:
            # Authlib ≥1.0: userinfo is in the token; fallback to endpoint
            resp     = oauth.google.get('https://openidconnect.googleapis.com/v1/userinfo')
            userinfo = resp.json()
        except Exception as e:
            _logger.error('Google userinfo fetch failed: %s', e)
            try:
                # Last resort: parse id_token
                userinfo = oauth.google.parse_id_token(token, nonce=None)
            except Exception as e2:
                _logger.error('Google parse_id_token failed: %s', e2)
                return redirect(url_for('auth.login'))

    if not userinfo or not userinfo.get('email'):
        _logger.error('No email in Google userinfo: %s', userinfo)
        return redirect(url_for('auth.login'))

    return _finalize_oauth_login(userinfo, provider='Google')


def _finalize_oauth_login(userinfo, provider):
    """
    Google sign-in handler.
    For EXISTING accounts → log in directly to the feed.
    For NEW accounts → stash the OAuth info in the session and send them
    to /auth/complete-profile where they fill in username, display name,
    and optional referral code before the account is actually created.
    """
    email = (userinfo or {}).get('email')
    name  = (userinfo or {}).get('name') or (email.split('@')[0] if email else None)
    if not email:
        return redirect(url_for('auth.login'))

    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()

    if user:
        # Returning user — sign in directly
        session.clear()
        if user and dict(user).get('is_banned', 0):
            return redirect(url_for('auth.login') + '?banned=1')
        user_d = dict(user)
        session['user_id'] = user_d['id']
        try:
            ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()[:45]
            ua = request.headers.get('User-Agent', '')[:300]
            db.execute('INSERT INTO login_history (user_id, ip_address, user_agent) VALUES (?,?,?)',
                       (user_d['id'], ip, ua))
            db.commit()
        except Exception:
            pass
        if user_d.get('is_admin', 0):
            return redirect(url_for('admin.admin'))
        return redirect(url_for('social.feed'))

    # New user — stash OAuth info, send to the profile-completion form
    # Important: do NOT session.clear() here — keep existing session data intact
    # and just add oauth_pending. We clear it after the profile is saved.
    session['oauth_pending'] = {
        'provider':     provider,
        'email':        email,
        'suggested_name': name or '',
    }
    return redirect(url_for('auth.complete_profile'))


@bp.route('/auth/complete-profile', methods=['GET', 'POST'])
@limiter.limit(LIMIT_SIGNUP)
def complete_profile():
    """
    First-time profile completion for users signing in via Google.
    Asks for username, display name, and optional referral code.
    """
    pending = session.get('oauth_pending')
    if not pending:
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        db           = get_db()
        username     = request.form.get('username', '').strip()
        display_name = request.form.get('display_name', '').strip()[:60]
        ref_code     = request.form.get('referral_code', '').strip()

        errors = []
        if len(username) < 3:
            errors.append('Username must be at least 3 characters.')
        if not username.replace('_', '').replace('-', '').isalnum():
            errors.append('Username may only contain letters, numbers, _ and -.')
        if not display_name:
            errors.append('Display name is required.')
        if db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
            errors.append('Username already taken.')
        if errors:
            return jsonify({'success': False, 'errors': errors})

        referrer = (
            db.execute('SELECT * FROM users WHERE referral_code=?', (ref_code,)).fetchone()
            if ref_code else None
        )

        email    = pending['email']
        provider = pending.get('provider', 'OAuth')

        db.execute(
            'INSERT INTO users (username,display_name,email,password,referred_by,referral_code) '
            'VALUES (?,?,?,?,?,?)',
            (username, display_name, email, None,
             referrer['id'] if referrer else None, username)
        )
        db.commit()
        user = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()

        if referrer:
            CURRENCY_SYMBOL = current_app.config['CURRENCY_SYMBOL']
            add_notification(
                db, referrer['id'],
                f'👤 {username} signed up using your referral code! '
                f'Bonus will be awarded when they activate their account by spending {CURRENCY_SYMBOL}1.'
            )
        add_notification(db, user['id'], f'🔐 Welcome! Signed in with {provider}.')
        db.commit()

        # Clean up and log in
        session.pop('oauth_pending', None)
        session['user_id'] = user['id']
        # New accounts via Google are never admin
        return jsonify({'success': True, 'redirect': url_for('social.feed')})

    return render_template(
        'complete_profile.html',
        provider=pending.get('provider', 'Google'),
        email=pending.get('email', ''),
        suggested_name=pending.get('suggested_name', ''),
    )


@bp.route('/settings/security')
@login_required
def security_settings():
    db  = get_db()
    uid = session['user_id']
    try:
        history = db.execute(
            'SELECT ip_address, user_agent, created_at FROM login_history '
            'WHERE user_id=? ORDER BY created_at DESC LIMIT 10',
            (uid,)
        ).fetchall()
    except Exception:
        history = []
    user = db.execute('SELECT totp_enabled FROM users WHERE id=?', (uid,)).fetchone()
    return render_template('security_settings.html',
                           login_history=[dict(h) for h in history],
                           totp_enabled=bool(user and user['totp_enabled']))


# ── 2FA challenge (redirect target after password check when TOTP is on) ──────

@bp.route('/login/2fa', methods=['GET', 'POST'])
def two_fa_challenge():
    if 'user_id' in session:
        return redirect(url_for('social.feed'))
    uid = session.get('2fa_pending_uid')
    if not uid:
        return redirect(url_for('auth.login'))
    if request.method == 'POST':
        import pyotp
        db   = get_db()
        code = (request.form.get('code') or '').strip().replace(' ', '')
        user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if not user:
            session.pop('2fa_pending_uid', None)
            return redirect(url_for('auth.login'))
        totp = pyotp.TOTP(user['totp_secret'])
        if not totp.verify(code, valid_window=1):
            return render_template('2fa_challenge.html', error='Invalid code. Try again.')
        session.pop('2fa_pending_uid', None)
        session['user_id'] = uid
        try:
            ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()[:45]
            ua = request.headers.get('User-Agent', '')[:300]
            db.execute('INSERT INTO login_history (user_id, ip_address, user_agent) VALUES (?,?,?)',
                       (uid, ip, ua))
            db.commit()
        except Exception:
            pass
        is_admin = bool(dict(user).get('is_admin', 0))
        return redirect(url_for('admin.admin') if is_admin else url_for('social.feed'))
    return render_template('2fa_challenge.html', error=None)


# ── 2FA setup / enable / disable ──────────────────────────────────────────────

@bp.route('/settings/2fa', methods=['GET'])
@login_required
def two_fa_setup():
    import pyotp, qrcode, io, base64
    db  = get_db()
    uid = session['user_id']
    user = db.execute('SELECT username, totp_secret, totp_enabled FROM users WHERE id=?', (uid,)).fetchone()
    user_d = dict(user)

    # Generate a fresh secret each time the page loads (only saved on enable)
    secret = pyotp.random_base32()
    session['2fa_setup_secret'] = secret

    totp     = pyotp.TOTP(secret)
    otp_uri  = totp.provisioning_uri(name=user_d['username'], issuer_name='DUYS Boost')
    img      = qrcode.make(otp_uri)
    buf      = io.BytesIO()
    img.save(buf, format='PNG')
    qr_b64   = base64.b64encode(buf.getvalue()).decode()

    return render_template('settings_2fa.html',
                           qr_b64=qr_b64,
                           secret=secret,
                           totp_enabled=bool(user_d.get('totp_enabled')))


@bp.route('/settings/2fa/enable', methods=['POST'])
@login_required
@csrf_exempt
def two_fa_enable():
    import pyotp
    db     = get_db()
    uid    = session['user_id']
    if request.is_json:
        code = str((request.get_json(silent=True) or {}).get('code', '')).strip().replace(' ', '')
    else:
        code = str(request.form.get('code', '')).strip().replace(' ', '')
    secret = session.get('2fa_setup_secret')
    if not secret:
        return jsonify({'success': False, 'error': 'Setup session expired. Please reload the page.'}), 400
    totp = pyotp.TOTP(secret)
    if not totp.verify(code, valid_window=1):
        return jsonify({'success': False, 'error': 'Invalid code. Make sure your authenticator app time is correct.'}), 400
    db.execute('UPDATE users SET totp_secret=?, totp_enabled=1 WHERE id=?', (secret, uid))
    db.commit()
    session.pop('2fa_setup_secret', None)
    return jsonify({'success': True})


@bp.route('/settings/2fa/disable', methods=['POST'])
@login_required
@csrf_exempt
def two_fa_disable():
    import pyotp
    db   = get_db()
    uid  = session['user_id']
    data = request.get_json(silent=True) or {}
    code = str(data.get('code', '') or request.form.get('code', '')).strip().replace(' ', '')
    user = db.execute('SELECT totp_secret, totp_enabled FROM users WHERE id=?', (uid,)).fetchone()
    if not user or not user['totp_enabled'] or not user['totp_secret']:
        return jsonify({'success': False, 'error': '2FA is not enabled.'}), 400
    totp = pyotp.TOTP(user['totp_secret'])
    if not totp.verify(code, valid_window=1):
        return jsonify({'success': False, 'error': 'Invalid code.'}), 400
    db.execute('UPDATE users SET totp_secret=NULL, totp_enabled=0 WHERE id=?', (uid,))
    db.commit()
    return jsonify({'success': True})
