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
        db       = get_db()
        username = request.form.get('username', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm_password', '')
        ref_code = request.form.get('referral_code', '').strip()

        errors = []
        if len(username) < 3:
            errors.append('Username must be at least 3 characters.')
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
        if db.execute('SELECT id FROM users WHERE email=%s', (email,)).fetchone():
            errors.append('Email already registered.')
        if db.execute('SELECT id FROM users WHERE username=%s', (username,)).fetchone():
            errors.append('Username already taken.')
        if errors:
            return jsonify({'success': False, 'errors': errors})

        referrer = (
            db.execute('SELECT * FROM users WHERE referral_code=%s', (ref_code,)).fetchone()
            if ref_code else None
        )
        db.execute(
            'INSERT INTO users (username,email,password,referred_by,referral_code) '
            'VALUES (%s,%s,%s,%s,%s)',
            (username, email, hash_password(password),
             referrer['id'] if referrer else None, secrets.token_hex(5))
        )
        db.commit()
        user = db.execute('SELECT * FROM users WHERE username=%s', (username,)).fetchone()
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
            'SELECT * FROM users WHERE email=%s OR username=%s',
            (identifier.lower(), identifier)
        ).fetchone()
        if not user or not verify_password(password, user['password']):
            return jsonify({'success': False, 'errors': ['Invalid credentials.']}), 401
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
    from flask import current_app
    oauth = current_app.extensions.get('authlib.integrations.flask_client')
    if not oauth or not getattr(oauth, 'google', None):
        return redirect(url_for('auth.login'))
    try:
        token    = oauth.google.authorize_access_token()
        userinfo = oauth.google.parse_id_token(token)
    except Exception:
        return redirect(url_for('auth.login'))
    return _finalize_oauth_login(userinfo, provider='Google')


def _finalize_oauth_login(userinfo, provider):
    email = (userinfo or {}).get('email')
    name  = (userinfo or {}).get('name') or (email.split('@')[0] if email else None)
    if not email:
        return redirect(url_for('auth.login'))
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE email=%s', (email,)).fetchone()
    if not user:
        base     = (name or email.split('@')[0]).replace(' ', '').lower()
        username = base or 'user'
        i = 1
        while db.execute('SELECT id FROM users WHERE username=%s', (username,)).fetchone():
            username = f'{base}{i}'
            i += 1
        db.execute(
            'INSERT INTO users (username,email,password,referral_code) VALUES (%s,%s,%s,%s)',
            (username, email, None, secrets.token_hex(5))
        )
        db.commit()
        user = db.execute('SELECT * FROM users WHERE email=%s', (email,)).fetchone()
        add_notification(db, user['id'], f'🔐 Signed in with {provider}.')
        db.commit()
    session.clear()
    session['user_id'] = user['id']
    return redirect(url_for('social.feed'))
