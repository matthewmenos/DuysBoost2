"""
DUYS Boost — Social Media Boost Platform
App factory replacing the monolithic app.py.

Run locally:
    python app.py

With gunicorn (multi-worker safe once you switch to PostgreSQL):
    gunicorn "app:create_app()" -w 4 -b 0.0.0.0:5000
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

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    if os.environ.get('OAUTHLIB_INSECURE_TRANSPORT', '0') == '1':
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

    app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
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
        ctx  = {
            'current_user':  user,
            'CURRENCY_SYMBOL': CURRENCY_SYMBOL,
            'CURRENCY_CODE':   CURRENCY_CODE,
            'CRYPTO_NETWORKS': CRYPTO_NETWORKS,
            'CRYPTO_WALLETS':  CRYPTO_WALLETS,
            'CRYPTO_ENABLED':  any(CRYPTO_WALLETS.values()),
            'GOOGLE_ENABLED':  bool(os.environ.get('GOOGLE_CLIENT_ID')),
            'open_report_count':  0,
            'pending_wdr_count':  0,
        }
        # For admin pages, inject badge counts
        if user and user['is_admin']:
            try:
                db = get_db()
                ctx['open_report_count'] = db.execute(
                    "SELECT COUNT(*) FROM reports WHERE status='open'"
                ).fetchone()[0]
                ctx['pending_wdr_count'] = db.execute(
                    "SELECT COUNT(*) FROM withdrawals WHERE status='pending'"
                ).fetchone()[0]
            except Exception:
                pass
        return ctx

    # ── Blueprints ────────────────────────────────────────────────────────────
    from blueprints.auth    import bp as auth_bp
    from blueprints.social  import bp as social_bp
    from blueprints.boost   import bp as boost_bp
    from blueprints.wallet  import bp as wallet_bp
    from blueprints.admin   import bp as admin_bp
    from blueprints.stories import bp as stories_bp
    from sse                import bp as sse_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(social_bp)
    app.register_blueprint(boost_bp)
    app.register_blueprint(wallet_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(stories_bp)
    app.register_blueprint(sse_bp)   # SSE streams

    # Start background story cleanup thread
    from blueprints.stories import start_cleanup_thread
    start_cleanup_thread(app)

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
                'init_db() failed at startup (DATABASE_URL may not be set yet): ?', _e
            )

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

    return app



def init_db():
    """
    Initialize the global SQLite database (global.db).
    This holds shared/public data: user directory, ads, trending indexes.
    Individual per-user .db files are created on first login via db.open_user_db().
    Called automatically by create_app() on startup.
    """
    import sqlite3 as _sqlite
    import secrets as _sec
    from helpers import hash_password

    db_path = os.path.join(_base_dir, 'global.db')
    conn = _sqlite.connect(db_path)
    conn.row_factory = _sqlite.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous  = NORMAL')

    # Initialize full schema in global.db (same schema as user DBs)
    from db import init_user_tables
    init_user_tables(conn)

    # Seed default admin if not present
    admin = conn.execute('SELECT id FROM users WHERE username=?', ('admin',)).fetchone()
    if not admin:
        conn.execute(
            'INSERT INTO users (username,email,password,is_admin,balance,referral_code) '
            'VALUES (?,?,?,1,1000.0,?)',
            ('admin', 'admin@duysboost.com', hash_password('admin123'), _sec.token_hex(5))
        )
    conn.commit()
    conn.close()
    print('✅ Global SQLite database initialised.')


if __name__ == '__main__':
    # create_app() calls init_db() automatically — no manual step needed.
    app   = create_app()
    port  = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    app.run(host='0.0.0.0', port=port, debug=debug)
