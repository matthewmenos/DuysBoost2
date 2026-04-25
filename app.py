"""
DUYS Boost — Social Media Boost Platform
Flask backend with SQLite, Paystack deposits, OAuth (Google),
referral rewards, and an ads/tasks marketplace.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps

import requests
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import (
    Flask, abort, g, jsonify, redirect, render_template, request, session,
    url_for
)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
base_dir = os.path.dirname(__file__)
dotenv_path = os.path.join(base_dir, '.env')
if not os.path.exists(dotenv_path):
    example_path = os.path.join(base_dir, '.env.example')
    if os.path.exists(example_path):
        dotenv_path = example_path
load_dotenv(dotenv_path, override=False)

# Use a strong secret key from env in production; fall back to a random one locally.
# IMPORTANT: set FLASK_SECRET_KEY in production so sessions survive restarts.
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)

# Secure cookie settings (hardens sessions against CSRF and XSS)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '0') == '1',
)

DB_PATH = os.path.join(os.path.dirname(__file__), 'duys_boost.db')

# Currency & pricing (Ghana Cedis)
CURRENCY_CODE = 'GHS'
CURRENCY_SYMBOL = 'GH₵'
WORKER_REWARD_PER_TASK = 0.30   # 30 pesewas earned per completed follower task
LISTER_COST_PER_TASK = 0.70     # 70 pesewas spent per follower gained
REFERRAL_BONUS = 1.0            # 1 cedi per successful referral

# Ghana Mobile Money providers supported by Paystack Transfers.
# The key is what the user sees; the value is Paystack's bank_code.
# Note: Vodafone Cash rebranded to Telecel Cash, but Paystack still uses 'VOD'.
MOMO_PROVIDERS = {
    'MTN':      'MTN Mobile Money',
    'VOD':      'Telecel Cash (Vodafone)',
    'ATL':      'AirtelTigo Money',
}

# External integrations — ALWAYS read from environment, never hardcode secrets.
PAYSTACK_PUBLIC_KEY = os.environ.get('PAYSTACK_PUBLIC_KEY', '')
PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', '')

GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')

oauth = OAuth(app)
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name='google',
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────────────────────
def get_db():
    """Get per-request SQLite connection."""
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(_e=None):
    db = g.pop('db', None)
    if db:
        db.close()


def init_db():
    """Create schema on first run and ensure an admin user exists."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT,
        balance REAL DEFAULT 0,
        referral_code TEXT UNIQUE,
        referred_by INTEGER,
        is_admin INTEGER DEFAULT 0,
        theme TEXT DEFAULT 'dark',
        paystack_recipient TEXT,
        recipient_provider TEXT,
        recipient_account TEXT,
        recipient_name TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS ads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        platform TEXT NOT NULL,
        target_url TEXT NOT NULL,
        task_type TEXT NOT NULL,
        reward_per_task REAL DEFAULT 0.10,
        budget REAL NOT NULL,
        budget_spent REAL DEFAULT 0,
        followers_target INTEGER DEFAULT 0,
        followers_gained INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS task_completions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ad_id INTEGER NOT NULL,
        worker_id INTEGER NOT NULL,
        proof_link TEXT NOT NULL,
        status TEXT DEFAULT 'approved',
        reward REAL,
        submitted_at TEXT DEFAULT (datetime('now')),
        reviewed_at TEXT,
        FOREIGN KEY(ad_id) REFERENCES ads(id),
        FOREIGN KEY(worker_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        type TEXT,
        amount REAL,
        description TEXT,
        status TEXT DEFAULT 'completed',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        message TEXT,
        read INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount REAL,
        method TEXT,
        account TEXT,
        status TEXT DEFAULT 'pending',
        transfer_code TEXT,
        paystack_reference TEXT,
        failure_reason TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        processed_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    ''')

    # ── Migrations for databases created by earlier versions ─────────────
    # Adding columns via CREATE TABLE IF NOT EXISTS is a no-op on existing
    # tables, so we add them here idempotently.
    def _add_col_if_missing(table, col, decl):
        cols = {r[1] for r in db.execute(f'PRAGMA table_info({table})').fetchall()}
        if col not in cols:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {col} {decl}')

    def _create_index_if_missing(table, index_name, cols):
        cols_set = {r[1] for r in db.execute(f'PRAGMA table_info({table})').fetchall()}
        if not set(c.strip() for c in cols.split(',')) <= cols_set:
            return
        existing = {r[1] for r in db.execute(f'PRAGMA index_list({table})').fetchall()}
        if index_name not in existing:
            db.execute(f'CREATE INDEX {index_name} ON {table}({cols})')

    _add_col_if_missing('users', 'paystack_recipient', 'TEXT')
    _add_col_if_missing('users', 'recipient_provider', 'TEXT')
    _add_col_if_missing('users', 'recipient_account', 'TEXT')
    _add_col_if_missing('users', 'recipient_name', 'TEXT')
    _add_col_if_missing('withdrawals', 'transfer_code', 'TEXT')
    _add_col_if_missing('withdrawals', 'paystack_reference', 'TEXT')
    _add_col_if_missing('withdrawals', 'failure_reason', 'TEXT')
    _add_col_if_missing('withdrawals', 'processed_at', 'TEXT')

    _create_index_if_missing('ads', 'idx_ads_user', 'user_id')
    _create_index_if_missing('ads', 'idx_ads_status', 'status')
    _create_index_if_missing('task_completions', 'idx_tc_worker', 'worker_id')
    _create_index_if_missing('task_completions', 'idx_tc_ad', 'ad_id')
    _create_index_if_missing('transactions', 'idx_tx_user', 'user_id')
    _create_index_if_missing('notifications', 'idx_notif_user', 'user_id, read')
    _create_index_if_missing('withdrawals', 'idx_wdr_status', 'status')
    _create_index_if_missing('withdrawals', 'idx_wdr_transfer', 'transfer_code')
    existing = db.execute('SELECT id FROM users WHERE username=?', ('admin',)).fetchone()
    if not existing:
        db.execute(
            'INSERT INTO users (username,email,password,is_admin,balance,referral_code) '
            'VALUES (?,?,?,1,1000.0,?)',
            ('admin', 'admin@duysboost.com', hash_password('admin123'),
             secrets.token_hex(5))
        )
    db.commit()
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Password hashing (salted PBKDF2-SHA256)
# ─────────────────────────────────────────────────────────────────────────────
# Format: "pbkdf2_sha256${iterations}${salt_hex}${hash_hex}"
# Legacy format (plain sha256 hex) is still verified for backward compatibility
# and transparently upgraded on next successful login.
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
    # Legacy: plain sha256 hex digest
    return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)


def _maybe_upgrade_password_hash(db, user_id: int, plaintext: str, stored: str):
    """Transparently upgrade legacy sha256 hashes to pbkdf2 on successful login."""
    if stored and not stored.startswith('pbkdf2_sha256$'):
        db.execute('UPDATE users SET password=? WHERE id=?',
                   (hash_password(plaintext), user_id))
        db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def add_notification(db, user_id, message):
    db.execute('INSERT INTO notifications (user_id, message) VALUES (?,?)',
               (user_id, message))


def add_transaction(db, user_id, type_, amount, description, status='completed'):
    db.execute(
        'INSERT INTO transactions (user_id,type,amount,description,status) '
        'VALUES (?,?,?,?,?)',
        (user_id, type_, amount, description, status)
    )


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


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = get_db().execute(
            'SELECT is_admin FROM users WHERE id=?', (session['user_id'],)
        ).fetchone()
        if not user or not user['is_admin']:
            return jsonify({'success': False, 'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated


def verify_task_completion(ad, proof_link, user_id):
    """
    Strict verification of task completion based on platform and task type.
    Returns {'valid': bool, 'error': str}
    """
    platform = ad['platform'].lower()
    task_type = ad['task_type'].lower()

    # Basic URL validation
    if not proof_link or not proof_link.startswith(('http://', 'https://')):
        return {'valid': False, 'error': 'Please provide a valid URL as proof.'}

    # Platform-specific validation
    if task_type == 'follow':
        return verify_follow_task(platform, proof_link, ad['target_url'])
    elif task_type == 'like':
        return verify_like_task(platform, proof_link)
    elif task_type == 'comment':
        return verify_comment_task(platform, proof_link)
    elif task_type == 'share':
        return verify_share_task(platform, proof_link)
    else:
        # For unknown task types, do basic validation
        return {'valid': True, 'error': ''}


def verify_follow_task(platform, proof_link, target_url):
    """Verify follow task completion"""
    if platform == 'instagram':
        # Check if it's an Instagram URL
        if 'instagram.com/' in proof_link:
            return {'valid': True, 'error': ''}
        else:
            return {'valid': False, 'error': 'Please provide an Instagram URL as proof.'}

    elif platform == 'tiktok':
        if 'tiktok.com/' in proof_link:
            return {'valid': True, 'error': ''}
        else:
            return {'valid': False, 'error': 'Please provide a TikTok URL as proof.'}

    elif platform == 'twitter' or platform == 'x':
        if 'twitter.com/' in proof_link or 'x.com/' in proof_link:
            return {'valid': True, 'error': ''}
        else:
            return {'valid': False, 'error': 'Please provide a Twitter/X URL as proof.'}

    elif platform == 'facebook':
        if 'facebook.com/' in proof_link:
            return {'valid': True, 'error': ''}
        else:
            return {'valid': False, 'error': 'Please provide a Facebook URL as proof.'}

    elif platform == 'youtube':
        if 'youtube.com/' in proof_link or 'youtu.be/' in proof_link:
            return {'valid': True, 'error': ''}
        else:
            return {'valid': False, 'error': 'Please provide a YouTube URL as proof.'}

    # For other platforms, basic validation
    return {'valid': True, 'error': ''}


def verify_like_task(platform, proof_link):
    """Verify like task completion"""
    return verify_follow_task(platform, proof_link, '')


def verify_comment_task(platform, proof_link):
    """Verify comment task completion"""
    return verify_follow_task(platform, proof_link, '')


def verify_share_task(platform, proof_link):
    """Verify share task completion"""
    return verify_follow_task(platform, proof_link, '')


def get_current_user():
    if 'user_id' not in session:
        return None
    return get_db().execute(
        'SELECT * FROM users WHERE id=?', (session['user_id'],)
    ).fetchone()


@app.context_processor
def inject_user():
    return {
        'current_user': get_current_user(),
        'CURRENCY_SYMBOL': CURRENCY_SYMBOL,
        'CURRENCY_CODE': CURRENCY_CODE,
        'PAYSTACK_PUBLIC_KEY': PAYSTACK_PUBLIC_KEY,
        'PAYSTACK_ENABLED': bool(PAYSTACK_SECRET_KEY and PAYSTACK_PUBLIC_KEY),
        'PAYSTACK_TRANSFERS_ENABLED': bool(PAYSTACK_SECRET_KEY),
        'MOMO_PROVIDERS': MOMO_PROVIDERS,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Paystack API helpers
# ─────────────────────────────────────────────────────────────────────────────
PAYSTACK_BASE = 'https://api.paystack.co'
PAYSTACK_TIMEOUT = 15  # seconds


def _paystack_headers():
    return {
        'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}',
        'Content-Type': 'application/json',
    }


def paystack_post(path, payload):
    """POST helper that normalises (ok, data_or_error) responses."""
    try:
        r = requests.post(
            PAYSTACK_BASE + path, headers=_paystack_headers(),
            json=payload, timeout=PAYSTACK_TIMEOUT,
        )
        body = r.json()
    except requests.RequestException:
        return False, 'Could not reach Paystack. Please try again.'
    except ValueError:
        return False, 'Unexpected response from Paystack.'
    if not body.get('status'):
        return False, body.get('message', 'Paystack request failed.')
    return True, body.get('data') or {}


def paystack_get(path):
    try:
        r = requests.get(
            PAYSTACK_BASE + path, headers=_paystack_headers(),
            timeout=PAYSTACK_TIMEOUT,
        )
        body = r.json()
    except requests.RequestException:
        return False, 'Could not reach Paystack.'
    except ValueError:
        return False, 'Unexpected response from Paystack.'
    if not body.get('status'):
        return False, body.get('message', 'Paystack request failed.')
    return True, body.get('data') or {}


def _verify_paystack_signature(raw_body: bytes, signature: str) -> bool:
    """Webhooks are signed with HMAC-SHA512 of the raw request body using the
    secret key. If the signature doesn't match, the request wasn't really from
    Paystack and must be discarded."""
    if not signature or not PAYSTACK_SECRET_KEY:
        return False
    computed = hmac.new(
        PAYSTACK_SECRET_KEY.encode('utf-8'), raw_body, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        db = get_db()
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
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
        if db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
            errors.append('Email already registered.')
        if db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
            errors.append('Username already taken.')
        if errors:
            return jsonify({'success': False, 'errors': errors})

        referrer = (db.execute('SELECT * FROM users WHERE referral_code=?', (ref_code,))
                    .fetchone() if ref_code else None)
        db.execute(
            'INSERT INTO users (username,email,password,referred_by,referral_code) '
            'VALUES (?,?,?,?,?)',
            (username, email, hash_password(password),
             referrer['id'] if referrer else None, secrets.token_hex(5))
        )
        db.commit()
        user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if referrer:
            db.execute('UPDATE users SET balance=balance+? WHERE id=?',
                       (REFERRAL_BONUS, referrer['id']))
            add_notification(
                db, referrer['id'],
                f'🎉 {username} signed up using your referral! '
                f'+{CURRENCY_SYMBOL}{REFERRAL_BONUS:.2f} added.'
            )
            add_transaction(db, referrer['id'], 'earn', REFERRAL_BONUS,
                            f'Referral bonus from {username}')
        add_notification(db, user['id'], '👋 Welcome to DUYS Boost! Your account is ready.')
        db.commit()
        session['user_id'] = user['id']
        return jsonify({'success': True, 'redirect': url_for('dashboard')})
    return render_template('auth.html', mode='signup')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        db = get_db()
        identifier = request.form.get('identifier', '').strip()
        password = request.form.get('password', '')
        user = db.execute(
            'SELECT * FROM users WHERE email=? OR username=?',
            (identifier.lower(), identifier)
        ).fetchone()
        if not user or not verify_password(password, user['password']):
            return jsonify({'success': False, 'errors': ['Invalid credentials.']})
        _maybe_upgrade_password_hash(db, user['id'], password, user['password'])
        session.clear()
        session['user_id'] = user['id']
        return jsonify({'success': True, 'redirect': url_for('dashboard')})
    return render_template('auth.html', mode='login')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


# ── OAuth: Google ────────────────────────────────────────────────────────────
@app.route('/auth/google')
def google_login_route():
    if not getattr(oauth, 'google', None):
        return redirect(url_for('login'))
    redirect_uri = url_for('google_auth_callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route('/auth/google/callback')
def google_auth_callback():
    if not getattr(oauth, 'google', None):
        return redirect(url_for('login'))
    try:
        token = oauth.google.authorize_access_token()
        userinfo = oauth.google.parse_id_token(token)
    except Exception:
        return redirect(url_for('login'))
    return _finalize_oauth_login(userinfo, provider='Google')


def _finalize_oauth_login(userinfo, provider):
    email = (userinfo or {}).get('email')
    name = (userinfo or {}).get('name') or (email.split('@')[0] if email else None)
    if not email:
        return redirect(url_for('login'))
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
    if not user:
        base = (name or email.split('@')[0]).replace(' ', '').lower()
        username = base or 'user'
        i = 1
        while db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
            username = f'{base}{i}'
            i += 1
        db.execute(
            'INSERT INTO users (username,email,password,referral_code) VALUES (?,?,?,?)',
            (username, email, None, secrets.token_hex(5))
        )
        db.commit()
        user = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        add_notification(db, user['id'], f'🔐 Signed in with {provider}.')
        db.commit()
    session.clear()
    session['user_id'] = user['id']
    return redirect(url_for('dashboard'))


# ── Dashboard ────────────────────────────────────────────────────────────────
@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    uid = session['user_id']
    ads = db.execute(
        'SELECT * FROM ads WHERE user_id=? ORDER BY created_at DESC LIMIT 5', (uid,)
    ).fetchall()
    # Convert Row objects to dicts for JSON serialization
    ads = [dict(ad) for ad in ads]
    
    recent_tasks = db.execute(
        'SELECT tc.*, a.title as ad_title FROM task_completions tc '
        'JOIN ads a ON tc.ad_id=a.id WHERE tc.worker_id=? '
        'ORDER BY tc.submitted_at DESC LIMIT 5',
        (uid,)
    ).fetchall()
    total_earned = db.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND type="earn"',
        (uid,)
    ).fetchone()[0]
    total_spent = db.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND type="spend"',
        (uid,)
    ).fetchone()[0]
    unread = db.execute(
        'SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0', (uid,)
    ).fetchone()[0]
    available_ads = db.execute(
        'SELECT * FROM ads WHERE status="active" AND user_id!=? '
        'AND id NOT IN (SELECT ad_id FROM task_completions WHERE worker_id=?) '
        'AND budget_spent < budget '
        'ORDER BY created_at DESC LIMIT 10',
        (uid, uid)
    ).fetchall()
    # Convert available_ads Row objects to dicts
    available_ads = [dict(ad) for ad in available_ads]
    
    return render_template(
        'dashboard.html',
        ads=ads, recent_tasks=recent_tasks,
        total_earned=total_earned, total_spent=total_spent,
        unread=unread, available_ads=available_ads
    )


# ── Ads ──────────────────────────────────────────────────────────────────────
@app.route('/ads')
@login_required
def ads():
    db = get_db()
    user_ads = db.execute(
        'SELECT * FROM ads WHERE user_id=? ORDER BY created_at DESC',
        (session['user_id'],)
    ).fetchall()
    return render_template(
        'ads.html', ads=user_ads,
        worker_reward=WORKER_REWARD_PER_TASK,
        lister_cost=LISTER_COST_PER_TASK
    )


@app.route('/ads/create', methods=['POST'])
@login_required
def create_ad():
    db = get_db()
    uid = session['user_id']
    user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

    title = request.form.get('title', '').strip()
    platform = request.form.get('platform', '').strip()
    task_type = request.form.get('task_type', '').strip()
    target_url = request.form.get('target_url', '').strip()
    followers_target = safe_int(request.form.get('followers_target'), 0)

    if not title or len(title) > 120:
        return jsonify({'success': False, 'error': 'Please enter a valid campaign title.'})
    if not platform or not task_type:
        return jsonify({'success': False, 'error': 'Please select a platform and task type.'})
    if not target_url.startswith(('http://', 'https://')):
        return jsonify({'success': False, 'error': 'Please enter a valid target URL.'})
    if followers_target <= 0:
        return jsonify({'success': False, 'error': 'Please enter a valid followers target.'})

    budget = round(followers_target * LISTER_COST_PER_TASK, 2)
    if budget <= 0 or budget > user['balance']:
        return jsonify({'success': False,
                        'error': 'Insufficient balance for this followers target.'})

    db.execute(
        'INSERT INTO ads (user_id,title,platform,target_url,task_type,'
        'reward_per_task,budget,followers_target) VALUES (?,?,?,?,?,?,?,?)',
        (uid, title, platform, target_url, task_type,
         WORKER_REWARD_PER_TASK, budget, followers_target)
    )
    db.execute('UPDATE users SET balance=balance-? WHERE id=?', (budget, uid))
    ad = db.execute(
        'SELECT * FROM ads WHERE user_id=? ORDER BY id DESC LIMIT 1', (uid,)
    ).fetchone()
    add_transaction(db, uid, 'spend', budget, f'Budget for ad: {ad["title"]}')
    add_notification(db, uid, f'📢 Ad "{ad["title"]}" is now live!')
    
    # Notify all other users about the new task
    users = db.execute('SELECT id FROM users WHERE id != ?', (uid,)).fetchall()
    for user in users:
        add_notification(db, user['id'], f'📢 New task available: "{ad["title"]}" on {ad["platform"]}')
    
    db.commit()
    return jsonify({'success': True})


@app.route('/ads/<int:ad_id>/toggle', methods=['POST'])
@login_required
def toggle_ad(ad_id):
    db = get_db()
    ad = db.execute('SELECT * FROM ads WHERE id=?', (ad_id,)).fetchone()
    if not ad or ad['user_id'] != session['user_id']:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    if ad['status'] == 'completed':
        return jsonify({'success': False, 'error': 'Campaign already completed.'})
    new_status = 'paused' if ad['status'] == 'active' else 'active'
    db.execute('UPDATE ads SET status=? WHERE id=?', (new_status, ad_id))
    db.commit()
    return jsonify({'success': True, 'status': new_status})


# ── Tasks ────────────────────────────────────────────────────────────────────
@app.route('/tasks')
@login_required
def tasks():
    db = get_db()
    uid = session['user_id']
    available = db.execute(
        'SELECT * FROM ads WHERE status="active" AND user_id!=? '
        'AND id NOT IN (SELECT ad_id FROM task_completions WHERE worker_id=?) '
        'AND budget_spent < budget '
        'ORDER BY created_at DESC',
        (uid, uid)
    ).fetchall()
    my_tasks = db.execute(
        'SELECT tc.*, a.title as ad_title FROM task_completions tc '
        'JOIN ads a ON tc.ad_id=a.id WHERE tc.worker_id=? '
        'ORDER BY tc.submitted_at DESC',
        (uid,)
    ).fetchall()
    return render_template('tasks.html', available=available, my_tasks=my_tasks)


@app.route('/tasks/submit', methods=['POST'])
@login_required
def submit_task():
    db = get_db()
    uid = session['user_id']
    ad_id = safe_int(request.form.get('ad_id'), 0)
    proof_link = request.form.get('proof_link', '').strip()

    ad = db.execute('SELECT * FROM ads WHERE id=?', (ad_id,)).fetchone()
    if not ad:
        return jsonify({'success': False, 'error': 'Ad not found.'})
    if ad['status'] != 'active':
        return jsonify({'success': False, 'error': 'This campaign is not active.'})
    if ad['user_id'] == uid:
        return jsonify({'success': False, 'error': 'Cannot complete your own ad.'})
    if db.execute('SELECT id FROM task_completions WHERE ad_id=? AND worker_id=?',
                  (ad_id, uid)).fetchone():
        return jsonify({'success': False, 'error': 'Already submitted for this ad.'})
    if not proof_link.startswith(('http://', 'https://')):
        return jsonify({'success': False, 'error': 'Please enter a valid proof URL.'})
    if ad['budget_spent'] + LISTER_COST_PER_TASK > ad['budget']:
        return jsonify({'success': False, 'error': 'This campaign has reached its budget.'})

    now = datetime.now(timezone.utc).isoformat()
    reward = WORKER_REWARD_PER_TASK

    # Strict verification: Check proof link validity based on platform and task type
    verification_result = verify_task_completion(ad, proof_link, uid)

    if not verification_result['valid']:
        return jsonify({'success': False, 'error': verification_result['error']})

    # Award reward immediately for valid submissions
    db.execute(
        'INSERT INTO task_completions (ad_id,worker_id,proof_link,status,reward,reviewed_at) '
        'VALUES (?,?,?,?,?,?)',
        (ad_id, uid, proof_link, 'completed', reward, now)
    )

    # Update ad budget and follower count
    db.execute(
        'UPDATE ads SET budget_spent=budget_spent+?, followers_gained=followers_gained+1 '
        'WHERE id=?',
        (LISTER_COST_PER_TASK, ad_id)
    )

    # Credit the worker's balance immediately
    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (reward, uid))

    # Add transaction record
    add_transaction(db, uid, 'earn', reward, f'Task completed: {ad["title"]}')

    # Notify user of successful completion
    add_notification(db, uid,
                     f'✅ Task completed! +{CURRENCY_SYMBOL}{reward:.2f} added to your wallet for "{ad["title"]}"')

    # Notify ad owner of new follower gained
    add_notification(db, ad['user_id'],
                     f'📈 New follower gained for "{ad["title"]}"!')

    db.commit()
    return jsonify({'success': True, 'message': f'Task completed! +{CURRENCY_SYMBOL}{reward:.2f} added to your wallet'})


# ── Wallet ───────────────────────────────────────────────────────────────────
@app.route('/wallet')
@login_required
def wallet():
    db = get_db()
    uid = session['user_id']
    txs = db.execute(
        'SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC', (uid,)
    ).fetchall()
    wdrs = db.execute(
        'SELECT * FROM withdrawals WHERE user_id=? ORDER BY created_at DESC', (uid,)
    ).fetchall()
    return render_template('wallet.html', transactions=txs, withdrawals=wdrs)


@app.route('/wallet/deposit', methods=['POST'])
@login_required
def deposit():
    """Verify a Paystack transaction reference and credit the user's wallet."""
    if not PAYSTACK_SECRET_KEY:
        return jsonify({'success': False,
                        'error': 'Deposits are not available. Paystack is not configured.'}), 503
    db = get_db()
    uid = session['user_id']
    payload = request.get_json(silent=True) or {}
    reference = (payload.get('reference') or '').strip()
    declared_amount = safe_float(payload.get('amount'), 0)
    if not reference or declared_amount <= 0:
        return jsonify({'success': False, 'error': 'Invalid deposit payload.'}), 400
    ok, data = paystack_get(f'/transaction/verify/{reference}')
    if not ok:
        return jsonify({'success': False, 'error': data}), 502
    if not data.get('status'):
        return jsonify({'success': False, 'error': 'Unable to verify payment.'}), 400
    if data.get('status') != 'success' or data.get('currency') != CURRENCY_CODE:
        return jsonify({'success': False, 'error': 'Payment not successful.'}), 400
    paid_amount = (data.get('amount') or 0) / 100.0
    if paid_amount <= 0:
        return jsonify({'success': False,
                        'error': 'Invalid amount from payment gateway.'}), 400
    # Prevent double-credit: match by the unique reference tag in description.
    existing = db.execute(
        'SELECT id FROM transactions WHERE user_id=? AND description LIKE ?',
        (uid, f'%{reference}%')
    ).fetchone()
    if existing:
        user = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()
        return jsonify({'success': True, 'balance': user['balance']})
    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (paid_amount, uid))
    add_transaction(db, uid, 'deposit', paid_amount, f'Paystack deposit {reference}')
    add_notification(
        db, uid,
        f'💳 {CURRENCY_SYMBOL}{paid_amount:.2f} deposited to your wallet via Paystack.'
    )
    db.commit()
    user = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()
    return jsonify({'success': True, 'balance': user['balance']})


@app.route('/wallet/recipient', methods=['POST'])
@login_required
def add_recipient():
    """Register the user's Mobile Money account with Paystack as a transfer
    recipient. The recipient_code is saved on the user record and reused for
    all future withdrawals until the user changes it."""
    if not PAYSTACK_SECRET_KEY:
        return jsonify({'success': False,
                        'error': 'Payouts are not configured.'}), 503
    db = get_db()
    uid = session['user_id']
    name = request.form.get('account_name', '').strip()
    phone = request.form.get('phone', '').strip().replace(' ', '')
    provider = request.form.get('provider', '').strip().upper()

    # Normalise Ghanaian local format (0XXXXXXXXX) or international (+233XXXXXXXXX / 233XXXXXXXXX)
    if phone.startswith('+233'):
        phone = '0' + phone[4:]
    elif phone.startswith('233') and len(phone) == 12:
        phone = '0' + phone[3:]

    if not name or len(name) < 2:
        return jsonify({'success': False, 'error': 'Enter the account holder name.'})
    if provider not in MOMO_PROVIDERS:
        return jsonify({'success': False, 'error': 'Choose a valid mobile money provider.'})
    if not phone.isdigit() or len(phone) != 10 or not phone.startswith('0'):
        return jsonify({'success': False,
                        'error': 'Enter a valid 10-digit Ghana mobile number (e.g. 0241234567).'})

    ok, data = paystack_post('/transferrecipient', {
        'type': 'mobile_money',
        'name': name,
        'account_number': phone,
        'bank_code': provider,
        'currency': CURRENCY_CODE,
    })
    if not ok:
        return jsonify({'success': False, 'error': data}), 400

    recipient_code = data.get('recipient_code')
    if not recipient_code:
        return jsonify({'success': False,
                        'error': 'Paystack did not return a recipient code.'}), 502

    db.execute(
        'UPDATE users SET paystack_recipient=?, recipient_provider=?, '
        'recipient_account=?, recipient_name=? WHERE id=?',
        (recipient_code, provider, phone, name, uid)
    )
    add_notification(db, uid,
                     f'✅ Payout account saved: {MOMO_PROVIDERS[provider]} • {phone}')
    db.commit()
    return jsonify({
        'success': True,
        'provider': provider,
        'provider_label': MOMO_PROVIDERS[provider],
        'account': phone,
        'name': name,
    })


@app.route('/wallet/recipient', methods=['DELETE'])
@login_required
def remove_recipient():
    """Clear the user's saved payout recipient. Doesn't delete it on Paystack's
    side (Paystack keeps recipients around so you can reuse them), just forgets
    it locally so the user can add a different one."""
    db = get_db()
    db.execute(
        'UPDATE users SET paystack_recipient=NULL, recipient_provider=NULL, '
        'recipient_account=NULL, recipient_name=NULL WHERE id=?',
        (session['user_id'],)
    )
    db.commit()
    return jsonify({'success': True})


@app.route('/wallet/withdraw', methods=['POST'])
@login_required
def withdraw():
    db = get_db()
    uid = session['user_id']
    amount = safe_float(request.form.get('amount'), 0)

    user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

    if amount <= 0:
        return jsonify({'success': False, 'error': 'Enter a valid amount.'})
    # Paystack minimum transfer is 1 GHS
    if amount < 1:
        return jsonify({'success': False,
                        'error': f'Minimum withdrawal is {CURRENCY_SYMBOL}1.00.'})
    if not user['paystack_recipient']:
        return jsonify({'success': False,
                        'error': 'Please add a payout account first.'}), 400
    if amount > user['balance']:
        return jsonify({'success': False, 'error': 'Insufficient balance.'})

    provider_label = MOMO_PROVIDERS.get(user['recipient_provider'],
                                        user['recipient_provider'] or 'Mobile Money')
    method = provider_label
    account = user['recipient_account'] or ''

    # Debit the user's balance and record a pending withdrawal. The actual
    # Paystack transfer only runs when an admin approves it (or you could
    # call _initiate_transfer here directly for auto-payouts).
    db.execute('UPDATE users SET balance=balance-? WHERE id=?', (amount, uid))
    db.execute(
        'INSERT INTO withdrawals (user_id,amount,method,account) VALUES (?,?,?,?)',
        (uid, amount, method, account)
    )
    add_transaction(db, uid, 'withdrawal', amount,
                    f'Withdrawal via {method}', status='pending')
    add_notification(
        db, uid,
        f'🏦 Withdrawal of {CURRENCY_SYMBOL}{amount:.2f} via {method} submitted.'
    )
    db.commit()
    updated = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()
    return jsonify({'success': True, 'balance': updated['balance']})


# ── Notifications ────────────────────────────────────────────────────────────
@app.route('/notifications')
@login_required
def notifications():
    db = get_db()
    uid = session['user_id']
    notifs = db.execute(
        'SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC', (uid,)
    ).fetchall()
    db.execute('UPDATE notifications SET read=1 WHERE user_id=?', (uid,))
    db.commit()
    return render_template('notifications.html', notifications=notifs)


@app.route('/api/notifications/unread')
@login_required
def unread_count():
    db = get_db()
    uid = session['user_id']
    count = db.execute(
        'SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0', (uid,)
    ).fetchone()[0]
    recent = db.execute(
        'SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 5',
        (uid,)
    ).fetchall()
    return jsonify({
        'count': count,
        'recent': [{'msg': n['message'], 'time': n['created_at'][:16]} for n in recent]
    })


@app.route('/api/theme', methods=['POST'])
@login_required
def toggle_theme():
    db = get_db()
    uid = session['user_id']
    user = db.execute('SELECT theme FROM users WHERE id=?', (uid,)).fetchone()
    new_theme = 'light' if user['theme'] == 'dark' else 'dark'
    db.execute('UPDATE users SET theme=? WHERE id=?', (new_theme, uid))
    db.commit()
    return jsonify({'theme': new_theme})


# ── Referrals ────────────────────────────────────────────────────────────────
@app.route('/referral')
@login_required
def referral():
    db = get_db()
    uid = session['user_id']
    referred_users = db.execute(
        'SELECT * FROM users WHERE referred_by=? ORDER BY created_at DESC', (uid,)
    ).fetchall()
    total_earned = len(referred_users) * REFERRAL_BONUS
    return render_template('referral.html',
                           referred_users=referred_users,
                           total_earned=total_earned)


# ── Admin ────────────────────────────────────────────────────────────────────
@app.route('/admin')
@login_required
def admin():
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?',
                      (session['user_id'],)).fetchone()
    if not user['is_admin']:
        return redirect(url_for('dashboard'))
    users = db.execute('SELECT * FROM users ORDER BY created_at DESC').fetchall()
    # Pending + failed → actionable by admin (pending = approve/reject; failed = retry/reject)
    wdrs = db.execute(
        'SELECT w.*, u.username FROM withdrawals w JOIN users u ON w.user_id=u.id '
        'WHERE w.status IN ("pending", "failed") ORDER BY w.created_at DESC'
    ).fetchall()
    # Recently processed for admin audit
    recent_wdrs = db.execute(
        'SELECT w.*, u.username FROM withdrawals w JOIN users u ON w.user_id=u.id '
        'WHERE w.status IN ("approved", "rejected", "processing") '
        'ORDER BY w.created_at DESC LIMIT 20'
    ).fetchall()
    all_ads = db.execute(
        'SELECT a.*, u.username as owner_name FROM ads a '
        'JOIN users u ON a.user_id=u.id ORDER BY a.created_at DESC'
    ).fetchall()
    total_users = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    total_ads = db.execute('SELECT COUNT(*) FROM ads').fetchone()[0]
    total_vol = db.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions'
    ).fetchone()[0]
    return render_template(
        'admin.html',
        users=users, withdrawals=wdrs, recent_withdrawals=recent_wdrs, ads=all_ads,
        total_users=total_users, total_ads=total_ads, total_vol=total_vol
    )


def _initiate_paystack_transfer(db, wr):
    """Trigger a real Paystack transfer for a withdrawal row. Returns
    (ok, message, transfer_code). Caller is responsible for the DB commit."""
    if not PAYSTACK_SECRET_KEY:
        return False, 'Paystack is not configured on this server.', None

    user = db.execute('SELECT * FROM users WHERE id=?',
                      (wr['user_id'],)).fetchone()
    if not user or not user['paystack_recipient']:
        return False, 'User has no saved payout recipient.', None

    # Idempotent reference so re-triggering the same withdrawal won't send
    # money twice (Paystack rejects duplicate references).
    reference = wr['paystack_reference'] or f'duys_wdr_{wr["id"]}_{secrets.token_hex(4)}'

    ok, data = paystack_post('/transfer', {
        'source': 'balance',
        'reason': f'DUYS Boost withdrawal #{wr["id"]}',
        'amount': int(round(wr['amount'] * 100)),  # pesewas
        'recipient': user['paystack_recipient'],
        'currency': CURRENCY_CODE,
        'reference': reference,
    })
    if not ok:
        return False, data, None

    transfer_code = data.get('transfer_code')
    status = data.get('status', 'pending')  # pending | otp | success | failed
    return True, status, transfer_code


@app.route('/admin/withdrawal/<int:wdr_id>/<action>', methods=['POST'])
@admin_required
def process_withdrawal(wdr_id, action):
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
        db.execute(
            'UPDATE withdrawals SET status=?, processed_at=? WHERE id=?',
            ('rejected', now, wdr_id)
        )
        db.execute('UPDATE users SET balance=balance+? WHERE id=?',
                   (wr['amount'], wr['user_id']))
        add_notification(
            db, wr['user_id'],
            f'❌ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} rejected. '
            f'Amount refunded.'
        )
        db.commit()
        return jsonify({'success': True, 'status': 'rejected'})

    # ── Approve: actually trigger a Paystack Transfer ───────────────────
    if not PAYSTACK_SECRET_KEY:
        # No Paystack configured — fall back to manual approval (old behaviour)
        db.execute(
            'UPDATE withdrawals SET status=?, processed_at=? WHERE id=?',
            ('approved', now, wdr_id)
        )
        add_notification(
            db, wr['user_id'],
            f'✅ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} approved '
            f'(manual payout).'
        )
        db.commit()
        return jsonify({'success': True, 'status': 'approved', 'manual': True})

    ok, status_or_msg, transfer_code = _initiate_paystack_transfer(db, wr)
    if not ok:
        # Mark as failed but keep balance debited; admin can retry or reject.
        db.execute(
            'UPDATE withdrawals SET status=?, failure_reason=? WHERE id=?',
            ('failed', status_or_msg[:500], wdr_id)
        )
        db.commit()
        return jsonify({'success': False, 'error': status_or_msg}), 502

    # Map Paystack's initial status to our withdrawal status.
    # 'pending' and 'otp' both mean "in flight"; we'll update to approved/failed
    # when the webhook fires (transfer.success / transfer.failed / transfer.reversed).
    ps_status = (status_or_msg or 'pending').lower()
    local_status = 'processing'
    if ps_status == 'success':
        local_status = 'approved'
    elif ps_status == 'failed':
        local_status = 'failed'

    db.execute(
        'UPDATE withdrawals SET status=?, transfer_code=?, processed_at=? WHERE id=?',
        (local_status, transfer_code, now, wdr_id)
    )
    if local_status == 'approved':
        add_notification(
            db, wr['user_id'],
            f'✅ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} paid out!'
        )
    elif ps_status == 'otp':
        # Paystack is in OTP mode — someone needs to finalize it.
        add_notification(
            db, wr['user_id'],
            f'⏳ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} is being '
            f'verified by our payment processor.'
        )
    else:
        add_notification(
            db, wr['user_id'],
            f'⏳ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} is being '
            f'processed — you will be notified once it arrives.'
        )
    db.commit()
    return jsonify({
        'success': True,
        'status': local_status,
        'paystack_status': ps_status,
        'transfer_code': transfer_code,
    })


@app.route('/admin/deposit_user', methods=['POST'])
@admin_required
def admin_deposit():
    db = get_db()
    user_id = safe_int(request.form.get('user_id'), 0)
    amount = safe_float(request.form.get('amount'), 0)
    if user_id <= 0 or amount <= 0:
        return jsonify({'success': False, 'error': 'Invalid input.'}), 400
    target = db.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone()
    if not target:
        return jsonify({'success': False, 'error': 'User not found.'}), 404

    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))
    add_transaction(db, user_id, 'deposit', amount, 'Admin deposit')
    add_notification(
        db, user_id,
        f'💰 Admin credited {CURRENCY_SYMBOL}{amount:.2f} to your account!'
    )
    db.commit()
    return jsonify({'success': True})


@app.route('/admin/send_notification', methods=['POST'])
@admin_required
def send_notification():
    db = get_db()
    message = request.form.get('message', '').strip()
    user_id = safe_int(request.form.get('user_id'), 0)
    
    if not message:
        return jsonify({'success': False, 'error': 'Message cannot be empty.'}), 400
    
    if user_id:
        # Send to specific user
        user = db.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found.'}), 404
        add_notification(db, user_id, f'📢 {message}')
    else:
        # Send to all users
        users = db.execute('SELECT id FROM users').fetchall()
        for user in users:
            add_notification(db, user['id'], f'📢 {message}')
    
    db.commit()
    return jsonify({'success': True})


@app.route('/api/activity')
@login_required
def activity_feed():
    db = get_db()
    rows = db.execute(
        'SELECT tc.reward, tc.submitted_at, u.username, a.title '
        'FROM task_completions tc '
        'JOIN users u ON tc.worker_id=u.id '
        'JOIN ads a ON tc.ad_id=a.id '
        'ORDER BY tc.submitted_at DESC LIMIT 10'
    ).fetchall()
    return jsonify([
        {'worker': r['username'], 'ad': r['title'],
         'reward': r['reward'], 'time': r['submitted_at'][11:19]}
        for r in rows
    ])



# ─────────────────────────────────────────────────────────────────────────────
# Paystack webhook
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/webhooks/paystack', methods=['POST'])
def paystack_webhook():
    """Paystack POSTs here server-to-server whenever a payment or transfer
    event happens. We verify the HMAC-SHA512 signature, then update wallet
    balances or withdrawal statuses accordingly.

    Events handled:
      • charge.success        — a deposit cleared (back-up for the inline flow)
      • transfer.success      — a withdrawal arrived in the user's MoMo wallet
      • transfer.failed       — payout failed; we refund the user
      • transfer.reversed     — successful payout was reversed; we refund too

    All other events are acknowledged with HTTP 200 so Paystack stops retrying.
    """
    raw = request.get_data()
    signature = request.headers.get('x-paystack-signature', '')
    if not _verify_paystack_signature(raw, signature):
        # Don't leak whether the secret is configured; just 401.
        abort(401)

    try:
        event = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return '', 400

    event_type = event.get('event', '')
    data = event.get('data') or {}
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()

    # ── Deposit confirmation ─────────────────────────────────────────────
    if event_type == 'charge.success':
        reference = (data.get('reference') or '').strip()
        amount = (data.get('amount') or 0) / 100.0
        currency = data.get('currency')
        customer_email = ((data.get('customer') or {}).get('email') or '').lower()
        if not reference or amount <= 0 or currency != CURRENCY_CODE or not customer_email:
            return '', 200

        user = db.execute('SELECT id FROM users WHERE email=?',
                          (customer_email,)).fetchone()
        if not user:
            return '', 200

        existing = db.execute(
            'SELECT id FROM transactions WHERE user_id=? AND description LIKE ?',
            (user['id'], f'%{reference}%')
        ).fetchone()
        if existing:
            return '', 200  # already credited by the inline callback

        db.execute('UPDATE users SET balance=balance+? WHERE id=?',
                   (amount, user['id']))
        add_transaction(db, user['id'], 'deposit', amount,
                        f'Paystack deposit {reference} (webhook)')
        add_notification(
            db, user['id'],
            f'💳 {CURRENCY_SYMBOL}{amount:.2f} deposited via Paystack.'
        )
        db.commit()
        return '', 200

    # ── Transfer (withdrawal) status updates ─────────────────────────────
    if event_type in ('transfer.success', 'transfer.failed', 'transfer.reversed'):
        transfer_code = data.get('transfer_code')
        if not transfer_code:
            return '', 200

        wr = db.execute('SELECT * FROM withdrawals WHERE transfer_code=?',
                        (transfer_code,)).fetchone()
        if not wr:
            return '', 200  # probably not one of ours

        # Idempotency — if we already moved past 'processing', ignore duplicate events
        if wr['status'] in ('approved', 'failed', 'rejected') and \
                event_type == 'transfer.success' and wr['status'] == 'approved':
            return '', 200
        if wr['status'] == 'rejected':
            return '', 200  # refund already happened via admin rejection

        if event_type == 'transfer.success':
            db.execute(
                'UPDATE withdrawals SET status=?, processed_at=? WHERE id=?',
                ('approved', now, wr['id'])
            )
            add_notification(
                db, wr['user_id'],
                f'✅ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} delivered '
                f'to your mobile money wallet!'
            )
        else:
            # Failed or reversed — refund the user and mark as failed.
            reason = (data.get('reason')
                      or data.get('gateway_response')
                      or data.get('failure_reason')
                      or 'Transfer did not complete')
            # Only refund if we haven't already (status wasn't already failed)
            if wr['status'] != 'failed':
                db.execute('UPDATE users SET balance=balance+? WHERE id=?',
                           (wr['amount'], wr['user_id']))
            db.execute(
                'UPDATE withdrawals SET status=?, failure_reason=?, processed_at=? '
                'WHERE id=?',
                ('failed', str(reason)[:500], now, wr['id'])
            )
            add_notification(
                db, wr['user_id'],
                f'❌ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} failed: '
                f'{reason}. Amount refunded to your wallet.'
            )
        db.commit()
        return '', 200

    # Unknown event — acknowledge so Paystack stops retrying.
    return '', 200


# ── Analytics ───────────────────────────────────────────────────────────────
@app.route('/analytics')
@login_required
def analytics():
    """Main analytics dashboard for advertisers."""
    db = get_db()
    uid = session['user_id']
    
    # Fetch all ads created by this user
    ads = db.execute(
        'SELECT * FROM ads WHERE user_id=? ORDER BY created_at DESC', (uid,)
    ).fetchall()
    
    if not ads:
        return render_template('analytics.html', ads=[], summary=None, currency=CURRENCY_SYMBOL)
    
    # Calculate aggregate metrics
    total_budget = sum(float(ad['budget']) for ad in ads)
    total_spent = sum(float(ad['budget_spent']) or 0 for ad in ads)
    total_followers = sum(int(ad['followers_gained']) or 0 for ad in ads)
    active_campaigns = sum(1 for ad in ads if ad['status'] == 'active')
    
    # Total task completions
    total_completions = db.execute(
        'SELECT COUNT(*) FROM task_completions WHERE ad_id IN '
        '(SELECT id FROM ads WHERE user_id=?) AND status="approved"',
        (uid,)
    ).fetchone()[0]
    
    # ROI calculation
    roi = 0
    if total_spent > 0:
        roi = ((total_followers * LISTER_COST_PER_TASK - total_spent) / total_spent * 100)
    
    summary = {
        'total_ads': len(ads),
        'active_campaigns': active_campaigns,
        'total_budget': total_budget,
        'total_spent': total_spent,
        'total_followers': total_followers,
        'total_completions': total_completions,
        'roi': roi,
        'avg_cost_per_follower': total_spent / total_followers if total_followers > 0 else 0
    }
    
    return render_template('analytics.html', ads=ads, summary=summary, 
                         currency=CURRENCY_SYMBOL, cost_per_task=LISTER_COST_PER_TASK)


@app.route('/api/analytics/<int:ad_id>')
@login_required
def api_analytics(ad_id):
    """Get detailed analytics for a specific ad."""
    db = get_db()
    uid = session['user_id']
    
    # Verify ownership
    ad = db.execute('SELECT * FROM ads WHERE id=? AND user_id=?', (ad_id, uid)).fetchone()
    if not ad:
        return jsonify({'success': False, 'error': 'Ad not found'}), 404
    
    # Fetch task completions
    completions = db.execute(
        'SELECT * FROM task_completions WHERE ad_id=? ORDER BY submitted_at DESC',
        (ad_id,)
    ).fetchall()
    
    # Calculate metrics
    total_tasks = len(completions)
    completed_tasks = sum(1 for c in completions if c['status'] == 'completed')
    rejected_tasks = sum(1 for c in completions if c['status'] == 'rejected')
    
    completion_rate = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0
    total_paid = sum(float(c['reward']) or 0 for c in completions if c['status'] == 'completed')
    
    # Group by date for trend chart
    trend_data = {}
    for completion in completions:
        date = completion['submitted_at'][:10]  # YYYY-MM-DD
        if date not in trend_data:
            trend_data[date] = {'total': 0, 'completed': 0}
        trend_data[date]['total'] += 1
        if completion['status'] == 'completed':
            trend_data[date]['completed'] += 1
    
    return jsonify({
        'success': True,
        'ad': {
            'id': ad['id'],
            'title': ad['title'],
            'platform': ad['platform'],
            'task_type': ad['task_type'],
            'budget': ad['budget'],
            'budget_spent': ad['budget_spent'] or 0,
            'followers_target': ad['followers_target'],
            'followers_gained': ad['followers_gained'] or 0,
            'status': ad['status']
        },
        'metrics': {
            'total_tasks': total_tasks,
            'completed_tasks': completed_tasks,
            'rejected_tasks': rejected_tasks,
            'completion_rate': round(completion_rate, 2),
            'total_paid': round(total_paid, 2),
            'avg_reward': round(total_paid / completed_tasks, 2) if completed_tasks > 0 else 0,
            'target_completion_rate': round((completed_tasks / ad['followers_target'] * 100), 2) if ad['followers_target'] > 0 else 0,
            'roi': round((ad['followers_gained'] * LISTER_COST_PER_TASK - (ad['budget_spent'] or 0)) / (ad['budget_spent'] or 1) * 100, 2) if ad['budget_spent'] else 0
        },
        'trend': sorted(trend_data.items())
    })


@app.route('/api/analytics/performance')
@login_required
def api_analytics_performance():
    """Get performance comparison across all user ads."""
    db = get_db()
    uid = session['user_id']
    
    ads = db.execute(
        'SELECT id, title, platform, task_type, followers_target, followers_gained, '
        'budget, budget_spent, status FROM ads WHERE user_id=? ORDER BY followers_gained DESC LIMIT 10',
        (uid,)
    ).fetchall()
    
    performance_data = []
    for ad in ads:
        completions = db.execute(
            'SELECT COUNT(*) FROM task_completions WHERE ad_id=? AND status="approved"',
            (ad['id'],)
        ).fetchone()[0]
        
        roi = 0
        if ad['budget_spent']:
            roi = ((ad['followers_gained'] or 0) * LISTER_COST_PER_TASK - ad['budget_spent']) / ad['budget_spent'] * 100
        
        performance_data.append({
            'title': ad['title'],
            'platform': ad['platform'],
            'followers_target': ad['followers_target'],
            'followers_gained': ad['followers_gained'] or 0,
            'completion_rate': round((ad['followers_gained'] or 0) / ad['followers_target'] * 100, 1) if ad['followers_target'] > 0 else 0,
            'budget': ad['budget'],
            'cost_per_follower': round(ad['budget_spent'] / (ad['followers_gained'] or 1), 2) if ad['followers_gained'] else 0,
            'roi': round(roi, 2),
            'status': ad['status']
        })
    
    return jsonify({
        'success': True,
        'performance': performance_data
    })


# ─────────────────────────────────────────────────────────────────────────────
# Error handlers
# ─────────────────────────────────────────────────────────────────────────────
@app.errorhandler(404)
def _not_found(_e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Not found'}), 404
    return render_template('error.html', code=404,
                           message='Page not found.'), 404


@app.errorhandler(500)
def _server_error(_e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Server error'}), 500
    return render_template('error.html', code=500,
                           message='Something went wrong on our end.'), 500


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    app.run(host='0.0.0.0', port=port, debug=debug)
