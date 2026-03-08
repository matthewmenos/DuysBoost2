import sqlite3, secrets, hashlib, os
from datetime import datetime
from functools import wraps

import requests
from authlib.integrations.flask_client import OAuth
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, g

app = Flask(__name__)
app.secret_key = 'duys_boost_secret_2024_xK9mP'
DB_PATH = os.path.join(os.path.dirname(__file__), 'duys_boost.db')

# Currency & pricing (Ghana Cedis)
CURRENCY_CODE = 'GHS'
CURRENCY_SYMBOL = 'GH₵'
WORKER_REWARD_PER_TASK = 0.30   # 30 pesewas earned per completed follower task
LISTER_COST_PER_TASK = 0.70     # 70 pesewas spent per follower gained

# External integrations
PAYSTACK_PUBLIC_KEY = os.environ.get('PAYSTACK_PUBLIC_KEY', '')
PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', '')

GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')
APPLE_CLIENT_ID = os.environ.get('APPLE_CLIENT_ID', '')
APPLE_CLIENT_SECRET = os.environ.get('APPLE_CLIENT_SECRET', '')

oauth = OAuth(app)
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name='google',
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )

if APPLE_CLIENT_ID and APPLE_CLIENT_SECRET:
    oauth.register(
        name='apple',
        client_id=APPLE_CLIENT_ID,
        client_secret=APPLE_CLIENT_SECRET,
        server_metadata_url='https://appleid.apple.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'name email'},
    )

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')


# ── DB ────────────────────────────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
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
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    ''')
    # Create admin
    existing = db.execute('SELECT id FROM users WHERE username=?', ('admin',)).fetchone()
    if not existing:
        db.execute('INSERT INTO users (username,email,password,is_admin,balance,referral_code) VALUES (?,?,?,1,1000.0,?)',
                   ('admin','admin@duysboost.com', hash_password('admin123'), secrets.token_hex(5)))
    db.commit()
    db.close()

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def add_notification(db, user_id, message):
    db.execute('INSERT INTO notifications (user_id, message) VALUES (?,?)', (user_id, message))

def add_transaction(db, user_id, type_, amount, description, status='completed'):
    db.execute('INSERT INTO transactions (user_id,type,amount,description,status) VALUES (?,?,?,?,?)',
               (user_id, type_, amount, description, status))

# ── AUTH ──────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if 'user_id' not in session:
        return None
    return get_db().execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()

@app.context_processor
def inject_user():
    return {
        'current_user': get_current_user(),
        'CURRENCY_SYMBOL': CURRENCY_SYMBOL,
        'CURRENCY_CODE': CURRENCY_CODE,
        'PAYSTACK_PUBLIC_KEY': PAYSTACK_PUBLIC_KEY,
    }

# ── ROUTES ────────────────────────────────────────────────────────────────────


@app.route('/signup', methods=['GET','POST'])
def signup():
    if request.method == 'POST':
        db = get_db()
        username = request.form.get('username','').strip()
        email = request.form.get('email','').strip()
        password = request.form.get('password','')
        confirm = request.form.get('confirm_password','')
        ref_code = request.form.get('referral_code','').strip()
        errors = []
        if len(username) < 3:
            errors.append('Username must be at least 3 characters.')
        if len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        # Basic password complexity: upper, lower, digit
        if not any(c.islower() for c in password) or not any(c.isupper() for c in password) or not any(c.isdigit() for c in password):
            errors.append('Password must include upper, lower case letters and numbers.')
        if password != confirm:
            errors.append('Passwords do not match.')
        if db.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone():
            errors.append('Email already registered.')
        if db.execute('SELECT id FROM users WHERE username=?',(username,)).fetchone():
            errors.append('Username already taken.')
        if errors:
            return jsonify({'success':False,'errors':errors})
        referrer = db.execute('SELECT * FROM users WHERE referral_code=?',(ref_code,)).fetchone() if ref_code else None
        db.execute('INSERT INTO users (username,email,password,referred_by,referral_code) VALUES (?,?,?,?,?)',
                   (username,email,hash_password(password),referrer['id'] if referrer else None,secrets.token_hex(5)))
        db.commit()
        user = db.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
        if referrer:
            bonus_amount = 1.0
            db.execute('UPDATE users SET balance=balance+? WHERE id=?',(bonus_amount,referrer['id']))
            add_notification(db, referrer['id'], f'🎉 {username} signed up using your referral! +{CURRENCY_SYMBOL}{bonus_amount:.2f} added.')
            add_transaction(db, referrer['id'], 'earn', bonus_amount, f'Referral bonus from {username}')
        add_notification(db, user['id'], '👋 Welcome to DUYS Boost! Your account is ready.')
        db.commit()
        session['user_id'] = user['id']
        return jsonify({'success':True,'redirect':url_for('dashboard')})
    return render_template('auth.html', mode='signup')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        db = get_db()
        identifier = request.form.get('identifier','').strip()
        password   = request.form.get('password','')
        user = db.execute('SELECT * FROM users WHERE email=? OR username=?',(identifier,identifier)).fetchone()
        if not user or user['password'] != hash_password(password):
            return jsonify({'success':False,'errors':['Invalid credentials.']})
        session['user_id'] = user['id']
        return jsonify({'success':True,'redirect':url_for('dashboard')})
    return render_template('auth.html', mode='login')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/auth/google')
def google_login_route():
    # Redirect to Google OAuth if configured, otherwise fall back to normal login
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
    email = (userinfo or {}).get('email')
    name = (userinfo or {}).get('name') or (email.split('@')[0] if email else None)
    if not email:
        return redirect(url_for('login'))
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
    if not user:
        base = (name or email.split('@')[0]).replace(' ', '').lower()
        username = base or 'user'
        i = 1
        while db.execute('SELECT id FROM users WHERE username=?',(username,)).fetchone():
            username = f'{base}{i}'
            i += 1
        db.execute('INSERT INTO users (username,email,password,referral_code) VALUES (?,?,?,?)',
                   (username, email, None, secrets.token_hex(5)))
        db.commit()
        user = db.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
        add_notification(db, user['id'], '🔐 Signed in with Google.')
        db.commit()
    session['user_id'] = user['id']
    return redirect(url_for('dashboard'))

@app.route('/auth/apple')
def apple_login_route():
    if not getattr(oauth, 'apple', None):
        return redirect(url_for('login'))
    redirect_uri = url_for('apple_auth_callback', _external=True)
    return oauth.apple.authorize_redirect(redirect_uri)

@app.route('/auth/apple/callback', methods=['GET','POST'])
def apple_auth_callback():
    if not getattr(oauth, 'apple', None):
        return redirect(url_for('login'))
    try:
        token = oauth.apple.authorize_access_token()
        userinfo = token.get('userinfo') or {}
    except Exception:
        return redirect(url_for('login'))
    email = userinfo.get('email')
    name = userinfo.get('name') or (email.split('@')[0] if email else None)
    if not email:
        return redirect(url_for('login'))
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
    if not user:
        base = (name or email.split('@')[0]).replace(' ', '').lower()
        username = base or 'user'
        i = 1
        while db.execute('SELECT id FROM users WHERE username=?',(username,)).fetchone():
            username = f'{base}{i}'
            i += 1
        db.execute('INSERT INTO users (username,email,password,referral_code) VALUES (?,?,?,?)',
                   (username, email, None, secrets.token_hex(5)))
        db.commit()
        user = db.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
        add_notification(db, user['id'], '🔐 Signed in with Apple.')
        db.commit()
    session['user_id'] = user['id']
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    uid = session['user_id']
    ads = db.execute('SELECT * FROM ads WHERE user_id=? ORDER BY created_at DESC LIMIT 5',(uid,)).fetchall()
    recent_tasks = db.execute('''SELECT tc.*, a.title as ad_title FROM task_completions tc
        JOIN ads a ON tc.ad_id=a.id WHERE tc.worker_id=? ORDER BY tc.submitted_at DESC LIMIT 5''',(uid,)).fetchall()
    total_earned = db.execute('SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND type="earn"',(uid,)).fetchone()[0]
    total_spent  = db.execute('SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND type="spend"',(uid,)).fetchone()[0]
    unread       = db.execute('SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0',(uid,)).fetchone()[0]
    done_ids     = [r[0] for r in db.execute('SELECT ad_id FROM task_completions WHERE worker_id=?',(uid,)).fetchall()]
    available_ads = db.execute('SELECT * FROM ads WHERE status="active" AND user_id!=? ORDER BY created_at DESC LIMIT 10',(uid,)).fetchall()
    available_ads = [a for a in available_ads if a['id'] not in done_ids and a['budget_spent'] < a['budget']]
    return render_template('dashboard.html', ads=ads, recent_tasks=recent_tasks,
        total_earned=total_earned, total_spent=total_spent, unread=unread, available_ads=available_ads)

@app.route('/ads')
@login_required
def ads():
    db = get_db()
    user_ads = db.execute('SELECT * FROM ads WHERE user_id=? ORDER BY created_at DESC',(session['user_id'],)).fetchall()
    return render_template('ads.html', ads=user_ads, worker_reward=WORKER_REWARD_PER_TASK, lister_cost=LISTER_COST_PER_TASK)

@app.route('/ads/create', methods=['POST'])
@login_required
def create_ad():
    db = get_db()
    uid = session['user_id']
    user = db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    followers_target = int(request.form.get('followers_target',0))
    if followers_target <= 0:
        return jsonify({'success':False,'error':'Please enter a valid followers target.'})
    # Enforce pricing: advertiser spends 0.70 GHS per follower, worker earns 0.30 GHS
    budget = followers_target * LISTER_COST_PER_TASK
    if budget <= 0 or budget > user['balance']:
        return jsonify({'success':False,'error':'Insufficient balance for this followers target.'})
    db.execute('INSERT INTO ads (user_id,title,platform,target_url,task_type,reward_per_task,budget,followers_target) VALUES (?,?,?,?,?,?,?,?)',
               (uid,
                request.form.get('title'),
                request.form.get('platform'),
                request.form.get('target_url'),
                request.form.get('task_type'),
                WORKER_REWARD_PER_TASK,
                budget,
                followers_target))
    db.execute('UPDATE users SET balance=balance-? WHERE id=?',(budget,uid))
    ad = db.execute('SELECT * FROM ads WHERE user_id=? ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
    add_transaction(db, uid, 'spend', budget, f'Budget for ad: {ad["title"]}')
    add_notification(db, uid, f'📢 Ad "{ad["title"]}" is now live!')
    db.commit()
    return jsonify({'success':True})

@app.route('/ads/<int:ad_id>/toggle', methods=['POST'])
@login_required
def toggle_ad(ad_id):
    db = get_db()
    ad = db.execute('SELECT * FROM ads WHERE id=?',(ad_id,)).fetchone()
    if not ad or ad['user_id'] != session['user_id']: return jsonify({'success':False})
    new_status = 'paused' if ad['status'] == 'active' else 'active'
    db.execute('UPDATE ads SET status=? WHERE id=?',(new_status,ad_id))
    db.commit()
    return jsonify({'success':True,'status':new_status})

@app.route('/tasks')
@login_required
def tasks():
    db = get_db()
    uid = session['user_id']
    done_ids = [r[0] for r in db.execute('SELECT ad_id FROM task_completions WHERE worker_id=?',(uid,)).fetchall()]
    available = db.execute('SELECT * FROM ads WHERE status="active" AND user_id!=? ORDER BY created_at DESC',(uid,)).fetchall()
    available = [a for a in available if a['id'] not in done_ids and a['budget_spent'] < a['budget']]
    my_tasks  = db.execute('''SELECT tc.*, a.title as ad_title FROM task_completions tc
        JOIN ads a ON tc.ad_id=a.id WHERE tc.worker_id=? ORDER BY tc.submitted_at DESC''',(uid,)).fetchall()
    return render_template('tasks.html', available=available, my_tasks=my_tasks)

@app.route('/tasks/submit', methods=['POST'])
@login_required
def submit_task():
    db = get_db()
    uid = session['user_id']
    ad_id = int(request.form.get('ad_id'))
    proof_link = request.form.get('proof_link','').strip()
    ad = db.execute('SELECT * FROM ads WHERE id=?',(ad_id,)).fetchone()
    if not ad: return jsonify({'success':False,'error':'Ad not found.'})
    if ad['user_id'] == uid: return jsonify({'success':False,'error':'Cannot complete your own ad.'})
    if db.execute('SELECT id FROM task_completions WHERE ad_id=? AND worker_id=?',(ad_id,uid)).fetchone():
        return jsonify({'success':False,'error':'Already submitted for this ad.'})
    if not proof_link.startswith('http'): return jsonify({'success':False,'error':'Please enter a valid proof URL.'})
    now = datetime.utcnow().isoformat()
    reward = WORKER_REWARD_PER_TASK
    db.execute('INSERT INTO task_completions (ad_id,worker_id,proof_link,status,reward,reviewed_at) VALUES (?,?,?,?,?,?)',
               (ad_id,uid,proof_link,'approved',reward,now))
    db.execute('UPDATE users SET balance=balance+? WHERE id=?',(reward,uid))
    db.execute('UPDATE ads SET budget_spent=budget_spent+?, followers_gained=followers_gained+1 WHERE id=?',
               (LISTER_COST_PER_TASK,ad_id))
    updated_ad = db.execute('SELECT * FROM ads WHERE id=?',(ad_id,)).fetchone()
    if updated_ad['budget_spent'] >= updated_ad['budget'] or (updated_ad['followers_target'] and updated_ad['followers_gained'] >= updated_ad['followers_target']):
        db.execute('UPDATE ads SET status="completed" WHERE id=?',(ad_id,))
        add_notification(db, ad['user_id'], f'✅ Your ad "{ad["title"]}" has reached its goal!')
    add_transaction(db, uid, 'earn', reward, f'Task completed: {ad["title"]}')
    add_notification(db, uid, f'💰 +{CURRENCY_SYMBOL}{reward:.2f} earned for completing "{ad["title"]}"')
    add_notification(db, ad['user_id'], f'📈 New follower gained on "{ad["title"]}"!')
    db.commit()
    return jsonify({'success':True,'earned':ad['reward_per_task']})

@app.route('/wallet')
@login_required
def wallet():
    db = get_db()
    uid = session['user_id']
    txs  = db.execute('SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC',(uid,)).fetchall()
    wdrs = db.execute('SELECT * FROM withdrawals WHERE user_id=? ORDER BY created_at DESC',(uid,)).fetchall()
    return render_template('wallet.html', transactions=txs, withdrawals=wdrs)

@app.route('/wallet/deposit', methods=['POST'])
@login_required
def deposit():
    # This endpoint is called from the Paystack inline callback to confirm and credit deposits.
    if not PAYSTACK_SECRET_KEY:
        return jsonify({'success':False,'error':'Deposits are not available. Paystack is not configured.'}), 503
    db = get_db()
    uid = session['user_id']
    payload = request.get_json(silent=True) or {}
    reference = payload.get('reference')
    declared_amount = float(payload.get('amount', 0) or 0)
    if not reference or declared_amount <= 0:
        return jsonify({'success':False,'error':'Invalid deposit payload.'}), 400
    try:
        resp = requests.get(
            f'https://api.paystack.co/transaction/verify/{reference}',
            headers={'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}'},
            timeout=10,
        )
        body = resp.json()
    except Exception:
        return jsonify({'success':False,'error':'Failed to verify payment with Paystack.'}), 502
    if not body.get('status'):
        return jsonify({'success':False,'error':'Unable to verify payment.'}), 400
    data = body.get('data') or {}
    if data.get('status') != 'success' or data.get('currency') != CURRENCY_CODE:
        return jsonify({'success':False,'error':'Payment not successful.'}), 400
    paid_amount = (data.get('amount') or 0) / 100.0
    if paid_amount <= 0:
        return jsonify({'success':False,'error':'Invalid amount from payment gateway.'}), 400
    # Prevent double-credit: tag transactions with the Paystack reference
    existing = db.execute(
        'SELECT id FROM transactions WHERE description LIKE ?',
        (f'%{reference}%',)
    ).fetchone()
    if existing:
        user = db.execute('SELECT balance FROM users WHERE id=?',(uid,)).fetchone()
        return jsonify({'success':True,'balance':user['balance']})
    db.execute('UPDATE users SET balance=balance+? WHERE id=?',(paid_amount,uid))
    add_transaction(db, uid, 'deposit', paid_amount, f'Paystack deposit {reference}')
    add_notification(db, uid, f'💳 {CURRENCY_SYMBOL}{paid_amount:.2f} deposited to your wallet via Paystack.')
    db.commit()
    user = db.execute('SELECT balance FROM users WHERE id=?',(uid,)).fetchone()
    return jsonify({'success':True,'balance':user['balance']})

@app.route('/wallet/withdraw', methods=['POST'])
@login_required
def withdraw():
    db = get_db()
    uid = session['user_id']
    amount  = float(request.form.get('amount',0))
    method  = request.form.get('method','')
    account = request.form.get('account','')
    user = db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    if amount <= 0 or amount > user['balance']: return jsonify({'success':False,'error':'Insufficient balance.'})
    db.execute('UPDATE users SET balance=balance-? WHERE id=?',(amount,uid))
    db.execute('INSERT INTO withdrawals (user_id,amount,method,account) VALUES (?,?,?,?)',(uid,amount,method,account))
    add_transaction(db, uid, 'withdrawal', amount, f'Withdrawal via {method}', status='pending')
    add_notification(db, uid, f'🏦 Withdrawal of {CURRENCY_SYMBOL}{amount:.2f} via {method} submitted.')
    db.commit()
    updated = db.execute('SELECT balance FROM users WHERE id=?',(uid,)).fetchone()
    return jsonify({'success':True,'balance':updated['balance']})

@app.route('/notifications')
@login_required
def notifications():
    db = get_db()
    uid = session['user_id']
    notifs = db.execute('SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC',(uid,)).fetchall()
    db.execute('UPDATE notifications SET read=1 WHERE user_id=?',(uid,))
    db.commit()
    return render_template('notifications.html', notifications=notifs)

@app.route('/api/notifications/unread')
@login_required
def unread_count():
    db = get_db()
    uid = session['user_id']
    count  = db.execute('SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0',(uid,)).fetchone()[0]
    recent = db.execute('SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 5',(uid,)).fetchall()
    return jsonify({'count':count,'recent':[{'msg':n['message'],'time':n['created_at'][:16]} for n in recent]})

@app.route('/api/theme', methods=['POST'])
@login_required
def toggle_theme():
    db = get_db()
    uid = session['user_id']
    user = db.execute('SELECT theme FROM users WHERE id=?',(uid,)).fetchone()
    new_theme = 'light' if user['theme'] == 'dark' else 'dark'
    db.execute('UPDATE users SET theme=? WHERE id=?',(new_theme,uid))
    db.commit()
    return jsonify({'theme':new_theme})

@app.route('/referral')
@login_required
def referral():
    db = get_db()
    uid = session['user_id']
    referred_users = db.execute('SELECT * FROM users WHERE referred_by=?',(uid,)).fetchall()
    total_earned   = len(referred_users) * 1.0
    return render_template('referral.html', referred_users=referred_users, total_earned=total_earned)

@app.route('/admin')
@login_required
def admin():
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if not user['is_admin']: return redirect(url_for('dashboard'))
    users       = db.execute('SELECT * FROM users ORDER BY created_at DESC').fetchall()
    wdrs        = db.execute('''SELECT w.*,u.username FROM withdrawals w JOIN users u ON w.user_id=u.id
                               WHERE w.status="pending"''').fetchall()
    all_ads     = db.execute('''SELECT a.*,u.username as owner_name FROM ads a JOIN users u ON a.user_id=u.id
                               ORDER BY a.created_at DESC''').fetchall()
    total_users = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    total_ads   = db.execute('SELECT COUNT(*) FROM ads').fetchone()[0]
    total_vol   = db.execute('SELECT COALESCE(SUM(amount),0) FROM transactions').fetchone()[0]
    return render_template('admin.html', users=users, withdrawals=wdrs, ads=all_ads,
        total_users=total_users, total_ads=total_ads, total_vol=total_vol)

@app.route('/admin/withdrawal/<int:wdr_id>/<action>', methods=['POST'])
@login_required
def process_withdrawal(wdr_id, action):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if not user['is_admin']: return jsonify({'success':False})
    wr = db.execute('SELECT * FROM withdrawals WHERE id=?',(wdr_id,)).fetchone()
    new_status = 'approved' if action == 'approve' else 'rejected'
    db.execute('UPDATE withdrawals SET status=? WHERE id=?',(new_status,wdr_id))
    if action == 'rejected':
        db.execute('UPDATE users SET balance=balance+? WHERE id=?',(wr['amount'],wr['user_id']))
        add_notification(db, wr['user_id'], f'❌ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} rejected. Amount refunded.')
    else:
        add_notification(db, wr['user_id'], f'✅ Withdrawal of {CURRENCY_SYMBOL}{wr["amount"]:.2f} approved!')
    db.commit()
    return jsonify({'success':True})

@app.route('/admin/deposit_user', methods=['POST'])
@login_required
def admin_deposit():
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
    if not user['is_admin']: return jsonify({'success':False})
    user_id = int(request.form.get('user_id'))
    amount  = float(request.form.get('amount'))
    db.execute('UPDATE users SET balance=balance+? WHERE id=?',(amount,user_id))
    add_transaction(db, user_id, 'deposit', amount, 'Admin deposit')
    add_notification(db, user_id, f'💰 Admin credited {CURRENCY_SYMBOL}{amount:.2f} to your account!')
    db.commit()
    return jsonify({'success':True})

@app.route('/api/activity')
@login_required
def activity_feed():
    db = get_db()
    rows = db.execute('''SELECT tc.reward, tc.submitted_at, u.username, a.title
        FROM task_completions tc JOIN users u ON tc.worker_id=u.id JOIN ads a ON tc.ad_id=a.id
        ORDER BY tc.submitted_at DESC LIMIT 10''').fetchall()
    return jsonify([{'worker':r['username'],'ad':r['title'],'reward':r['reward'],
                     'time':r['submitted_at'][11:19]} for r in rows])

if __name__ == '__main__':
    init_db()
    app.run(debug=True)
    # port = int(os.environ.get("PORT", 10000))
    # # Bind to 0.0.0.0 to be accessible externally
    # app.run(host="0.0.0.0", port=port)

