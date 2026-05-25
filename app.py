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
        DATABASE_URL=os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL', ''),
        R2_ACCESS_KEY_ID=os.environ.get('R2_ACCESS_KEY_ID', ''),
        R2_SECRET_ACCESS_KEY=os.environ.get('R2_SECRET_ACCESS_KEY', ''),
        R2_BUCKET_NAME=os.environ.get('R2_BUCKET_NAME', ''),
        R2_ACCOUNT_ID=os.environ.get('R2_ACCOUNT_ID', ''),
        R2_PUBLIC_URL=os.environ.get('R2_PUBLIC_URL', ''),
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
        return {
            'current_user':  get_current_user(),
            'CURRENCY_SYMBOL': CURRENCY_SYMBOL,
            'CURRENCY_CODE':   CURRENCY_CODE,
            'CRYPTO_NETWORKS': CRYPTO_NETWORKS,
            'CRYPTO_WALLETS':  CRYPTO_WALLETS,
            'CRYPTO_ENABLED':  any(CRYPTO_WALLETS.values()),
        }

    # ── Blueprints ────────────────────────────────────────────────────────────
    from blueprints.auth   import bp as auth_bp
    from blueprints.social import bp as social_bp
    from blueprints.boost  import bp as boost_bp
    from blueprints.wallet import bp as wallet_bp
    from blueprints.admin  import bp as admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(social_bp)
    app.register_blueprint(boost_bp)
    app.register_blueprint(wallet_bp)
    app.register_blueprint(admin_bp)

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
                'init_db() failed at startup (DATABASE_URL may not be set yet): %s', _e
            )

    # ── Storage health check ─────────────────────────────────────────────────
    @app.route('/api/admin/storage-check')
    def storage_check():
        from flask import jsonify as _j, session as _s
        import storage as _st
        db = get_db()
        user = db.execute('SELECT is_admin FROM users WHERE id=%s', (_s.get('user_id', 0),)).fetchone()
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
    Create the PostgreSQL schema and run safe column/index migrations.

    Called automatically by create_app() on every startup — uses
    CREATE TABLE IF NOT EXISTS and ALTER TABLE ... ADD COLUMN IF NOT EXISTS
    so it is fully idempotent and safe to run on every boot.

    Can also be run directly:
        python -c "from app import init_db; init_db()"
    """
    import psycopg2
    from helpers import hash_password

    dsn = (
        os.environ.get('DATABASE_URL') or
        os.environ.get('POSTGRES_URL', '')
    )
    if dsn.startswith('postgres://'):
        dsn = 'postgresql://' + dsn[len('postgres://'):]
    if not dsn:
        raise RuntimeError('DATABASE_URL is not set.')

    db = psycopg2.connect(dsn)
    cur = db.cursor()

    # ── Full schema ───────────────────────────────────────────────────────────
    cur.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT,
        balance NUMERIC(14,6) DEFAULT 0,
        referral_code TEXT UNIQUE,
        referred_by INTEGER REFERENCES users(id),
        is_admin INTEGER DEFAULT 0,
        theme TEXT DEFAULT 'dark',
        crypto_network TEXT,
        crypto_address TEXT,
        crypto_name TEXT,
        referral_bonus_awarded INTEGER DEFAULT 0,
        bio TEXT,
        avatar_url TEXT,
        banner_url TEXT,
        display_name TEXT,
        website TEXT,
        location TEXT,
        is_verified INTEGER DEFAULT 0,
        follower_count INTEGER DEFAULT 0,
        following_count INTEGER DEFAULT 0,
        post_count INTEGER DEFAULT 0,
        total_tips_received NUMERIC(14,6) DEFAULT 0,
        total_tips_sent NUMERIC(14,6) DEFAULT 0,
        subscriber_count INTEGER DEFAULT 0,
        search_count INTEGER DEFAULT 0,
        unread_dm_count INTEGER DEFAULT 0,
        unread_group_count INTEGER DEFAULT 0,
        online_at TEXT,
        show_online INTEGER DEFAULT 1,
        allow_post_saves INTEGER DEFAULT 1,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS ads (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT NOT NULL,
        platform TEXT NOT NULL,
        target_url TEXT NOT NULL,
        task_type TEXT NOT NULL,
        reward_per_task NUMERIC(14,6) DEFAULT 0.05,
        budget NUMERIC(14,6) NOT NULL,
        budget_spent NUMERIC(14,6) DEFAULT 0,
        followers_target INTEGER DEFAULT 0,
        followers_gained INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS task_completions (
        id SERIAL PRIMARY KEY,
        ad_id INTEGER NOT NULL REFERENCES ads(id),
        worker_id INTEGER NOT NULL REFERENCES users(id),
        proof_link TEXT NOT NULL,
        status TEXT DEFAULT 'approved',
        reward NUMERIC(14,6),
        submitted_at TIMESTAMPTZ DEFAULT NOW(),
        reviewed_at TIMESTAMPTZ
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS transactions (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        type TEXT,
        amount NUMERIC(14,6),
        description TEXT,
        status TEXT DEFAULT 'completed',
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS notifications (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        message TEXT,
        read INTEGER DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS withdrawals (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        amount NUMERIC(14,6),
        method TEXT,
        account TEXT,
        network TEXT,
        status TEXT DEFAULT 'pending',
        tx_hash TEXT,
        failure_reason TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        processed_at TIMESTAMPTZ
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS crypto_deposits (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        network TEXT NOT NULL,
        tx_hash TEXT UNIQUE NOT NULL,
        amount NUMERIC(14,6) NOT NULL,
        status TEXT DEFAULT 'pending',
        confirmed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS posts (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        body TEXT,
        image_url TEXT,
        reply_to_id INTEGER REFERENCES posts(id),
        repost_of_id INTEGER REFERENCES posts(id),
        quote_body TEXT,
        like_count INTEGER DEFAULT 0,
        reply_count INTEGER DEFAULT 0,
        repost_count INTEGER DEFAULT 0,
        is_boosted INTEGER DEFAULT 0,
        boost_ad_id INTEGER,
        hashtags_cached TEXT,
        media_url TEXT,        -- R2 CDN URL (replaces base64 media_data)
        view_count INTEGER DEFAULT 0,
        score NUMERIC(14,6) DEFAULT 0,
        is_subscriber_only INTEGER DEFAULT 0,
        edited_at TIMESTAMPTZ,
        post_type TEXT DEFAULT 'post',
        poll_expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS follows (
        follower_id INTEGER NOT NULL REFERENCES users(id),
        following_id INTEGER NOT NULL REFERENCES users(id),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY(follower_id, following_id)
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS post_likes (
        user_id INTEGER NOT NULL REFERENCES users(id),
        post_id INTEGER NOT NULL REFERENCES posts(id),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY(user_id, post_id)
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS bookmarks (
        user_id INTEGER NOT NULL REFERENCES users(id),
        post_id INTEGER NOT NULL REFERENCES posts(id),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY(user_id, post_id)
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS post_boosts (
        id SERIAL PRIMARY KEY,
        post_id INTEGER NOT NULL REFERENCES posts(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        budget NUMERIC(14,6) NOT NULL,
        budget_spent NUMERIC(14,6) DEFAULT 0,
        reward_per_engage NUMERIC(14,6) DEFAULT 0.05,
        engage_type TEXT DEFAULT 'like',
        target_count INTEGER DEFAULT 0,
        engaged_count INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS boost_engagements (
        id SERIAL PRIMARY KEY,
        boost_id INTEGER NOT NULL REFERENCES post_boosts(id),
        post_id INTEGER NOT NULL REFERENCES posts(id),
        worker_id INTEGER NOT NULL REFERENCES users(id),
        proof_link TEXT,
        reward NUMERIC(14,6),
        earned_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('CREATE TABLE IF NOT EXISTS hashtags (id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL)')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS post_hashtags (
        post_id INTEGER NOT NULL REFERENCES posts(id),
        hashtag_id INTEGER NOT NULL REFERENCES hashtags(id),
        PRIMARY KEY(post_id, hashtag_id)
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS tips (
        id SERIAL PRIMARY KEY,
        from_user_id INTEGER NOT NULL REFERENCES users(id),
        to_user_id INTEGER NOT NULL REFERENCES users(id),
        post_id INTEGER REFERENCES posts(id),
        amount NUMERIC(14,6) NOT NULL,
        message TEXT,
        tx_hash TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS subscription_tiers (
        id SERIAL PRIMARY KEY,
        creator_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
        price_usd NUMERIC(14,6) NOT NULL DEFAULT 1.00,
        title TEXT NOT NULL DEFAULT 'Supporter',
        description TEXT,
        perks TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS subscriptions (
        id SERIAL PRIMARY KEY,
        subscriber_id INTEGER NOT NULL REFERENCES users(id),
        creator_id INTEGER NOT NULL REFERENCES users(id),
        tier_id INTEGER NOT NULL REFERENCES subscription_tiers(id),
        status TEXT DEFAULT 'active',
        started_at TIMESTAMPTZ DEFAULT NOW(),
        expires_at TIMESTAMPTZ,
        UNIQUE(subscriber_id, creator_id)
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS search_history (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        query TEXT NOT NULL,
        result_type TEXT DEFAULT 'mixed',
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS post_views (
        id SERIAL PRIMARY KEY,
        post_id INTEGER NOT NULL REFERENCES posts(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(post_id, user_id)
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS channels (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        slug TEXT NOT NULL UNIQUE,
        description TEXT,
        avatar_url TEXT,
        owner_id INTEGER NOT NULL REFERENCES users(id),
        is_public INTEGER DEFAULT 1,
        member_count INTEGER DEFAULT 0,
        post_count INTEGER DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS channel_members (
        channel_id INTEGER NOT NULL REFERENCES channels(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        role TEXT DEFAULT 'member',
        joined_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY(channel_id, user_id)
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS channel_posts (
        id SERIAL PRIMARY KEY,
        channel_id INTEGER NOT NULL REFERENCES channels(id),
        post_id INTEGER NOT NULL REFERENCES posts(id),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(channel_id, post_id)
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS groups (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        description TEXT,
        avatar_url TEXT,
        owner_id INTEGER NOT NULL REFERENCES users(id),
        is_public INTEGER DEFAULT 1,
        member_count INTEGER DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS group_members (
        group_id INTEGER NOT NULL REFERENCES groups(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        role TEXT DEFAULT 'member',
        joined_at TIMESTAMPTZ DEFAULT NOW(),
        last_read_at TIMESTAMPTZ,
        PRIMARY KEY(group_id, user_id)
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS group_messages (
        id SERIAL PRIMARY KEY,
        group_id INTEGER NOT NULL REFERENCES groups(id),
        sender_id INTEGER NOT NULL REFERENCES users(id),
        body TEXT,
        msg_type TEXT DEFAULT 'text',
        file_url TEXT,        -- R2 CDN URL (replaces base64 file_data)
        file_name TEXT,
        file_mime TEXT,
        reply_to_id INTEGER,
        deleted_at TIMESTAMPTZ,
        edited_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS poll_options (
        id SERIAL PRIMARY KEY,
        post_id INTEGER NOT NULL REFERENCES posts(id),
        label TEXT NOT NULL,
        votes INTEGER DEFAULT 0
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS poll_votes (
        id SERIAL PRIMARY KEY,
        post_id INTEGER NOT NULL REFERENCES posts(id),
        option_id INTEGER NOT NULL REFERENCES poll_options(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        voted_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(post_id, user_id)
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS conversations (
        id SERIAL PRIMARY KEY,
        user_a INTEGER NOT NULL REFERENCES users(id),
        user_b INTEGER NOT NULL REFERENCES users(id),
        last_msg_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(user_a, user_b)
    )''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
        sender_id INTEGER NOT NULL REFERENCES users(id),
        body TEXT,
        msg_type TEXT DEFAULT 'text',
        file_url TEXT,        -- R2 CDN URL (replaces base64 file_data)
        file_name TEXT,
        file_mime TEXT,
        is_read INTEGER DEFAULT 0,
        edited_at TIMESTAMPTZ,
        reply_to_id INTEGER,
        reactions TEXT,
        is_pinned INTEGER DEFAULT 0,
        deleted_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )''')

    # ── Safe column migrations (ADD COLUMN IF NOT EXISTS) ────────────────────
    # Covers databases created before new columns were added.
    # PostgreSQL 9.6+ supports ADD COLUMN IF NOT EXISTS natively.
    column_migrations = [
        # table,            column,                  definition
        ('users',  'bio',                    'TEXT'),
        ('users',  'avatar_url',             'TEXT'),
        ('users',  'banner_url',             'TEXT'),
        ('users',  'display_name',           'TEXT'),
        ('users',  'website',                'TEXT'),
        ('users',  'location',               'TEXT'),
        ('users',  'is_verified',            'INTEGER DEFAULT 0'),
        ('users',  'follower_count',         'INTEGER DEFAULT 0'),
        ('users',  'following_count',        'INTEGER DEFAULT 0'),
        ('users',  'post_count',             'INTEGER DEFAULT 0'),
        ('users',  'total_tips_received',    'NUMERIC(14,6) DEFAULT 0'),
        ('users',  'total_tips_sent',        'NUMERIC(14,6) DEFAULT 0'),
        ('users',  'subscriber_count',       'INTEGER DEFAULT 0'),
        ('users',  'search_count',           'INTEGER DEFAULT 0'),
        ('users',  'unread_dm_count',        'INTEGER DEFAULT 0'),
        ('users',  'unread_group_count',     'INTEGER DEFAULT 0'),
        ('users',  'online_at',              'TIMESTAMPTZ'),
        ('users',  'show_online',            'INTEGER DEFAULT 1'),
        ('users',  'allow_post_saves',       'INTEGER DEFAULT 1'),
        ('users',  'crypto_network',         'TEXT'),
        ('users',  'crypto_address',         'TEXT'),
        ('users',  'crypto_name',            'TEXT'),
        ('users',  'referral_bonus_awarded', 'INTEGER DEFAULT 0'),
        ('posts',  'hashtags_cached',        'TEXT'),
        ('posts',  'media_url',              'TEXT'),
        ('posts',  'view_count',             'INTEGER DEFAULT 0'),
        ('posts',  'score',                  'NUMERIC(14,6) DEFAULT 0'),
        ('posts',  'is_subscriber_only',     'INTEGER DEFAULT 0'),
        ('posts',  'edited_at',              'TIMESTAMPTZ'),
        ('posts',  'post_type',              "TEXT DEFAULT 'post'"),
        ('posts',  'poll_expires_at',        'TIMESTAMPTZ'),
        ('withdrawals', 'tx_hash',           'TEXT'),
        ('withdrawals', 'network',           'TEXT'),
        ('withdrawals', 'failure_reason',    'TEXT'),
        ('withdrawals', 'processed_at',      'TIMESTAMPTZ'),
        ('messages', 'edited_at',            'TIMESTAMPTZ'),
        ('messages', 'reply_to_id',          'INTEGER'),
        ('messages', 'reactions',            'TEXT'),
        ('messages', 'is_pinned',            'INTEGER DEFAULT 0'),
        ('messages', 'deleted_at',           'TIMESTAMPTZ'),
        ('messages', 'msg_type',             "TEXT DEFAULT 'text'"),
        ('messages', 'file_url',             'TEXT'),
        ('messages', 'file_name',            'TEXT'),
        ('messages', 'file_mime',            'TEXT'),
        ('group_messages', 'file_url',       'TEXT'),
        ('group_members',  'last_read_at',   'TIMESTAMPTZ'),
    ]
    for tbl, col, defn in column_migrations:
        cur.execute(
            f'ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {defn}'
        )

    # ── Indexes (IF NOT EXISTS supported in PG 9.5+) ──────────────────────────
    indexes = [
        ('ads',              'idx_ads_user',         'user_id'),
        ('ads',              'idx_ads_status',        'status'),
        ('task_completions', 'idx_tc_worker',         'worker_id'),
        ('task_completions', 'idx_tc_ad',             'ad_id'),
        ('transactions',     'idx_tx_user',           'user_id'),
        ('notifications',    'idx_notif_user',        'user_id'),
        ('withdrawals',      'idx_wdr_status',        'status'),
        ('crypto_deposits',  'idx_cdep_user',         'user_id'),
        ('posts',            'idx_posts_user',        'user_id'),
        ('posts',            'idx_posts_created',     'created_at'),
        ('posts',            'idx_posts_reply',       'reply_to_id'),
        ('posts',            'idx_posts_score',       'score'),
        ('follows',          'idx_follows_follower',  'follower_id'),
        ('follows',          'idx_follows_following', 'following_id'),
        ('post_likes',       'idx_likes_post',        'post_id'),
        ('post_likes',       'idx_likes_user',        'user_id'),
        ('bookmarks',        'idx_bm_user',           'user_id'),
        ('post_boosts',      'idx_pb_post',           'post_id'),
        ('post_boosts',      'idx_pb_status',         'status'),
        ('boost_engagements','idx_be_boost',          'boost_id'),
        ('boost_engagements','idx_be_worker',         'worker_id'),
        ('post_hashtags',    'idx_ph_post',           'post_id'),
        ('post_hashtags',    'idx_ph_hashtag',        'hashtag_id'),
        ('tips',             'idx_tips_to',           'to_user_id'),
        ('tips',             'idx_tips_from',         'from_user_id'),
        ('subscriptions',    'idx_sub_creator',       'creator_id'),
        ('subscriptions',    'idx_sub_subscriber',    'subscriber_id'),
        ('search_history',   'idx_sh_user',           'user_id'),
        ('search_history',   'idx_sh_query',          'query'),
        ('post_views',       'idx_pv_post',           'post_id'),
        ('conversations',    'idx_conv_a',            'user_a'),
        ('conversations',    'idx_conv_b',            'user_b'),
        ('conversations',    'idx_conv_last',         'last_msg_at'),
        ('messages',         'idx_msg_conv',          'conversation_id'),
        ('messages',         'idx_msg_sender',        'sender_id'),
        ('channels',         'idx_ch_owner',          'owner_id'),
        ('channel_members',  'idx_chm_channel',       'channel_id'),
        ('channel_members',  'idx_chm_user',          'user_id'),
        ('channel_posts',    'idx_chp_channel',       'channel_id'),
        ('poll_options',     'idx_po_post',           'post_id'),
        ('poll_votes',       'idx_poll_votes_post',   'post_id'),
        ('groups',           'idx_grp_owner',         'owner_id'),
        ('group_members',    'idx_grpm_group',        'group_id'),
        ('group_members',    'idx_grpm_user',         'user_id'),
        ('group_messages',   'idx_grpms_group',       'group_id'),
        ('group_messages',   'idx_grpms_sender',      'sender_id'),
    ]
    for table, name, cols in indexes:
        cur.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {table}({cols})')

    # ── Default admin user ────────────────────────────────────────────────────
    cur.execute('SELECT id FROM users WHERE username = %s', ('admin',))
    if not cur.fetchone():
        import secrets as _s
        cur.execute(
            'INSERT INTO users (username, email, password, is_admin, balance, referral_code) '
            'VALUES (%s, %s, %s, 1, 1000.0, %s)',
            ('admin', 'admin@duysboost.com', hash_password('admin123'), _s.token_hex(5))
        )

    db.commit()
    cur.close()
    db.close()
    print('PostgreSQL schema initialised.')


if __name__ == '__main__':
    # create_app() calls init_db() automatically — no manual step needed.
    app   = create_app()
    port  = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    app.run(host='0.0.0.0', port=port, debug=debug)
