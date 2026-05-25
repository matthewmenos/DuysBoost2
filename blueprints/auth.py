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
             referrer['id'] if referrer else None, secrets.token_hex(5))
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
        return jsonify({'success': True, 'redirect': url_for('social.feed')})
    return render_template('auth.html', mode='signup')


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
        if user['is_banned']:
            reason = user.get('ban_reason') or 'Community guidelines violation'
            return jsonify({'success': False, 'errors': [f'Account suspended: {reason}']}), 403
        maybe_upgrade_password_hash(db, user['id'], password, user['password'])
        session.clear()
        session['user_id'] = user['id']
        return jsonify({'success': True, 'redirect': url_for('social.feed')})
    return render_template('auth.html', mode='login')


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.index'))


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
        if user['is_banned']:
            return redirect(url_for('auth.login') + '?banned=1')
        session['user_id'] = user['id']
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
             referrer['id'] if referrer else None, secrets.token_hex(5))
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
        return jsonify({'success': True, 'redirect': url_for('social.feed')})

    return render_template(
        'complete_profile.html',
        provider=pending.get('provider', 'Google'),
        email=pending.get('email', ''),
        suggested_name=pending.get('suggested_name', ''),
    )
