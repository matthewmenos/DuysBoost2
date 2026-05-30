"""
DUYS Boost — Social Media Boost Platform
App factory replacing the monolithic app.py.

Run locally:
    python app.py

With gunicorn (single-worker recommended — global.db syncs via R2):
    gunicorn "app:create_app()" -w 1 -b 0.0.0.0:5000
"""
import os
import secrets

import markupsafe
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import Flask, g, jsonify, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix
from security import init_security

# ── Load .env ────────────────────────────────────────────────────────────────
_base_dir    = os.path.dirname(__file__)
_dotenv_path = os.path.join(_base_dir, '.env')
if not os.path.exists(_dotenv_path):
    _example = os.path.join(_base_dir, '.env.example')
    if os.path.exists(_example):
        _dotenv_path = _example
load_dotenv(_dotenv_path, override=False)

# ── Business constants ────────────────────────────────────────────────────────
CURRENCY_CODE           = 'USD'
CURRENCY_SYMBOL         = '$'
WORKER_REWARD_PER_TASK  = 0.05
LISTER_COST_PER_TASK    = 0.10
REFERRAL_BONUS          = 0.50
REFERRAL_ACTIVATION_FEE = 1.00

CRYPTO_NETWORKS = {
    'aptos':     {'label': 'Aptos (APT)',          'token': 'USDT', 'chain': 'Aptos'},
    'avalanche': {'label': 'Avalanche (AVAX)',      'token': 'USDT', 'chain': 'Avalanche C-Chain'},
    'bsc':       {'label': 'BNB Smart Chain (BSC)', 'token': 'USDT', 'chain': 'BSC'},
}
CRYPTO_WALLETS = {
    'aptos':     os.environ.get('CRYPTO_WALLET_APTOS',     ''),
    'avalanche': os.environ.get('CRYPTO_WALLET_AVALANCHE', ''),
    'bsc':       os.environ.get('CRYPTO_WALLET_BSC',       ''),
}
WITHDRAWAL_KEYS = {
    'aptos':     os.environ.get('WITHDRAWAL_KEY_APTOS',     ''),
    'avalanche': os.environ.get('WITHDRAWAL_KEY_AVALANCHE', ''),
    'bsc':       os.environ.get('WITHDRAWAL_KEY_BSC',       ''),
}


def create_app() -> Flask:
    app = Flask(__name__)

    # Response compression
    try:
        from flask_compress import Compress
        app.config['COMPRESS_MIMETYPES'] = [
            'text/html', 'text/css', 'application/javascript', 'application/json'
        ]
        app.config['COMPRESS_MIN_SIZE'] = 500
        Compress(app)
    except ImportError:
        pass

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    if os.environ.get('OAUTHLIB_INSECURE_TRANSPORT', '0') == '1':
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

    _secret = os.environ.get('FLASK_SECRET_KEY', '')
    if not _secret:
        import logging as _log
        _log.getLogger(__name__).critical(
            'FLASK_SECRET_KEY is not set — using a random key. '
            'All sessions will be invalidated on every restart. '
            'Set FLASK_SECRET_KEY in your environment variables.'
        )
        _secret = secrets.token_hex(32)
    app.secret_key = _secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '0') == '1',
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
        DB_PATH=os.path.join(_base_dir, 'global.db'),
        R2_DB_BUCKET_NAME=os.environ.get('R2_DB_BUCKET_NAME', ''),
        R2_ACCESS_KEY_ID=os.environ.get('R2_ACCESS_KEY_ID', ''),
        R2_SECRET_ACCESS_KEY=os.environ.get('R2_SECRET_ACCESS_KEY', ''),
        R2_BUCKET_NAME=os.environ.get('R2_BUCKET_NAME', ''),
        R2_ACCOUNT_ID=os.environ.get('R2_ACCOUNT_ID', ''),
        R2_PUBLIC_URL=os.environ.get('R2_PUBLIC_URL', ''),
        REDIS_URL=os.environ.get('REDIS_URL', ''),
        DATABASE_URL='',  # not used (SQLite)
        CURRENCY_CODE=CURRENCY_CODE,
        CURRENCY_SYMBOL=CURRENCY_SYMBOL,
        WORKER_REWARD_PER_TASK=WORKER_REWARD_PER_TASK,
        LISTER_COST_PER_TASK=LISTER_COST_PER_TASK,
        REFERRAL_BONUS=REFERRAL_BONUS,
        REFERRAL_ACTIVATION_FEE=REFERRAL_ACTIVATION_FEE,
        CRYPTO_NETWORKS=CRYPTO_NETWORKS,
        CRYPTO_WALLETS=CRYPTO_WALLETS,
        WITHDRAWAL_KEYS=WITHDRAWAL_KEYS,
    )

    # ── OAuth ─────────────────────────────────────────────────────────────────
    google_id     = os.environ.get('GOOGLE_CLIENT_ID',     '')
    google_secret = os.environ.get('GOOGLE_CLIENT_SECRET', '')
    oauth = OAuth(app)
    if google_id and google_secret:
        oauth.register(
            name='google',
            client_id=google_id,
            client_secret=google_secret,
            server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
            client_kwargs={'scope': 'openid email profile'},
        )

    # ── DB teardown ───────────────────────────────────────────────────────────
    from helpers import get_current_user
    from db import close_db

    app.teardown_appcontext(close_db)

    # ── Jinja2 filters ────────────────────────────────────────────────────────
    import re as _re

    @app.template_filter('linkify')
    def linkify_filter(value):
        """Convert URLs in text to clickable links."""
        import re
        from markupsafe import Markup, escape as _esc
        if not value:
            return ''
        safe = str(_esc(value))
        url_pat = re.compile(r'(https?://[^\s<>"]+)')
        linked  = url_pat.sub(
            r'<a href="\1" target="_blank" rel="noopener noreferrer" '
            r'style="color:var(--accent);text-decoration:underline;word-break:break-all">\1</a>',
            safe
        )
        return Markup(linked)

    @app.template_filter('nl2br')
    def nl2br_filter(value):
        if value is None:
            return ''
        escaped = markupsafe.escape(value)
        return markupsafe.Markup(str(escaped).replace('\n', '<br>'))

    @app.template_filter('linkify_tags')
    def linkify_tags_filter(value):
        if value is None:
            return ''
        escaped = str(markupsafe.escape(value))
        escaped = _re.sub(
            r'#(\w+)',
            lambda m: f'<a href="/tag/{m.group(1).lower()}" class="post-tag">#{m.group(1)}</a>',
            escaped
        )
        escaped = _re.sub(
            r'@(\w+)',
            lambda m: f'<a href="/user/{m.group(1)}" class="post-mention">@{m.group(1)}</a>',
            escaped
        )
        return markupsafe.Markup(escaped.replace('\n', '<br>'))

    @app.context_processor
    def inject_user():
        user = get_current_user()
        # Convert to a plain dict so .get() works in templates
        # and missing columns don't crash Jinja attribute access
        if user is not None:
            try:
                user = dict(user)
                # Ensure all expected fields have safe defaults
                user.setdefault('balance', 0.0)
                user.setdefault('theme', 'dark')
                user.setdefault('is_admin', 0)
                user.setdefault('is_verified', 0)
                user.setdefault('verified_tier', 'blue')
                user.setdefault('avatar_url', None)
                user.setdefault('banner_url', None)
                user.setdefault('display_name', user.get('username', ''))
                user.setdefault('bio', '')
                user.setdefault('follower_count', 0)
                user.setdefault('following_count', 0)
                user.setdefault('post_count', 0)
                user.setdefault('unread_dm_count', 0)
                user.setdefault('username_changes', 0)
                user.setdefault('show_online', 1)
            except Exception:
                pass
        ctx  = {
            'current_user':  user,
            'CURRENCY_SYMBOL': CURRENCY_SYMBOL,
            'CURRENCY_CODE':   CURRENCY_CODE,
            'CRYPTO_NETWORKS': CRYPTO_NETWORKS,
            'CRYPTO_WALLETS':  CRYPTO_WALLETS,
            'CRYPTO_ENABLED':  any(CRYPTO_WALLETS.values()),
            'GOOGLE_ENABLED':  bool(os.environ.get('GOOGLE_CLIENT_ID')),
            'VAPID_PUBLIC_KEY': os.environ.get('VAPID_PUBLIC_KEY', ''),
            'PUSH_ENABLED':     bool(os.environ.get('VAPID_PUBLIC_KEY')),
            'open_report_count':  0,
            'pending_wdr_count':  0,
        }
        # Bump online_at on every authenticated page load (GET only, throttled to 30s via session)
        if user and request.method == 'GET' and not request.path.startswith('/static'):
            try:
                import time as _time
                _now_ts = _time.time()
                _last_bump = session.get('_online_bump', 0)
                if _now_ts - _last_bump > 30:
                    from db import get_db as _gdb
                    from datetime import datetime as _dt2, timezone as _tz2
                    _gdb().execute(
                        'UPDATE users SET online_at=? WHERE id=?',
                        (_dt2.now(_tz2.utc).isoformat(), user['id'])
                    )
                    _gdb().commit()
                    session['_online_bump'] = _now_ts
            except Exception:
                pass

        # For admin pages, inject badge counts
        if user and user['is_admin']:
            try:
                db = get_db()
                ctx['open_report_count'] = db.execute(
                    "SELECT COUNT(*) FROM reports WHERE status='open'"
                ).fetchone()[0]
                ctx['pending_wdr_count'] = db.execute(
                    "SELECT COUNT(*) FROM pending_withdrawals WHERE status='pending'"
                ).fetchone()[0]
            except Exception:
                pass
        return ctx

    # ── Blueprints ────────────────────────────────────────────────────────────
    from blueprints.auth       import bp as auth_bp
    from blueprints.auth_reset import bp as auth_reset_bp
    from blueprints.social  import bp as social_bp
    from blueprints.boost   import bp as boost_bp
    from blueprints.wallet  import bp as wallet_bp
    from blueprints.admin   import bp as admin_bp
    from blueprints.stories import bp as stories_bp
    from sse                import bp as sse_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(auth_reset_bp)
    app.register_blueprint(social_bp)
    app.register_blueprint(boost_bp)
    app.register_blueprint(wallet_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(stories_bp)
    app.register_blueprint(sse_bp)   # SSE streams

    # Start background story cleanup thread
    from blueprints.stories import start_cleanup_thread
    start_cleanup_thread(app)

    # ── Post scheduler — publishes scheduled posts when their time arrives ────
    def _run_post_scheduler(app_ref):
        import time as _t, sqlite3 as _sq3, logging as _lg
        _log2 = _lg.getLogger('post_scheduler')
        while True:
            _t.sleep(60)
            try:
                with app_ref.app_context():
                    from db import get_db as _gdb2
                    from datetime import datetime as _dt3, timezone as _tz3
                    _now = _dt3.now(_tz3.utc).isoformat()
                    _db2 = _gdb2()
                    _rows = _db2.execute(
                        "SELECT id FROM posts WHERE status='scheduled' AND scheduled_at <= ?",
                        (_now,)
                    ).fetchall()
                    for _r in _rows:
                        _db2.execute(
                            "UPDATE posts SET status='published', created_at=? WHERE id=?",
                            (_now, _r['id'])
                        )
                    if _rows:
                        _db2.commit()
                        _log2.info('Published %d scheduled post(s)', len(_rows))
            except Exception as _ex:
                _log2.warning('Post scheduler error: %s', _ex)

    import threading as _sched_t
    _sched_t.Thread(target=_run_post_scheduler, args=(app,), daemon=True, name='post-scheduler').start()

    # ── Daily boost reward payout ─────────────────────────────────────────────
    def _run_payout_scheduler(app_ref):
        """
        Daily batch payout: for every user with balance >= $1 and a crypto address,
        attempt on-chain USDT transfer. Falls back to creating a pending_withdrawals
        record for admin approval if private key is not configured.
        """
        import time as _t2, logging as _lg2
        _plog = _lg2.getLogger('payout_scheduler')
        while True:
            _t2.sleep(86400)  # once per day
            try:
                with app_ref.app_context():
                    import os as _os2
                    from db import get_db as _gdb3
                    from datetime import datetime as _dt4, timezone as _tz4
                    from helpers import add_transaction as _add_tx
                    _db3 = _gdb3()
                    _now4 = _dt4.now(_tz4.utc).isoformat()
                    _min_bal = 1.0

                    _eligible = _db3.execute(
                        "SELECT id, username, balance, crypto_network, crypto_address "
                        "FROM users WHERE balance >= ? AND crypto_address IS NOT NULL "
                        "AND crypto_address != ''",
                        (_min_bal,)
                    ).fetchall()

                    _sent = 0
                    for _u in _eligible:
                        _net  = _u['crypto_network'] or ''
                        _addr = _u['crypto_address']
                        _amt  = float(_u['balance'])
                        _pk   = _os2.environ.get(
                            f'PLATFORM_PRIVATE_KEY_{_net.upper()}', ''
                        )
                        if _pk:
                            try:
                                import crypto_engine as _ce
                                _res = _ce.send_usdt(_net, _pk, _addr, _amt)
                                if _res['ok']:
                                    _db3.execute('UPDATE users SET balance=0 WHERE id=?', (_u['id'],))
                                    _add_tx(_db3, _u['id'], 'withdraw', _amt,
                                            f'Auto payout {_amt:.2f} USDT to {_addr}')
                                    _db3.execute(
                                        "INSERT INTO pending_withdrawals "
                                        "(user_id,username,amount,method,address,status,created_at) "
                                        "VALUES (?,?,?,'auto',?,'completed',?)",
                                        (_u['id'], _u['username'], _amt, _addr, _now4)
                                    )
                                    _sent += 1
                                else:
                                    _plog.warning('Auto-payout failed uid=%s: %s', _u['id'], _res['error'])
                            except Exception as _pe:
                                _plog.warning('Auto-payout exception uid=%s: %s', _u['id'], _pe)
                        else:
                            # No private key — queue for admin
                            try:
                                _db3.execute(
                                    "INSERT OR IGNORE INTO pending_withdrawals "
                                    "(user_id,username,amount,method,address,status,created_at) "
                                    "VALUES (?,?,?,'auto',?,'pending',?)",
                                    (_u['id'], _u['username'], _amt, _addr or '', _now4)
                                )
                            except Exception:
                                pass

                    if _eligible:
                        _db3.commit()
                        _plog.info('Payout run: %d eligible, %d sent on-chain', len(_eligible), _sent)
            except Exception as _ex2:
                _plog.warning('Payout scheduler error: %s', _ex2)

    _sched_t.Thread(target=_run_payout_scheduler, args=(app,), daemon=True, name='payout-scheduler').start()

    # ── Security (CSRF + rate limiting) ──────────────────────────────────────
    init_security(app)

    # ── Auto-initialise database on startup ───────────────────────────────────
    # Runs init_db() once when the app starts — safe to call every time
    # because every statement uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.
    # This means `gunicorn "app:create_app()"` works on a fresh database
    # with no manual setup step required.
    with app.app_context():
        try:
            init_db()
        except Exception as _e:
            import logging as _log
            _log.getLogger(__name__).warning(
                'init_db() failed at startup: %s', _e
            )

    # ── Service worker at root scope ─────────────────────────────────────────
    @app.route('/sw.js')
    def service_worker():
        from flask import send_from_directory, make_response
        resp = make_response(send_from_directory('static', 'sw.js'))
        resp.headers['Content-Type'] = 'application/javascript'
        resp.headers['Service-Worker-Allowed'] = '/'
        resp.headers['Cache-Control'] = 'no-cache'
        return resp

    # ── Health check (for Render zero-downtime deploys) ──────────────────────
    @app.route('/health')
    def health():
        from db import _global_synced
        return jsonify({'ok': True, 'db_synced': bool(_global_synced)}), 200

    # ── Security headers ─────────────────────────────────────────────────────
    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options']  = 'nosniff'
        response.headers['X-Frame-Options']          = 'DENY'
        response.headers['Referrer-Policy']          = 'strict-origin-when-cross-origin'
        response.headers.setdefault(
            'Content-Security-Policy',
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https: blob:; "
            "connect-src 'self'; media-src 'self' https: blob:;"
        )
        return response

    # ── Storage health check ─────────────────────────────────────────────────
    @app.route('/api/admin/storage-check')
    def storage_check():
        from flask import jsonify as _j, session as _s
        import storage as _st
        db = get_db()
        user = db.execute('SELECT is_admin FROM users WHERE id=?', (_s.get('user_id', 0),)).fetchone()
        if not user or not user['is_admin']:
            return _j({'error': 'Forbidden'}), 403
        return _j(_st.check_connection())

    # ── Error handlers ────────────────────────────────────────────────────────
    # JSON-returning paths — any route the JS calls with fetch()
    _JSON_PREFIXES = (
        '/api/', '/post', '/user/', '/auth/',
        '/messages/', '/group/', '/channel/',
        '/profile/', '/account/', '/settings/',
        '/bookmarks', '/wallet', '/boost/',
    )

    def _wants_json():
        p = request.path
        # All mutating requests are AJAX and expect JSON
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return True
        for prefix in _JSON_PREFIXES:
            if p.startswith(prefix):
                return True
        if request.headers.get('X-Requested-With') == 'fetch':
            return True
        return False

    @app.errorhandler(404)
    def _not_found(_e):
        if _wants_json():
            return jsonify({'success': False, 'error': 'Not found'}), 404
        return render_template('error.html', code=404, message='Page not found.'), 404

    @app.errorhandler(429)
    def _rate_limited(_e):
        """Flask-Limiter returns 429 — always JSON so the toast shows the real message."""
        return jsonify({
            'success': False,
            'error': 'Too many posts. Please wait a moment and try again.'
        }), 429

    @app.errorhandler(500)
    def _server_error(_e):
        if _wants_json():
            return jsonify({'success': False, 'error': 'Server error — please try again.'}), 500
        return render_template('error.html', code=500,
                               message='Something went wrong on our end.'), 500

    return app



def init_db():
    """
    Initialize global.db on every startup:
      1. Run CREATE TABLE IF NOT EXISTS for all tables (new tables only)
      2. Run ALTER TABLE ADD COLUMN migrations (new columns on existing tables)
      3. Upsert the admin account from environment variables

    This is fully idempotent — safe to run on every deploy.
    """
    import sqlite3 as _sqlite
    import secrets as _sec
    import logging as _log
    from helpers import hash_password

    _logger = _log.getLogger(__name__)

    db_path = os.path.join(_base_dir, 'global.db')

    # If the local DB file is malformed (from a previous crashed run), wipe it
    # so executescript can start clean. run_schema_migrations will add columns.
    if os.path.exists(db_path):
        try:
            _tmp = _sqlite.connect(db_path)
            _row = _tmp.execute('PRAGMA integrity_check').fetchone()
            _tmp.close()
            if _row is None or _row[0] != 'ok':
                import logging as _log2
                _log2.getLogger(__name__).critical(
                    'init_db: local global.db is malformed — removing before re-init.'
                )
                os.remove(db_path)
        except Exception:
            try:
                os.remove(db_path)
            except OSError:
                pass

    conn    = _sqlite.connect(db_path)
    conn.row_factory = _sqlite.Row

    # Step 1: Create any missing tables
    from db import GLOBAL_SCHEMA, run_schema_migrations
    conn.executescript(GLOBAL_SCHEMA)
    conn.commit()

    # Step 2: Add any missing columns to existing tables
    run_schema_migrations(conn)

    # Step 3: Read admin credentials from environment variables
    # These are re-read on EVERY startup — change them in Render env vars
    # and redeploy; the admin row will be updated automatically.
    import os as _os
    admin_username = (_os.environ.get('ADMIN_USERNAME') or '').strip()
    admin_email    = (_os.environ.get('ADMIN_EMAIL')    or '').strip()
    admin_password = (_os.environ.get('ADMIN_PASSWORD') or '').strip()

    # Validate that all three are set
    missing = [k for k, v in [
        ('ADMIN_USERNAME', admin_username),
        ('ADMIN_EMAIL',    admin_email),
        ('ADMIN_PASSWORD', admin_password),
    ] if not v]
    if missing:
        _logger.critical(
            'Missing admin environment variables: %s  '
            '— Set them in your Render dashboard under Environment. '
            'The admin account will use fallback values until they are set.',
            ', '.join(missing)
        )
        # Fallback values only if env vars truly missing
        admin_username = admin_username or 'admin'
        admin_email    = admin_email    or 'admin@duysboost.com'
        admin_password = admin_password or 'changeme_set_ADMIN_PASSWORD_now'

    hashed_pw = hash_password(admin_password)

    # Step 4: Upsert admin row
    # Always UPDATE if a row exists (so env var changes take effect immediately)
    existing = conn.execute(
        'SELECT id FROM users WHERE is_admin=1 LIMIT 1'
    ).fetchone()

    if existing:
        conn.execute(
            'UPDATE users SET username=?, email=?, password=?, is_admin=1 WHERE id=?',
            (admin_username, admin_email, hashed_pw, existing['id'])
        )
        _logger.info(
            'Admin credentials updated from env vars: username=%s email=%s',
            admin_username, admin_email
        )
    else:
        # No admin exists yet — INSERT
        conn.execute(
            'INSERT INTO users '
            '(username, email, password, is_admin, balance, referral_code) '
            'VALUES (?, ?, ?, 1, 0, ?)',
            (admin_username, admin_email, hashed_pw, admin_username)
        )
        _logger.info(
            'Admin account created: username=%s email=%s',
            admin_username, admin_email
        )

    conn.commit()
    conn.close()
    print('✅ Global SQLite database initialised.')


if __name__ == '__main__':
    try:
        init_db()
    except Exception as _e:
        import logging as _ilog
        _ilog.getLogger(__name__).critical('init_db() failed at startup: %s', _e)

    app   = create_app()
    port  = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(host='0.0.0.0', port=port, debug=debug)
