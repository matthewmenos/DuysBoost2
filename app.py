"""
DUYS Boost — Social Media Boost Platform
Flask backend with SQLite, Crypto (USDT) deposits, OAuth (Google),
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

import threading
import requests
from crypto_engine import verify_deposit as _chain_verify_deposit
from crypto_engine import send_usdt as _chain_send_usdt
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

app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '0') == '1',
)

DB_PATH = os.path.join(os.path.dirname(__file__), 'duys_boost.db')

# Currency & pricing (USD)
CURRENCY_CODE = 'USD'
CURRENCY_SYMBOL = '$'
WORKER_REWARD_PER_TASK = 0.05   # $0.05 earned per completed follower task
LISTER_COST_PER_TASK = 0.10     # $0.10 spent per follower gained
REFERRAL_BONUS = 0.50           # $0.50 per successful referral
REFERRAL_ACTIVATION_FEE = 1.00  # $1.00 activation fee credited to admin

# Supported USDT crypto networks for deposit/withdrawal
CRYPTO_NETWORKS = {
    'aptos':     {'label': 'Aptos (APT)',        'token': 'USDT', 'chain': 'Aptos'},
    'avalanche': {'label': 'Avalanche (AVAX)',    'token': 'USDT', 'chain': 'Avalanche C-Chain'},
    'bsc':       {'label': 'BNB Smart Chain (BSC)', 'token': 'USDT', 'chain': 'BSC'},
}

# Wallet addresses for each network — set these in .env
CRYPTO_WALLETS = {
    'aptos':     os.environ.get('CRYPTO_WALLET_APTOS', ''),
    'avalanche': os.environ.get('CRYPTO_WALLET_AVALANCHE', ''),
    'bsc':       os.environ.get('CRYPTO_WALLET_BSC', ''),
}


# Hot-wallet private keys for automated withdrawals — KEEP THESE SECRET
# Each key must control the matching CRYPTO_WALLET_* address above
WITHDRAWAL_KEYS = {
    'aptos':     os.environ.get('WITHDRAWAL_KEY_APTOS',     ''),
    'avalanche': os.environ.get('WITHDRAWAL_KEY_AVALANCHE', ''),
    'bsc':       os.environ.get('WITHDRAWAL_KEY_BSC',       ''),
}

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
        crypto_network TEXT,
        crypto_address TEXT,
        crypto_name TEXT,
        referral_bonus_awarded INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS ads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        platform TEXT NOT NULL,
        target_url TEXT NOT NULL,
        task_type TEXT NOT NULL,
        reward_per_task REAL DEFAULT 0.05,
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
        network TEXT,
        status TEXT DEFAULT 'pending',
        tx_hash TEXT,
        failure_reason TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        processed_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS crypto_deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        network TEXT NOT NULL,
        tx_hash TEXT UNIQUE NOT NULL,
        amount REAL NOT NULL,
        status TEXT DEFAULT 'pending',
        confirmed_at TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    ''')

    # ── Migrations for databases created by earlier versions ─────────────
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

    # Migrate old paystack columns to crypto columns
    _add_col_if_missing('users', 'crypto_network', 'TEXT')
    _add_col_if_missing('users', 'crypto_address', 'TEXT')
    _add_col_if_missing('users', 'crypto_name', 'TEXT')
    _add_col_if_missing('withdrawals', 'tx_hash', 'TEXT')
    _add_col_if_missing('withdrawals', 'network', 'TEXT')
    _add_col_if_missing('withdrawals', 'failure_reason', 'TEXT')
    _add_col_if_missing('withdrawals', 'processed_at', 'TEXT')

    _create_index_if_missing('ads', 'idx_ads_user', 'user_id')
    _create_index_if_missing('ads', 'idx_ads_status', 'status')
    _create_index_if_missing('task_completions', 'idx_tc_worker', 'worker_id')
    _create_index_if_missing('task_completions', 'idx_tc_ad', 'ad_id')
    _create_index_if_missing('transactions', 'idx_tx_user', 'user_id')
    _create_index_if_missing('notifications', 'idx_notif_user', 'user_id, read')
    _create_index_if_missing('withdrawals', 'idx_wdr_status', 'status')
    _create_index_if_missing('crypto_deposits', 'idx_cdep_user', 'user_id')

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


def _maybe_upgrade_password_hash(db, user_id: int, plaintext: str, stored: str):
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


def check_and_award_referral_bonus(db, user_id):
    user = db.execute('SELECT referred_by, referral_bonus_awarded FROM users WHERE id=?', (user_id,)).fetchone()
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
    platform = ad['platform'].lower()
    task_type = ad['task_type'].lower()

    if not proof_link or not proof_link.startswith(('http://', 'https://')):
        return {'valid': False, 'error': 'Please provide a valid URL as proof.'}

    if task_type == 'follow':
        return verify_follow_task(platform, proof_link, ad['target_url'])
    elif task_type == 'like':
        return verify_like_task(platform, proof_link)
    elif task_type == 'comment':
        return verify_comment_task(platform, proof_link)
    elif task_type == 'share':
        return verify_share_task(platform, proof_link)
    else:
        return {'valid': True, 'error': ''}


def verify_follow_task(platform, proof_link, target_url):
    if platform == 'instagram':
        if 'instagram.com/' in proof_link:
            return {'valid': True, 'error': ''}
        return {'valid': False, 'error': 'Please provide an Instagram URL as proof.'}
    elif platform == 'tiktok':
        if 'tiktok.com/' in proof_link:
            return {'valid': True, 'error': ''}
        return {'valid': False, 'error': 'Please provide a TikTok URL as proof.'}
    elif platform in ('twitter', 'x'):
        if 'twitter.com/' in proof_link or 'x.com/' in proof_link:
            return {'valid': True, 'error': ''}
        return {'valid': False, 'error': 'Please provide a Twitter/X URL as proof.'}
    elif platform == 'facebook':
        if 'facebook.com/' in proof_link:
            return {'valid': True, 'error': ''}
        return {'valid': False, 'error': 'Please provide a Facebook URL as proof.'}
    elif platform == 'youtube':
        if 'youtube.com/' in proof_link or 'youtu.be/' in proof_link:
            return {'valid': True, 'error': ''}
        return {'valid': False, 'error': 'Please provide a YouTube URL as proof.'}
    return {'valid': True, 'error': ''}


def verify_like_task(platform, proof_link):
    return verify_follow_task(platform, proof_link, '')


def verify_comment_task(platform, proof_link):
    return verify_follow_task(platform, proof_link, '')


def verify_share_task(platform, proof_link):
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
        'CRYPTO_NETWORKS': CRYPTO_NETWORKS,
        'CRYPTO_WALLETS': CRYPTO_WALLETS,
        'CRYPTO_ENABLED': any(CRYPTO_WALLETS.values()),
    }


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
            add_notification(
                db, referrer['id'],
                f'👤 {username} signed up using your referral code! '
                f'Bonus will be awarded when they activate their account by spending {CURRENCY_SYMBOL}1.'
            )
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
            return jsonify({'success': False, 'errors': ['Invalid credentials.']}), 401
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
    check_and_award_referral_bonus(db, uid)
    add_notification(db, uid, f'📢 Ad "{ad["title"]}" is now live!')

    users = db.execute('SELECT id FROM users WHERE id != ?', (uid,)).fetchall()
    for u in users:
        add_notification(db, u['id'], f'📢 New task available: "{ad["title"]}" on {ad["platform"]}')

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

    verification_result = verify_task_completion(ad, proof_link, uid)
    if not verification_result['valid']:
        return jsonify({'success': False, 'error': verification_result['error']})

    db.execute(
        'INSERT INTO task_completions (ad_id,worker_id,proof_link,status,reward,reviewed_at) '
        'VALUES (?,?,?,?,?,?)',
        (ad_id, uid, proof_link, 'completed', reward, now)
    )
    db.execute(
        'UPDATE ads SET budget_spent=budget_spent+?, followers_gained=followers_gained+1 '
        'WHERE id=?',
        (LISTER_COST_PER_TASK, ad_id)
    )
    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (reward, uid))
    add_transaction(db, uid, 'earn', reward, f'Task completed: {ad["title"]}')
    add_notification(db, uid,
                     f'✅ Task completed! +{CURRENCY_SYMBOL}{reward:.2f} added to your wallet for "{ad["title"]}"')
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
    pending_deposits = db.execute(
        'SELECT * FROM crypto_deposits WHERE user_id=? ORDER BY created_at DESC LIMIT 10', (uid,)
    ).fetchall()
    return render_template('wallet.html', transactions=txs, withdrawals=wdrs,
                           pending_deposits=pending_deposits)


@app.route('/wallet/deposit', methods=['POST'])
@login_required
def deposit():
    """
    Automatic on-chain deposit verification.
    User submits their TX hash; we immediately query the chain,
    verify the USDT transfer reached our wallet, and credit the balance.
    No admin approval required.
    """
    db = get_db()
    uid = session['user_id']
    payload = request.get_json(silent=True) or {}
    network  = (payload.get('network')  or '').strip().lower()
    tx_hash  = (payload.get('tx_hash')  or '').strip()

    if network not in CRYPTO_NETWORKS:
        return jsonify({'success': False, 'error': 'Invalid network selected.'}), 400
    if not tx_hash or len(tx_hash) < 10:
        return jsonify({'success': False, 'error': 'Please enter a valid transaction hash.'}), 400

    # Guard against double-crediting the same TX
    existing = db.execute(
        'SELECT id, status FROM crypto_deposits WHERE tx_hash=?', (tx_hash,)
    ).fetchone()
    if existing:
        if existing['status'] == 'confirmed':
            return jsonify({'success': False,
                            'error': 'This transaction has already been credited.'}), 400
        # Still pending from a previous attempt — allow retry
        dep_id = existing['id']
    else:
        dep_id = None

    platform_wallet = CRYPTO_WALLETS.get(network, '')
    if not platform_wallet:
        return jsonify({'success': False,
                        'error': f'Platform wallet not configured for {network}.'}), 500

    net_label = CRYPTO_NETWORKS[network]['label']

    # ── Insert / update deposit record as 'verifying' ────────────────────
    now = datetime.now(timezone.utc).isoformat()
    if dep_id:
        db.execute('UPDATE crypto_deposits SET status=? WHERE id=?', ('verifying', dep_id))
    else:
        db.execute(
            'INSERT INTO crypto_deposits (user_id, network, tx_hash, amount, status, created_at) '
            'VALUES (?,?,?,0,?,?)',
            (uid, network, tx_hash, 'verifying', now)
        )
        dep_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    db.commit()

    # ── Hit the chain synchronously (Flask worker thread is fine for this) ─
    result = _chain_verify_deposit(
        network=network,
        tx_hash=tx_hash,
        expected_recipient=platform_wallet,
        min_amount_usd=0.01,
    )

    if not result['ok']:
        db.execute(
            'UPDATE crypto_deposits SET status=? WHERE id=?',
            ('failed', dep_id)
        )
        db.commit()
        return jsonify({'success': False, 'error': result['error']}), 400

    verified_amount = round(result['amount'], 6)

    # ── Credit the user ───────────────────────────────────────────────────
    db.execute(
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

    updated_balance = db.execute(
        'SELECT balance FROM users WHERE id=?', (uid,)
    ).fetchone()['balance']

    return jsonify({
        'success': True,
        'message': f'${verified_amount:.2f} USDT confirmed and credited to your balance!',
        'amount': verified_amount,
        'balance': updated_balance,
    })


@app.route('/wallet/crypto_address', methods=['POST'])
@login_required
def save_crypto_address():
    """Save the user's crypto withdrawal address."""
    db = get_db()
    uid = session['user_id']
    network = request.form.get('network', '').strip().lower()
    address = request.form.get('address', '').strip()
    name = request.form.get('name', '').strip()

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


@app.route('/wallet/crypto_address', methods=['DELETE'])
@login_required
def remove_crypto_address():
    """Clear the user's saved crypto withdrawal address."""
    db = get_db()
    db.execute(
        'UPDATE users SET crypto_network=NULL, crypto_address=NULL, crypto_name=NULL WHERE id=?',
        (session['user_id'],)
    )
    db.commit()
    return jsonify({'success': True})


@app.route('/wallet/withdraw', methods=['POST'])
@login_required
def withdraw():
    """
    Automatic on-chain withdrawal.
    Deducts from user balance immediately, then signs and broadcasts a
    USDT transfer to the user's wallet address on-chain.
    The TX hash is stored; if broadcast fails the balance is refunded.
    """
    db = get_db()
    uid = session['user_id']
    amount = safe_float(request.form.get('amount'), 0)
    user   = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

    # ── Validation ────────────────────────────────────────────────────────
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

    # ── Deduct balance & record as processing ─────────────────────────────
    db.execute('UPDATE users SET balance=balance-? WHERE id=?', (amount, uid))
    db.execute(
        'INSERT INTO withdrawals (user_id,amount,method,account,network,status) '
        'VALUES (?,?,?,?,?,?)',
        (uid, amount, f'USDT ({network_label})', to_address, network, 'processing')
    )
    wdr_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    add_transaction(db, uid, 'withdrawal', amount,
                    f'Withdrawal via USDT {network_label}', status='processing')
    add_notification(db, uid,
        f'⏳ Sending {CURRENCY_SYMBOL}{amount:.2f} USDT via {network_label} to your wallet…')
    db.commit()

    # ── Broadcast on-chain ────────────────────────────────────────────────
    # Run in the same request — typical EVM broadcast is <2 s
    result = _chain_send_usdt(
        network=network,
        private_key=private_key,
        to_address=to_address,
        amount_usd=amount,
    )

    now = datetime.now(timezone.utc).isoformat()

    if result['ok']:
        tx_hash = result['tx_hash']
        db.execute(
            'UPDATE withdrawals SET status=?, tx_hash=?, processed_at=? WHERE id=?',
            ('approved', tx_hash, now, wdr_id)
        )
        # Update matching transaction record to completed
        db.execute(
            "UPDATE transactions SET status='completed' "
            "WHERE user_id=? AND type='withdrawal' AND status='processing' "
            "ORDER BY id DESC LIMIT 1",
            (uid,)
        )
        add_notification(db, uid,
            f'✅ {CURRENCY_SYMBOL}{amount:.2f} USDT sent on {network_label}! '
            f'TX: {tx_hash[:24]}...')
        db.commit()

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
        # Broadcast failed — refund balance
        db.execute(
            'UPDATE withdrawals SET status=?, failure_reason=?, processed_at=? WHERE id=?',
            ('failed', result['error'], now, wdr_id)
        )
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, uid))
        db.execute(
            "UPDATE transactions SET status='failed' "
            "WHERE user_id=? AND type='withdrawal' AND status='processing' "
            "ORDER BY id DESC LIMIT 1",
            (uid,)
        )
        add_notification(db, uid,
            f'❌ Withdrawal of {CURRENCY_SYMBOL}{amount:.2f} USDT failed: {result["error"][:80]}. '
            f'Your balance has been refunded.')
        db.commit()

        return jsonify({
            'success': False,
            'error': f'On-chain transfer failed: {result["error"]}',
        }), 502


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
    wdrs = db.execute(
        'SELECT w.*, u.username FROM withdrawals w JOIN users u ON w.user_id=u.id '
        'WHERE w.status IN ("pending", "failed") ORDER BY w.created_at DESC'
    ).fetchall()
    recent_wdrs = db.execute(
        'SELECT w.*, u.username FROM withdrawals w JOIN users u ON w.user_id=u.id '
        'WHERE w.status IN ("approved", "rejected", "processing") '
        'ORDER BY w.created_at DESC LIMIT 20'
    ).fetchall()
    all_ads = db.execute(
        'SELECT a.*, u.username as owner_name FROM ads a '
        'JOIN users u ON a.user_id=u.id ORDER BY a.created_at DESC'
    ).fetchall()
    # Recent crypto deposits (auto-verified — shown for audit purposes)
    pending_deposits = db.execute(
        'SELECT cd.*, u.username FROM crypto_deposits cd '
        'JOIN users u ON cd.user_id=u.id '
        'ORDER BY cd.created_at DESC LIMIT 50'
    ).fetchall()
    total_users = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    total_ads = db.execute('SELECT COUNT(*) FROM ads').fetchone()[0]
    total_vol = db.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions'
    ).fetchone()[0]
    return render_template(
        'admin.html',
        users=users, withdrawals=wdrs, recent_withdrawals=recent_wdrs, ads=all_ads,
        pending_deposits=pending_deposits,
        total_users=total_users, total_ads=total_ads, total_vol=total_vol
    )




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
            f'❌ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} rejected. Amount refunded.'
        )
        db.commit()
        return jsonify({'success': True, 'status': 'rejected'})

    # Approve: mark as approved for manual crypto payout
    db.execute(
        'UPDATE withdrawals SET status=?, processed_at=? WHERE id=?',
        ('approved', now, wdr_id)
    )
    add_notification(
        db, wr['user_id'],
        f'✅ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} USDT approved. '
        f'Payment will be sent to your crypto address.'
    )
    db.commit()
    return jsonify({'success': True, 'status': 'approved'})


@app.route('/admin/deposit_user', methods=['POST'])
@app.route('/admin/deposit', methods=['POST'])
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
        user = db.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found.'}), 404
        add_notification(db, user_id, f'📢 {message}')
    else:
        users = db.execute('SELECT id FROM users').fetchall()
        for u in users:
            add_notification(db, u['id'], f'📢 {message}')

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


# ── Analytics ───────────────────────────────────────────────────────────────
@app.route('/analytics')
@login_required
def analytics():
    db = get_db()
    uid = session['user_id']

    ads_rows = db.execute(
        'SELECT * FROM ads WHERE user_id=? ORDER BY created_at DESC', (uid,)
    ).fetchall()

    # Convert to dicts for safe JSON serialization in template
    ads = [dict(a) for a in ads_rows]

    if not ads:
        return render_template('analytics.html', ads=[], summary=None,
                               currency=CURRENCY_SYMBOL)

    total_budget = sum(float(ad['budget'] or 0) for ad in ads)
    total_spent = sum(float(ad['budget_spent'] or 0) for ad in ads)
    total_followers = sum(int(ad['followers_gained'] or 0) for ad in ads)
    active_campaigns = sum(1 for ad in ads if ad['status'] == 'active')

    total_completions = db.execute(
        'SELECT COUNT(*) FROM task_completions WHERE ad_id IN '
        '(SELECT id FROM ads WHERE user_id=?) AND status="completed"',
        (uid,)
    ).fetchone()[0]

    roi = 0.0
    if total_spent > 0:
        roi = round((total_followers * LISTER_COST_PER_TASK - total_spent) / total_spent * 100, 2)

    avg_cost = round(total_spent / total_followers, 4) if total_followers > 0 else 0.0

    summary = {
        'total_ads': len(ads),
        'active_campaigns': active_campaigns,
        'total_budget': round(total_budget, 2),
        'total_spent': round(total_spent, 2),
        'total_followers': total_followers,
        'total_completions': total_completions,
        'roi': roi,
        'avg_cost_per_follower': avg_cost,
    }

    return render_template('analytics.html', ads=ads, summary=summary,
                           currency=CURRENCY_SYMBOL, cost_per_task=LISTER_COST_PER_TASK)


@app.route('/api/analytics/<int:ad_id>')
@login_required
def api_analytics(ad_id):
    db = get_db()
    uid = session['user_id']

    ad = db.execute('SELECT * FROM ads WHERE id=? AND user_id=?', (ad_id, uid)).fetchone()
    if not ad:
        return jsonify({'success': False, 'error': 'Ad not found'}), 404

    completions = db.execute(
        'SELECT * FROM task_completions WHERE ad_id=? ORDER BY submitted_at DESC',
        (ad_id,)
    ).fetchall()

    total_tasks = len(completions)
    completed_tasks = sum(1 for c in completions if c['status'] == 'completed')
    rejected_tasks = sum(1 for c in completions if c['status'] == 'rejected')

    completion_rate = round(completed_tasks / total_tasks * 100, 2) if total_tasks > 0 else 0
    total_paid = sum(float(c['reward'] or 0) for c in completions if c['status'] == 'completed')

    trend_data = {}
    for completion in completions:
        date = completion['submitted_at'][:10]
        if date not in trend_data:
            trend_data[date] = {'total': 0, 'completed': 0}
        trend_data[date]['total'] += 1
        if completion['status'] == 'completed':
            trend_data[date]['completed'] += 1

    budget_spent = float(ad['budget_spent'] or 0)
    followers_gained = int(ad['followers_gained'] or 0)
    followers_target = int(ad['followers_target'] or 1)

    roi = 0.0
    if budget_spent > 0:
        roi = round((followers_gained * LISTER_COST_PER_TASK - budget_spent) / budget_spent * 100, 2)

    return jsonify({
        'success': True,
        'ad': {
            'id': ad['id'],
            'title': ad['title'],
            'platform': ad['platform'],
            'task_type': ad['task_type'],
            'budget': ad['budget'],
            'budget_spent': budget_spent,
            'followers_target': followers_target,
            'followers_gained': followers_gained,
            'status': ad['status'],
        },
        'metrics': {
            'total_tasks': total_tasks,
            'completed_tasks': completed_tasks,
            'rejected_tasks': rejected_tasks,
            'completion_rate': completion_rate,
            'total_paid': round(total_paid, 2),
            'avg_reward': round(total_paid / completed_tasks, 4) if completed_tasks > 0 else 0,
            'target_completion_rate': round(followers_gained / followers_target * 100, 2),
            'roi': roi,
        },
        'trend': sorted(trend_data.items()),
    })


@app.route('/api/analytics/performance')
@login_required
def api_analytics_performance():
    db = get_db()
    uid = session['user_id']

    ads_rows = db.execute(
        'SELECT id, title, platform, task_type, followers_target, followers_gained, '
        'budget, budget_spent, status FROM ads WHERE user_id=? ORDER BY followers_gained DESC LIMIT 10',
        (uid,)
    ).fetchall()

    performance_data = []
    for ad in ads_rows:
        followers_gained = int(ad['followers_gained'] or 0)
        followers_target = int(ad['followers_target'] or 1)
        budget_spent = float(ad['budget_spent'] or 0)

        roi = 0.0
        if budget_spent > 0:
            roi = round((followers_gained * LISTER_COST_PER_TASK - budget_spent) / budget_spent * 100, 2)

        performance_data.append({
            'title': ad['title'],
            'platform': ad['platform'],
            'followers_target': followers_target,
            'followers_gained': followers_gained,
            'completion_rate': round(followers_gained / followers_target * 100, 1) if followers_target > 0 else 0,
            'budget': ad['budget'],
            'cost_per_follower': round(budget_spent / followers_gained, 4) if followers_gained > 0 else 0,
            'roi': roi,
            'status': ad['status'],
        })

    return jsonify({'success': True, 'performance': performance_data})


# ─────────────────────────────────────────────────────────────────────────────
# Error handlers
# ─────────────────────────────────────────────────────────────────────────────
@app.errorhandler(404)
def _not_found(_e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Not found'}), 404
    return render_template('error.html', code=404, message='Page not found.'), 404


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
