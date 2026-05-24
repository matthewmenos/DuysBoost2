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
# In-memory typing state: {(user_id, recipient_username): timestamp}
_typing_state: dict = {}

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
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,   # 20 MB max upload
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

    -- ── Social layer ───────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        body TEXT,
        image_url TEXT,
        reply_to_id INTEGER,
        repost_of_id INTEGER,
        quote_body TEXT,
        like_count INTEGER DEFAULT 0,
        reply_count INTEGER DEFAULT 0,
        repost_count INTEGER DEFAULT 0,
        is_boosted INTEGER DEFAULT 0,
        boost_ad_id INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(reply_to_id) REFERENCES posts(id),
        FOREIGN KEY(repost_of_id) REFERENCES posts(id)
    );
    CREATE TABLE IF NOT EXISTS follows (
        follower_id INTEGER NOT NULL,
        following_id INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY(follower_id, following_id),
        FOREIGN KEY(follower_id) REFERENCES users(id),
        FOREIGN KEY(following_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS post_likes (
        user_id INTEGER NOT NULL,
        post_id INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY(user_id, post_id),
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(post_id) REFERENCES posts(id)
    );
    CREATE TABLE IF NOT EXISTS bookmarks (
        user_id INTEGER NOT NULL,
        post_id INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY(user_id, post_id),
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(post_id) REFERENCES posts(id)
    );

    -- ── Phase 2: Boost-in-feed ──────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS post_boosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        budget REAL NOT NULL,
        budget_spent REAL DEFAULT 0,
        reward_per_engage REAL DEFAULT 0.05,
        engage_type TEXT DEFAULT 'like',
        target_count INTEGER DEFAULT 0,
        engaged_count INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(post_id) REFERENCES posts(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS boost_engagements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        boost_id INTEGER NOT NULL,
        post_id INTEGER NOT NULL,
        worker_id INTEGER NOT NULL,
        proof_link TEXT,
        reward REAL,
        earned_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(boost_id) REFERENCES post_boosts(id),
        FOREIGN KEY(post_id) REFERENCES posts(id),
        FOREIGN KEY(worker_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS hashtags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    );
    CREATE TABLE IF NOT EXISTS post_hashtags (
        post_id INTEGER NOT NULL,
        hashtag_id INTEGER NOT NULL,
        PRIMARY KEY(post_id, hashtag_id),
        FOREIGN KEY(post_id) REFERENCES posts(id),
        FOREIGN KEY(hashtag_id) REFERENCES hashtags(id)
    );

    -- ── Phase 3: Creator monetisation ─────────────────────────────────
    CREATE TABLE IF NOT EXISTS tips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user_id INTEGER NOT NULL,
        to_user_id   INTEGER NOT NULL,
        post_id      INTEGER,
        amount       REAL NOT NULL,
        message      TEXT,
        tx_hash      TEXT,
        created_at   TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(from_user_id) REFERENCES users(id),
        FOREIGN KEY(to_user_id)   REFERENCES users(id),
        FOREIGN KEY(post_id)      REFERENCES posts(id)
    );
    CREATE TABLE IF NOT EXISTS subscription_tiers (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_id  INTEGER NOT NULL UNIQUE,
        price_usd   REAL NOT NULL DEFAULT 1.00,
        title       TEXT NOT NULL DEFAULT 'Supporter',
        description TEXT,
        perks       TEXT,
        is_active   INTEGER DEFAULT 1,
        created_at  TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(creator_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS subscriptions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        subscriber_id INTEGER NOT NULL,
        creator_id    INTEGER NOT NULL,
        tier_id       INTEGER NOT NULL,
        status        TEXT DEFAULT 'active',
        started_at    TEXT DEFAULT (datetime('now')),
        expires_at    TEXT,
        UNIQUE(subscriber_id, creator_id),
        FOREIGN KEY(subscriber_id) REFERENCES users(id),
        FOREIGN KEY(creator_id)    REFERENCES users(id),
        FOREIGN KEY(tier_id)       REFERENCES subscription_tiers(id)
    );

    -- ── Phase 4: Discovery & trending ─────────────────────────────────
    CREATE TABLE IF NOT EXISTS search_history (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        query      TEXT NOT NULL,
        result_type TEXT DEFAULT 'mixed',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS post_views (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id    INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(post_id, user_id),
        FOREIGN KEY(post_id) REFERENCES posts(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    -- ── Channels ───────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS channels (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL UNIQUE,
        slug        TEXT NOT NULL UNIQUE,
        description TEXT,
        avatar_url  TEXT,
        owner_id    INTEGER NOT NULL,
        is_public   INTEGER DEFAULT 1,
        member_count INTEGER DEFAULT 0,
        post_count  INTEGER DEFAULT 0,
        created_at  TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(owner_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS channel_members (
        channel_id  INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        role        TEXT DEFAULT 'member',
        joined_at   TEXT DEFAULT (datetime('now')),
        PRIMARY KEY(channel_id, user_id),
        FOREIGN KEY(channel_id) REFERENCES channels(id),
        FOREIGN KEY(user_id)    REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS channel_posts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL,
        post_id    INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(channel_id, post_id),
        FOREIGN KEY(channel_id) REFERENCES channels(id),
        FOREIGN KEY(post_id)    REFERENCES posts(id)
    );

    -- ── Polls ──────────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS poll_options (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL,
        label   TEXT NOT NULL,
        votes   INTEGER DEFAULT 0,
        FOREIGN KEY(post_id) REFERENCES posts(id)
    );
    CREATE TABLE IF NOT EXISTS poll_votes (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id   INTEGER NOT NULL,
        option_id INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        voted_at  TEXT DEFAULT (datetime('now')),
        UNIQUE(post_id, user_id),
        FOREIGN KEY(post_id)   REFERENCES posts(id),
        FOREIGN KEY(option_id) REFERENCES poll_options(id),
        FOREIGN KEY(user_id)   REFERENCES users(id)
    );

    -- ── Direct Messages ────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS conversations (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_a       INTEGER NOT NULL,
        user_b       INTEGER NOT NULL,
        last_msg_at  TEXT DEFAULT (datetime('now')),
        UNIQUE(user_a, user_b),
        FOREIGN KEY(user_a) REFERENCES users(id),
        FOREIGN KEY(user_b) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS messages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL,
        sender_id       INTEGER NOT NULL,
        body            TEXT,
        msg_type        TEXT DEFAULT 'text',
        file_data       TEXT,
        file_name       TEXT,
        file_mime       TEXT,
        is_read         INTEGER DEFAULT 0,
        created_at      TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(conversation_id) REFERENCES conversations(id),
        FOREIGN KEY(sender_id)       REFERENCES users(id)
    );
    ''')

    # ── Migrations for databases created by earlier versions ─────────────
    def _table_exists(table):
        return bool(db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone())

    def _add_col_if_missing(table, col, decl):
        if not _table_exists(table):
            return
        cols = {r[1] for r in db.execute(f'PRAGMA table_info({table})').fetchall()}
        if col not in cols:
            try:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {col} {decl}')
            except Exception:
                pass

    def _create_index_if_missing(table, index_name, cols):
        if not _table_exists(table):
            return
        cols_set = {r[1] for r in db.execute(f'PRAGMA table_info({table})').fetchall()}
        if not set(c.strip() for c in cols.split(',')) <= cols_set:
            return
        existing = {r[1] for r in db.execute(f'PRAGMA index_list({table})').fetchall()}
        if index_name not in existing:
            try:
                db.execute(f'CREATE INDEX {index_name} ON {table}({cols})')
            except Exception:
                pass

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

    # Social layer migrations
    _add_col_if_missing('users', 'bio', 'TEXT')
    _add_col_if_missing('users', 'avatar_url', 'TEXT')
    _add_col_if_missing('users', 'banner_url', 'TEXT')
    _add_col_if_missing('users', 'display_name', 'TEXT')
    _add_col_if_missing('users', 'website', 'TEXT')
    _add_col_if_missing('users', 'location', 'TEXT')
    _add_col_if_missing('users', 'is_verified', 'INTEGER DEFAULT 0')
    _add_col_if_missing('users', 'follower_count', 'INTEGER DEFAULT 0')
    _add_col_if_missing('users', 'following_count', 'INTEGER DEFAULT 0')
    _add_col_if_missing('users', 'post_count', 'INTEGER DEFAULT 0')

    _create_index_if_missing('posts', 'idx_posts_user', 'user_id')
    _create_index_if_missing('posts', 'idx_posts_created', 'created_at')
    _create_index_if_missing('posts', 'idx_posts_reply', 'reply_to_id')
    _create_index_if_missing('follows', 'idx_follows_follower', 'follower_id')
    _create_index_if_missing('follows', 'idx_follows_following', 'following_id')
    _create_index_if_missing('post_likes', 'idx_likes_post', 'post_id')
    _create_index_if_missing('post_likes', 'idx_likes_user', 'user_id')
    _create_index_if_missing('bookmarks', 'idx_bm_user', 'user_id')

    # Phase 2 migrations
    _create_index_if_missing('post_boosts', 'idx_pb_post',   'post_id')
    _create_index_if_missing('post_boosts', 'idx_pb_status', 'status')
    _create_index_if_missing('boost_engagements', 'idx_be_boost',  'boost_id')
    _create_index_if_missing('boost_engagements', 'idx_be_worker', 'worker_id')
    _create_index_if_missing('post_hashtags', 'idx_ph_post',    'post_id')
    _create_index_if_missing('post_hashtags', 'idx_ph_hashtag', 'hashtag_id')
    _add_col_if_missing('posts', 'hashtags_cached', 'TEXT')
    _add_col_if_missing('posts', 'media_data',     'TEXT')
    _add_col_if_missing('posts', 'media_mime',     'TEXT')

    # Phase 3 migrations
    _add_col_if_missing('users', 'total_tips_received', 'REAL DEFAULT 0')
    _add_col_if_missing('users', 'total_tips_sent',     'REAL DEFAULT 0')
    _add_col_if_missing('users', 'subscriber_count',    'INTEGER DEFAULT 0')
    _add_col_if_missing('posts', 'is_subscriber_only',  'INTEGER DEFAULT 0')
    _create_index_if_missing('tips', 'idx_tips_to',   'to_user_id')
    _create_index_if_missing('tips', 'idx_tips_from', 'from_user_id')
    _create_index_if_missing('subscriptions', 'idx_sub_creator',    'creator_id')
    _create_index_if_missing('subscriptions', 'idx_sub_subscriber', 'subscriber_id')

    # Phase 4 migrations
    _add_col_if_missing('posts', 'view_count',   'INTEGER DEFAULT 0')
    _add_col_if_missing('posts', 'score',        'REAL DEFAULT 0')
    _add_col_if_missing('users', 'search_count', 'INTEGER DEFAULT 0')
    _create_index_if_missing('search_history', 'idx_sh_user',  'user_id')
    _create_index_if_missing('search_history', 'idx_sh_query', 'query')
    _create_index_if_missing('post_views',     'idx_pv_post',  'post_id')
    _create_index_if_missing('posts',          'idx_posts_score', 'score')

    # DM migrations
    _add_col_if_missing('users', 'unread_dm_count', 'INTEGER DEFAULT 0')
    _add_col_if_missing('users', 'online_at',       'TEXT')
    _add_col_if_missing('users', 'show_online',     'INTEGER DEFAULT 1')
    _add_col_if_missing('messages', 'msg_type',   'TEXT DEFAULT "text"')
    _add_col_if_missing('messages', 'file_data',  'TEXT')
    _add_col_if_missing('messages', 'file_name',  'TEXT')
    _add_col_if_missing('messages', 'file_mime',  'TEXT')
    _add_col_if_missing('messages', 'edited_at',  'TEXT')
    _add_col_if_missing('messages', 'reply_to_id','INTEGER')
    _add_col_if_missing('messages', 'reactions',  'TEXT')
    _add_col_if_missing('messages', 'is_pinned',  'INTEGER DEFAULT 0')
    _add_col_if_missing('messages', 'deleted_at', 'TEXT')
    _add_col_if_missing('posts',    'edited_at',  'TEXT')
    _create_index_if_missing('conversations', 'idx_conv_a',    'user_a')
    _create_index_if_missing('conversations', 'idx_conv_b',    'user_b')
    _create_index_if_missing('conversations', 'idx_conv_last', 'last_msg_at')
    _create_index_if_missing('messages', 'idx_msg_conv',   'conversation_id')
    _create_index_if_missing('messages', 'idx_msg_sender', 'sender_id')

    # Channels + Polls + Settings migrations
    _add_col_if_missing('posts', 'post_type',       'TEXT DEFAULT "post"')
    _add_col_if_missing('posts', 'poll_expires_at', 'TEXT')
    _add_col_if_missing('users', 'allow_post_saves','INTEGER DEFAULT 1')
    _create_index_if_missing('channels',       'idx_ch_owner',    'owner_id')
    _create_index_if_missing('channel_members','idx_chm_channel', 'channel_id')
    _create_index_if_missing('channel_members','idx_chm_user',    'user_id')
    _create_index_if_missing('channel_posts',  'idx_chp_channel', 'channel_id')
    _create_index_if_missing('poll_options',   'idx_po_post',     'post_id')
    _create_index_if_missing('poll_votes',     'idx_poll_votes_post',  'post_id')

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



# ── Jinja filters ──────────────────────────────────────────────────────────
import markupsafe

@app.template_filter('nl2br')
def nl2br_filter(value):
    """Convert newlines to <br> tags, safely escaping HTML."""
    if value is None:
        return ''
    escaped = markupsafe.escape(value)
    return markupsafe.Markup(str(escaped).replace('\n', '<br>'))


@app.template_filter('linkify_tags')
def linkify_tags_filter(value):
    """Convert #hashtag and @mention in post body to links, safely."""
    import re as _re
    if value is None:
        return ''
    escaped = str(markupsafe.escape(value))
    # hashtags → /tag/<name>
    escaped = _re.sub(
        r'#(\w+)',
        lambda m: f'<a href="/tag/{m.group(1).lower()}" class="post-tag">#{m.group(1)}</a>',
        escaped
    )
    # @mentions → /user/<username>
    escaped = _re.sub(
        r'@(\w+)',
        lambda m: f'<a href="/user/{m.group(1)}" class="post-mention">@{m.group(1)}</a>',
        escaped
    )
    # newlines
    escaped = escaped.replace('\n', '<br>')
    return markupsafe.Markup(escaped)

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
        return redirect(url_for('feed'))
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
        return jsonify({'success': True, 'redirect': url_for('feed')})
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
        return jsonify({'success': True, 'redirect': url_for('feed')})
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
    return redirect(url_for('feed'))


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
# Social — helpers
# ─────────────────────────────────────────────────────────────────────────────

def _format_post(row, current_uid, db):
    """Convert a DB row to a plain dict enriched with viewer-specific flags."""
    if row is None:
        return None
    p = dict(row)
    p['liked']      = bool(db.execute('SELECT 1 FROM post_likes WHERE user_id=? AND post_id=?',
                                       (current_uid, p['id'])).fetchone())
    p['bookmarked'] = bool(db.execute('SELECT 1 FROM bookmarks WHERE user_id=? AND post_id=?',
                                       (current_uid, p['id'])).fetchone())
    author = db.execute('SELECT id,username,display_name,avatar_url,is_verified FROM users WHERE id=?',
                        (p['user_id'],)).fetchone()
    p['author'] = dict(author) if author else {}

    # Nested original for reposts
    if p.get('repost_of_id'):
        orig_row = db.execute('SELECT * FROM posts WHERE id=?', (p['repost_of_id'],)).fetchone()
        p['repost_of'] = _format_post(orig_row, current_uid, db) if orig_row else None
    else:
        p['repost_of'] = None

    # Parent post stub for replies
    if p.get('reply_to_id'):
        parent = db.execute(
            'SELECT p.id, u.username FROM posts p JOIN users u ON p.user_id=u.id WHERE p.id=?',
            (p['reply_to_id'],)
        ).fetchone()
        p['reply_to_username'] = parent['username'] if parent else None
    else:
        p['reply_to_username'] = None

    # Active boost info for earn-while-scrolling
    boost = db.execute(
        """SELECT pb.* FROM post_boosts pb
           WHERE pb.post_id=? AND pb.status='active'
             AND pb.budget_spent < pb.budget
             AND pb.user_id != ?
             AND NOT EXISTS (
               SELECT 1 FROM boost_engagements be
               WHERE be.boost_id=pb.id AND be.worker_id=?
             )
           ORDER BY pb.created_at DESC LIMIT 1""",
        (p['id'], current_uid, current_uid)
    ).fetchone()
    p['active_boost'] = dict(boost) if boost else None

    # Subscriber-only lock: viewer has no active subscription to this author?
    # Ensure media fields come through
    if 'media_data' not in p:
        p['media_data'] = None
    if 'media_mime' not in p:
        p['media_mime'] = None

    if p.get('is_subscriber_only') and p['user_id'] != current_uid:
        is_subscribed = bool(db.execute(
            "SELECT 1 FROM subscriptions WHERE subscriber_id=? AND creator_id=? AND status='active'",
            (current_uid, p['user_id'])
        ).fetchone())
        # Check admin
        viewer = db.execute('SELECT is_admin FROM users WHERE id=?', (current_uid,)).fetchone()
        is_admin = viewer and viewer['is_admin']
        p['locked'] = not (is_subscribed or is_admin)
    else:
        p['locked'] = False

    return p


def _update_counts(db, user_id):
    """Sync follower/following/post counts for a user from live data."""
    db.execute("""UPDATE users SET
        follower_count  = (SELECT COUNT(*) FROM follows WHERE following_id=?),
        following_count = (SELECT COUNT(*) FROM follows WHERE follower_id=?),
        post_count      = (SELECT COUNT(*) FROM posts WHERE user_id=? AND reply_to_id IS NULL)
        WHERE id=?""", (user_id, user_id, user_id, user_id))


def _recalc_post_score(db, post_id):
    """
    Hacker-News-style score: (likes*2 + replies + reposts*1.5 + views*0.1 + boost*20)
    decayed by hours since posting.  Stored on the row for cheap ORDER BY.
    """
    import math
    row = db.execute(
        'SELECT like_count,reply_count,repost_count,view_count,is_boosted,created_at '
        'FROM posts WHERE id=?', (post_id,)
    ).fetchone()
    if not row:
        return
    try:
        from datetime import datetime, timezone
        posted = datetime.fromisoformat(row['created_at'].replace('Z',''))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        age_h = max(0.1, (datetime.now(timezone.utc) - posted).total_seconds() / 3600)
    except Exception:
        age_h = 1.0
    gravity = 1.8
    interactions = (
        float(row['like_count'] or 0) * 2 +
        float(row['reply_count'] or 0) * 1.5 +
        float(row['repost_count'] or 0) * 1.5 +
        float(row['view_count'] or 0) * 0.05 +
        (20.0 if row['is_boosted'] else 0)
    )
    score = interactions / math.pow(age_h + 2, gravity)
    db.execute('UPDATE posts SET score=? WHERE id=?', (round(score, 6), post_id))


def _get_personalized_post_ids(db, uid, limit=20, offset=0):
    """
    Personalised For-You feed:
    1. Posts from people the viewer follows (weight ×1.5)
    2. Posts liked/boosted by followed accounts
    3. Top-scored public posts the viewer hasn't seen
    Deduped, ordered by weighted score.
    """
    # IDs the viewer has already interacted with
    seen = {r[0] for r in db.execute(
        'SELECT post_id FROM post_views WHERE user_id=?', (uid,)
    ).fetchall()}
    liked = {r[0] for r in db.execute(
        'SELECT post_id FROM post_likes WHERE user_id=?', (uid,)
    ).fetchall()}
    exclude = seen | liked

    following_ids = [r[0] for r in db.execute(
        'SELECT following_id FROM follows WHERE follower_id=?', (uid,)
    ).fetchall()]

    results = {}  # post_id -> weighted_score

    # Tier 1: posts from followed accounts
    if following_ids:
        ph = ','.join('?' * len(following_ids))
        rows = db.execute(
            f'SELECT id, score FROM posts '
            f'WHERE user_id IN ({ph}) AND reply_to_id IS NULL '
            f'ORDER BY score DESC LIMIT 60',
            following_ids
        ).fetchall()
        for r in rows:
            if r['id'] not in exclude:
                results[r['id']] = float(r['score'] or 0) * 1.6

    # Tier 2: posts liked by people the viewer follows
    if following_ids:
        ph = ','.join('?' * len(following_ids))
        rows = db.execute(
            f'SELECT DISTINCT p.id, p.score FROM posts p '
            f'JOIN post_likes l ON l.post_id=p.id '
            f'WHERE l.user_id IN ({ph}) AND p.user_id != ? '
            f'AND p.reply_to_id IS NULL ORDER BY p.score DESC LIMIT 40',
            following_ids + [uid]
        ).fetchall()
        for r in rows:
            if r['id'] not in exclude:
                existing = results.get(r['id'], 0)
                results[r['id']] = max(existing, float(r['score'] or 0) * 1.2)

    # Tier 3: high-score public posts not yet seen (fill remainder)
    need = max(0, limit + offset - len(results))
    if need > 0:
        known = list(results.keys()) + list(exclude) + [0]
        ph = ','.join('?' * len(known))
        rows = db.execute(
            f'SELECT id, score FROM posts '
            f'WHERE id NOT IN ({ph}) AND reply_to_id IS NULL AND user_id != ? '
            f'ORDER BY score DESC LIMIT ?',
            known + [uid, need + 20]
        ).fetchall()
        for r in rows:
            results[r['id']] = float(r['score'] or 0)

    # Sort by weighted score, paginate
    ranked = sorted(results.items(), key=lambda x: -x[1])
    page_ids = [pid for pid, _ in ranked[offset: offset + limit]]
    return page_ids


# ─────────────────────────────────────────────────────────────────────────────
# Social — Feed (Home)
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/feed')
@login_required
def feed():
    db   = get_db()
    uid  = session['user_id']
    tab  = request.args.get('tab', 'for_you')   # 'for_you' | 'following'
    page = safe_int(request.args.get('page'), 1)
    per  = 20
    off  = (page - 1) * per

    if tab == 'following':
        rows = db.execute("""
            SELECT p.* FROM posts p
            WHERE p.reply_to_id IS NULL
              AND p.user_id IN (SELECT following_id FROM follows WHERE follower_id=?)
            ORDER BY p.created_at DESC LIMIT ? OFFSET ?
        """, (uid, per, off)).fetchall()
    elif tab == 'earn':
        # Only posts with an active boost the viewer can still earn from
        rows = db.execute("""
            SELECT DISTINCT p.* FROM posts p
            JOIN post_boosts pb ON pb.post_id = p.id
            WHERE pb.status='active'
              AND pb.budget_spent < pb.budget
              AND pb.user_id != ?
              AND NOT EXISTS (
                SELECT 1 FROM boost_engagements be
                WHERE be.boost_id=pb.id AND be.worker_id=?
              )
            ORDER BY pb.reward_per_engage DESC, p.created_at DESC LIMIT ? OFFSET ?
        """, (uid, uid, per, off)).fetchall()
    else:
        # For-you: personalised scored algorithm
        ranked_ids = _get_personalized_post_ids(db, uid, limit=per, offset=off)
        if ranked_ids:
            ph   = ','.join('?' * len(ranked_ids))
            rows = db.execute(
                f'SELECT * FROM posts WHERE id IN ({ph})', ranked_ids
            ).fetchall()
            # re-order to match ranked_ids order
            row_map = {r['id']: r for r in rows}
            rows    = [row_map[pid] for pid in ranked_ids if pid in row_map]
        else:
            # fallback for empty graph (new user / no follows)
            rows = db.execute("""
                SELECT * FROM posts
                WHERE reply_to_id IS NULL
                ORDER BY score DESC, created_at DESC LIMIT ? OFFSET ?
            """, (per, off)).fetchall()

    posts    = [_format_post_with_poll(r, uid, db) for r in rows]
    has_more = len(rows) == per

    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify({'posts': posts, 'has_more': has_more})

    # Suggested users to follow (not already following, not self)
    suggestions = db.execute("""
        SELECT id, username, display_name, avatar_url, is_verified, follower_count
        FROM users
        WHERE id != ?
          AND id NOT IN (SELECT following_id FROM follows WHERE follower_id=?)
        ORDER BY follower_count DESC, id DESC
        LIMIT 5
    """, (uid, uid)).fetchall()
    suggestions = [dict(s) for s in suggestions]

    # Trending — most-liked posts in last 48h
    trending = db.execute("""
        SELECT p.*, u.username, u.display_name, u.avatar_url, u.is_verified
        FROM posts p JOIN users u ON p.user_id=u.id
        WHERE p.reply_to_id IS NULL
          AND p.created_at >= datetime('now', '-48 hours')
        ORDER BY p.like_count DESC LIMIT 5
    """).fetchall()
    trending = [dict(t) for t in trending]

    return render_template('feed.html', posts=posts, tab=tab,
                           page=page, has_more=has_more,
                           suggestions=suggestions, trending=trending)


# ─────────────────────────────────────────────────────────────────────────────
# Social — Create / delete post
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/post', methods=['POST'])
@login_required
def create_post():
    db      = get_db()
    uid     = session['user_id']
    body    = (request.form.get('body') or '').strip()
    reply_to = safe_int(request.form.get('reply_to_id'), 0) or None
    repost_of = safe_int(request.form.get('repost_of_id'), 0) or None
    quote_body = (request.form.get('quote_body') or '').strip() or None
    subscriber_only = 1 if request.form.get('subscriber_only') else 0
    media_data = (request.form.get('media_data') or '').strip() or None  # base64 data-URI
    media_mime = (request.form.get('media_mime') or '').strip() or None
    post_type  = (request.form.get('post_type') or 'post').strip().lower()
    channel_id = safe_int(request.form.get('channel_id'), 0) or None

    # Poll options (JSON list of strings)
    import json as _json_create
    poll_options_raw = request.form.get('poll_options') or '[]'
    try:
        poll_options = [str(o).strip() for o in _json_create.loads(poll_options_raw) if str(o).strip()][:6]
    except Exception:
        poll_options = []
    if post_type == 'poll' and len(poll_options) < 2:
        return jsonify({'success': False, 'error': 'A poll needs at least 2 options.'}), 400
    poll_expires_at = None
    if post_type == 'poll':
        from datetime import timedelta
        poll_expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    if not body and not repost_of and not media_data and post_type != 'poll':
        return jsonify({'success': False, 'error': 'Post cannot be empty.'}), 400
    if body and len(body) > 500:
        return jsonify({'success': False, 'error': 'Max 500 characters.'}), 400

    now = datetime.now(timezone.utc).isoformat()

    db.execute("""
        INSERT INTO posts (user_id, body, reply_to_id, repost_of_id, quote_body,
                           is_subscriber_only, media_data, media_mime,
                           post_type, poll_expires_at, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (uid, body or None, reply_to, repost_of, quote_body,
            subscriber_only, media_data, media_mime,
            post_type, poll_expires_at, now))
    post_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

    # Insert poll options
    for opt_label in poll_options:
        db.execute('INSERT INTO poll_options (post_id, label) VALUES (?,?)', (post_id, opt_label))

    # Link to channel if provided
    if channel_id:
        ch = db.execute('SELECT id FROM channels WHERE id=?', (channel_id,)).fetchone()
        if ch:
            db.execute('INSERT OR IGNORE INTO channel_posts (channel_id, post_id) VALUES (?,?)',
                       (channel_id, post_id))
            db.execute('UPDATE channels SET post_count=post_count+1 WHERE id=?', (channel_id,))

    # Update counts
    if reply_to:
        db.execute('UPDATE posts SET reply_count=reply_count+1 WHERE id=?', (reply_to,))
        # Notify parent author
        parent = db.execute('SELECT user_id FROM posts WHERE id=?', (reply_to,)).fetchone()
        if parent and parent['user_id'] != uid:
            me = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
            add_notification(db, parent['user_id'],
                f'💬 @{me["username"]} replied to your post.')
    if repost_of:
        db.execute('UPDATE posts SET repost_count=repost_count+1 WHERE id=?', (repost_of,))
        parent = db.execute('SELECT user_id FROM posts WHERE id=?', (repost_of,)).fetchone()
        if parent and parent['user_id'] != uid:
            me = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
            add_notification(db, parent['user_id'],
                f'🔁 @{me["username"]} reposted your post.')

    # Extract and store hashtags
    import re as _re
    tags = list(set(t.lower() for t in _re.findall(r'#(\w+)', body)))
    for tag in tags[:10]:  # cap at 10 per post
        db.execute('INSERT OR IGNORE INTO hashtags (name) VALUES (?)', (tag,))
        ht = db.execute('SELECT id FROM hashtags WHERE name=?', (tag,)).fetchone()
        if ht:
            db.execute('INSERT OR IGNORE INTO post_hashtags (post_id,hashtag_id) VALUES (?,?)',
                       (post_id, ht['id']))
    if tags:
        db.execute('UPDATE posts SET hashtags_cached=? WHERE id=?',
                   (' '.join('#' + t for t in tags), post_id))

    # ── @mention notifications ──────────────────────────────────────────────
    if body:
        mentioned = list(set(_re.findall(r'@(\w+)', body)))
        me_row = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
        me_name = me_row['username'] if me_row else ''
        for username in mentioned[:10]:
            if username.lower() == me_name.lower():
                continue
            target = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
            if target and target['id'] != uid:
                add_notification(db, target['id'],
                    f'🔔 @{me_name} mentioned you in a post.')

    _update_counts(db, uid)
    _recalc_post_score(db, post_id)
    db.commit()

    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    return jsonify({'success': True, 'post': _format_post(post, uid, db)})


@app.route('/post/<int:post_id>/delete', methods=['POST'])
@login_required
def delete_post(post_id):
    db  = get_db()
    uid = session['user_id']
    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    user = db.execute('SELECT is_admin FROM users WHERE id=?', (uid,)).fetchone()
    if post['user_id'] != uid and not user['is_admin']:
        return jsonify({'success': False, 'error': 'Forbidden'}), 403

    # Decrement parent counts
    if post['reply_to_id']:
        db.execute('UPDATE posts SET reply_count=MAX(0,reply_count-1) WHERE id=?', (post['reply_to_id'],))
    if post['repost_of_id']:
        db.execute('UPDATE posts SET repost_count=MAX(0,repost_count-1) WHERE id=?', (post['repost_of_id'],))

    db.execute('DELETE FROM post_likes WHERE post_id=?', (post_id,))
    db.execute('DELETE FROM bookmarks  WHERE post_id=?', (post_id,))
    db.execute('DELETE FROM posts      WHERE id=?', (post_id,))
    _update_counts(db, uid)
    db.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────────────────────────────────────────
# Social — Single post + replies
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/post/<int:post_id>')
@login_required
def post_detail(post_id):
    db  = get_db()
    uid = session['user_id']
    row = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not row:
        return render_template('error.html', code=404, message='Post not found.'), 404
    post = _format_post(row, uid, db)

    replies = db.execute("""
        SELECT * FROM posts WHERE reply_to_id=? ORDER BY created_at ASC
    """, (post_id,)).fetchall()
    replies = [_format_post(r, uid, db) for r in replies]

    return render_template('post_detail.html', post=post, replies=replies)


# ─────────────────────────────────────────────────────────────────────────────
# Social — Like / unlike
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/post/<int:post_id>/like', methods=['POST'])
@login_required
def toggle_like(post_id):
    db  = get_db()
    uid = session['user_id']
    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Not found'}), 404

    existing = db.execute('SELECT 1 FROM post_likes WHERE user_id=? AND post_id=?',
                          (uid, post_id)).fetchone()
    if existing:
        db.execute('DELETE FROM post_likes WHERE user_id=? AND post_id=?', (uid, post_id))
        db.execute('UPDATE posts SET like_count=MAX(0,like_count-1) WHERE id=?', (post_id,))
        liked = False
    else:
        db.execute('INSERT OR IGNORE INTO post_likes (user_id,post_id) VALUES (?,?)',
                   (uid, post_id))
        db.execute('UPDATE posts SET like_count=like_count+1 WHERE id=?', (post_id,))
        liked = True
        if post['user_id'] != uid:
            me = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
            add_notification(db, post['user_id'],
                f'❤️ @{me["username"]} liked your post.')

    new_count = db.execute('SELECT like_count FROM posts WHERE id=?', (post_id,)).fetchone()['like_count']
    _recalc_post_score(db, post_id)
    db.commit()
    return jsonify({'success': True, 'liked': liked, 'like_count': new_count})


# ─────────────────────────────────────────────────────────────────────────────
# Social — Bookmark
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/post/<int:post_id>/bookmark', methods=['POST'])
@login_required
def toggle_bookmark(post_id):
    db  = get_db()
    uid = session['user_id']
    existing = db.execute('SELECT 1 FROM bookmarks WHERE user_id=? AND post_id=?',
                          (uid, post_id)).fetchone()
    if existing:
        db.execute('DELETE FROM bookmarks WHERE user_id=? AND post_id=?', (uid, post_id))
        saved = False
    else:
        db.execute('INSERT OR IGNORE INTO bookmarks (user_id,post_id) VALUES (?,?)',
                   (uid, post_id))
        saved = True
    db.commit()
    return jsonify({'success': True, 'saved': saved})


@app.route('/bookmarks')
@login_required
def bookmarks():
    db  = get_db()
    uid = session['user_id']
    rows = db.execute("""
        SELECT p.* FROM posts p
        JOIN bookmarks b ON b.post_id=p.id
        WHERE b.user_id=?
        ORDER BY b.created_at DESC
    """, (uid,)).fetchall()
    posts = [_format_post(r, uid, db) for r in rows]
    return render_template('bookmarks.html', posts=posts)


# ─────────────────────────────────────────────────────────────────────────────
# Social — Follow / unfollow
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/user/<username>/follow', methods=['POST'])
@login_required
def toggle_follow(username):
    db  = get_db()
    uid = session['user_id']
    target = db.execute('SELECT id,username FROM users WHERE username=?', (username,)).fetchone()
    if not target or target['id'] == uid:
        return jsonify({'success': False, 'error': 'Not found'}), 404

    existing = db.execute('SELECT 1 FROM follows WHERE follower_id=? AND following_id=?',
                          (uid, target['id'])).fetchone()
    if existing:
        db.execute('DELETE FROM follows WHERE follower_id=? AND following_id=?',
                   (uid, target['id']))
        following = False
    else:
        db.execute('INSERT OR IGNORE INTO follows (follower_id,following_id) VALUES (?,?)',
                   (uid, target['id']))
        following = True
        me = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
        add_notification(db, target['id'],
            f'👤 @{me["username"]} started following you.')

    _update_counts(db, uid)
    _update_counts(db, target['id'])
    db.commit()

    new_followers = db.execute('SELECT follower_count FROM users WHERE id=?',
                               (target['id'],)).fetchone()['follower_count']
    return jsonify({'success': True, 'following': following, 'follower_count': new_followers})


# ─────────────────────────────────────────────────────────────────────────────
# Social — Profile
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/user/<username>')
@login_required
def profile(username):
    db  = get_db()
    uid = session['user_id']
    target = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not target:
        return render_template('error.html', code=404, message='User not found.'), 404

    tab = request.args.get('tab', 'posts')   # posts | replies | likes | media

    is_following = bool(db.execute('SELECT 1 FROM follows WHERE follower_id=? AND following_id=?',
                                   (uid, target['id'])).fetchone())
    is_own = (uid == target['id'])

    if tab == 'replies':
        rows = db.execute("""
            SELECT * FROM posts WHERE user_id=? AND reply_to_id IS NOT NULL
            ORDER BY created_at DESC LIMIT 40
        """, (target['id'],)).fetchall()
    elif tab == 'likes':
        rows = db.execute("""
            SELECT p.* FROM posts p
            JOIN post_likes l ON l.post_id=p.id
            WHERE l.user_id=?
            ORDER BY l.created_at DESC LIMIT 40
        """, (target['id'],)).fetchall()
    else:  # posts
        rows = db.execute("""
            SELECT * FROM posts WHERE user_id=? AND reply_to_id IS NULL
            ORDER BY created_at DESC LIMIT 40
        """, (target['id'],)).fetchall()

    posts = [_format_post(r, uid, db) for r in rows]

    followers = db.execute("""
        SELECT u.id, u.username, u.display_name, u.avatar_url, u.is_verified
        FROM follows f JOIN users u ON u.id=f.follower_id
        WHERE f.following_id=? LIMIT 6
    """, (target['id'],)).fetchall()
    followers = [dict(f) for f in followers]

    # Creator monetisation data
    tier = db.execute(
        "SELECT * FROM subscription_tiers WHERE creator_id=? AND is_active=1",
        (target['id'],)
    ).fetchone()

    is_subscribed = False
    if not is_own and tier:
        is_subscribed = bool(db.execute(
            "SELECT 1 FROM subscriptions WHERE subscriber_id=? AND creator_id=? AND status='active'",
            (uid, target['id'])
        ).fetchone())

    # Top tippers for profile sidebar
    top_tips = db.execute("""
        SELECT t.amount, u.username, u.avatar_url, u.display_name
        FROM tips t JOIN users u ON u.id=t.from_user_id
        WHERE t.to_user_id=?
        ORDER BY t.amount DESC LIMIT 5
    """, (target['id'],)).fetchall()

    return render_template('profile.html', target=dict(target),
                           posts=posts, tab=tab,
                           is_following=is_following, is_own=is_own,
                           followers=followers,
                           tier=dict(tier) if tier else None,
                           is_subscribed=is_subscribed,
                           top_tips=[dict(t) for t in top_tips])


@app.route('/profile/edit', methods=['GET', 'POST'])
@login_required
def edit_profile():
    db  = get_db()
    uid = session['user_id']
    if request.method == 'POST':
        display_name = (request.form.get('display_name') or '').strip()[:60]
        bio          = (request.form.get('bio')          or '').strip()[:160]
        website      = (request.form.get('website')      or '').strip()[:120]
        location     = (request.form.get('location')     or '').strip()[:60]
        allow_saves  = 1 if request.form.get('allow_post_saves', '1') != '0' else 0

        db.execute("""UPDATE users SET
            display_name=?, bio=?, website=?, location=?, allow_post_saves=?
            WHERE id=?""",
            (display_name or None, bio or None, website or None,
             location or None, allow_saves, uid))
        db.commit()
        me = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
        return jsonify({'success': True, 'redirect': url_for('profile', username=me['username'])})

    user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    return render_template('edit_profile.html', user=dict(user))


# ─────────────────────────────────────────────────────────────────────────────
# Social — Explore / Search
# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# Social — Follow lists
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/user/<username>/followers')
@login_required
def follower_list(username):
    db  = get_db()
    uid = session['user_id']
    target = db.execute('SELECT id,username,display_name FROM users WHERE username=?', (username,)).fetchone()
    if not target:
        return render_template('error.html', code=404, message='User not found.'), 404
    rows = db.execute("""
        SELECT u.id, u.username, u.display_name, u.avatar_url, u.is_verified,
               u.follower_count, u.bio,
               EXISTS(SELECT 1 FROM follows WHERE follower_id=? AND following_id=u.id) AS you_follow
        FROM follows f JOIN users u ON u.id=f.follower_id
        WHERE f.following_id=? ORDER BY f.created_at DESC LIMIT 100
    """, (uid, target['id'])).fetchall()
    return render_template('follow_list.html', target=dict(target),
                           users=[dict(r) for r in rows], list_type='Followers')


@app.route('/user/<username>/following')
@login_required
def following_list(username):
    db  = get_db()
    uid = session['user_id']
    target = db.execute('SELECT id,username,display_name FROM users WHERE username=?', (username,)).fetchone()
    if not target:
        return render_template('error.html', code=404, message='User not found.'), 404
    rows = db.execute("""
        SELECT u.id, u.username, u.display_name, u.avatar_url, u.is_verified,
               u.follower_count, u.bio,
               EXISTS(SELECT 1 FROM follows WHERE follower_id=? AND following_id=u.id) AS you_follow
        FROM follows f JOIN users u ON u.id=f.following_id
        WHERE f.follower_id=? ORDER BY f.created_at DESC LIMIT 100
    """, (uid, target['id'])).fetchall()
    return render_template('follow_list.html', target=dict(target),
                           users=[dict(r) for r in rows], list_type='Following')


# ─────────────────────────────────────────────────────────────────────────────
# Social — redirect / logged-in root now goes to feed
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Boost a post (native post-level boosting)
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/post/<int:post_id>/boost', methods=['POST'])
@login_required
def boost_post(post_id):
    """Create a post_boost — spend wallet balance to surface a post in feeds."""
    db  = get_db()
    uid = session['user_id']

    post = db.execute('SELECT * FROM posts WHERE id=? AND user_id=?', (post_id, uid)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Post not found or not yours.'}), 404

    engage_type   = (request.form.get('engage_type') or 'like').strip().lower()
    target_count  = safe_int(request.form.get('target_count'), 0)
    reward_each   = safe_float(request.form.get('reward_each'), WORKER_REWARD_PER_TASK)

    if engage_type not in ('like', 'follow', 'comment', 'share'):
        return jsonify({'success': False, 'error': 'Invalid engagement type.'}), 400
    if target_count < 1:
        return jsonify({'success': False, 'error': 'Target must be at least 1.'}), 400
    if reward_each < 0.01:
        return jsonify({'success': False, 'error': 'Reward must be at least $0.01.'}), 400

    budget = round(target_count * reward_each * (1 / WORKER_REWARD_PER_TASK) * LISTER_COST_PER_TASK, 2)
    # simplified: budget = target * lister cost
    budget = round(target_count * LISTER_COST_PER_TASK, 2)

    user = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()
    if budget > user['balance']:
        return jsonify({'success': False,
                        'error': f'Insufficient balance. Need ${budget:.2f}, have ${user["balance"]:.2f}.'}), 400

    db.execute('UPDATE users SET balance=balance-? WHERE id=?', (budget, uid))
    db.execute("""
        INSERT INTO post_boosts (post_id, user_id, budget, reward_per_engage,
                                 engage_type, target_count, status)
        VALUES (?,?,?,?,?,?,'active')
    """, (post_id, uid, budget, WORKER_REWARD_PER_TASK, engage_type, target_count))
    boost_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

    db.execute('UPDATE posts SET is_boosted=1 WHERE id=?', (post_id,))
    add_transaction(db, uid, 'spend', budget,
                    f'Boost post #{post_id} — {target_count}x {engage_type}')
    add_notification(db, uid,
        f'📣 Your post is now boosted! ${budget:.2f} budget, {target_count} target engagements.')
    db.commit()

    return jsonify({'success': True, 'boost_id': boost_id, 'budget': budget})


@app.route('/post/<int:post_id>/boost/cancel', methods=['POST'])
@login_required
def cancel_boost(post_id):
    """Cancel an active boost and refund unspent budget."""
    db  = get_db()
    uid = session['user_id']

    boost = db.execute(
        "SELECT * FROM post_boosts WHERE post_id=? AND user_id=? AND status='active'",
        (post_id, uid)
    ).fetchone()
    if not boost:
        return jsonify({'success': False, 'error': 'No active boost found.'}), 404

    refund = round(float(boost['budget']) - float(boost['budget_spent']), 6)
    db.execute("UPDATE post_boosts SET status='cancelled' WHERE id=?", (boost['id'],))
    if refund > 0:
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (refund, uid))
        add_transaction(db, uid, 'deposit', refund, f'Boost refund for post #{post_id}')
        add_notification(db, uid, f'↩️ Boost cancelled. ${refund:.2f} refunded to your wallet.')

    # Only un-boost if no other active boosts on this post
    other = db.execute(
        "SELECT id FROM post_boosts WHERE post_id=? AND status='active' AND id!=?",
        (post_id, boost['id'])
    ).fetchone()
    if not other:
        db.execute('UPDATE posts SET is_boosted=0 WHERE id=?', (post_id,))
    db.commit()

    return jsonify({'success': True, 'refund': refund})


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Earn by engaging with boosted posts
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/post/<int:post_id>/earn', methods=['POST'])
@login_required
def earn_engagement(post_id):
    """
    Worker clicks Earn on a boosted post.
    Validates they haven't already earned from this boost,
    credits their wallet, debits the boost budget.
    """
    db  = get_db()
    uid = session['user_id']

    # Find an active boost on this post that the worker hasn't completed
    boost = db.execute("""
        SELECT pb.* FROM post_boosts pb
        WHERE pb.post_id=? AND pb.status='active'
          AND pb.budget_spent < pb.budget
          AND pb.user_id != ?
          AND NOT EXISTS (
            SELECT 1 FROM boost_engagements be
            WHERE be.boost_id=pb.id AND be.worker_id=?
          )
        ORDER BY pb.created_at DESC LIMIT 1
    """, (post_id, uid, uid)).fetchone()

    if not boost:
        return jsonify({'success': False,
                        'error': 'No earnable boost available on this post.'}), 400

    reward = float(boost['reward_per_engage'])

    # Record engagement
    db.execute("""
        INSERT INTO boost_engagements (boost_id, post_id, worker_id, reward, earned_at)
        VALUES (?,?,?,?,datetime('now'))
    """, (boost['id'], post_id, uid, reward))

    # Update boost budget and counts
    db.execute("""
        UPDATE post_boosts
        SET budget_spent  = budget_spent + ?,
            engaged_count = engaged_count + 1,
            status = CASE
              WHEN budget_spent + ? >= budget THEN 'completed'
              WHEN engaged_count + 1 >= target_count THEN 'completed'
              ELSE status
            END
        WHERE id=?
    """, (reward, reward, boost['id']))

    # Credit worker
    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (reward, uid))
    add_transaction(db, uid, 'earn', reward,
                    f'Earned from boosted post #{post_id} ({boost["engage_type"]})')

    # Notify post owner if boost just completed
    updated_boost = db.execute('SELECT * FROM post_boosts WHERE id=?', (boost['id'],)).fetchone()
    if updated_boost and updated_boost['status'] == 'completed':
        db.execute('UPDATE posts SET is_boosted=0 WHERE id=?', (post_id,))
        add_notification(db, boost['user_id'],
            f'🎉 Your boost on post #{post_id} completed! '
            f'{updated_boost["engaged_count"]} engagements reached.')

    check_and_award_referral_bonus(db, uid)
    db.commit()

    new_balance = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()['balance']
    return jsonify({
        'success': True,
        'reward': reward,
        'balance': new_balance,
        'message': f'+${reward:.2f} earned!'
    })


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Hashtag feeds
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/tag/<tag>')
@login_required
def hashtag_feed(tag):
    db  = get_db()
    uid = session['user_id']
    tag = tag.lower().lstrip('#')

    ht = db.execute('SELECT * FROM hashtags WHERE name=?', (tag,)).fetchone()
    if not ht:
        posts = []
    else:
        rows = db.execute("""
            SELECT p.* FROM posts p
            JOIN post_hashtags ph ON ph.post_id=p.id
            WHERE ph.hashtag_id=? AND p.reply_to_id IS NULL
            ORDER BY p.created_at DESC LIMIT 40
        """, (ht['id'],)).fetchall()
        posts = [_format_post(r, uid, db) for r in rows]

    # Trending hashtags sidebar
    trending_tags = db.execute("""
        SELECT h.name, COUNT(ph.post_id) as cnt
        FROM hashtags h JOIN post_hashtags ph ON ph.hashtag_id=h.id
        JOIN posts p ON p.id=ph.post_id
        WHERE p.created_at >= datetime('now', '-7 days')
        GROUP BY h.id ORDER BY cnt DESC LIMIT 10
    """).fetchall()

    return render_template('hashtag_feed.html', tag=tag, posts=posts,
                           trending_tags=[dict(t) for t in trending_tags])


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — My boosts dashboard
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/boosts')
@login_required
def my_boosts():
    db  = get_db()
    uid = session['user_id']

    boosts = db.execute("""
        SELECT pb.*, p.body, p.like_count, p.reply_count
        FROM post_boosts pb JOIN posts p ON p.id=pb.post_id
        WHERE pb.user_id=?
        ORDER BY pb.created_at DESC LIMIT 50
    """, (uid,)).fetchall()

    total_spent  = sum(float(b['budget_spent']) for b in boosts)
    total_budget = sum(float(b['budget'])       for b in boosts)
    total_engaged = sum(int(b['engaged_count']) for b in boosts)

    # Earned from others' boosts
    earned = db.execute("""
        SELECT COALESCE(SUM(be.reward),0) as total
        FROM boost_engagements be WHERE be.worker_id=?
    """, (uid,)).fetchone()['total']

    return render_template('my_boosts.html',
                           boosts=[dict(b) for b in boosts],
                           total_spent=total_spent,
                           total_budget=total_budget,
                           total_engaged=total_engaged,
                           earned_from_boosts=float(earned))


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Trending hashtags API (for explore sidebar)
# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Earn feed API (paginated JSON for infinite scroll)
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/earn/posts')
@login_required
def api_earn_posts():
    db   = get_db()
    uid  = session['user_id']
    page = safe_int(request.args.get('page'), 1)
    per  = 10
    off  = (page - 1) * per

    rows = db.execute("""
        SELECT DISTINCT p.* FROM posts p
        JOIN post_boosts pb ON pb.post_id = p.id
        WHERE pb.status='active'
          AND pb.budget_spent < pb.budget
          AND pb.user_id != ?
          AND NOT EXISTS (
            SELECT 1 FROM boost_engagements be
            WHERE be.boost_id=pb.id AND be.worker_id=?
          )
        ORDER BY pb.reward_per_engage DESC, p.created_at DESC LIMIT ? OFFSET ?
    """, (uid, uid, per, off)).fetchall()

    posts    = [_format_post(r, uid, db) for r in rows]
    has_more = len(rows) == per
    return jsonify({'posts': posts, 'has_more': has_more})


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — Tips
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/post/<int:post_id>/tip', methods=['POST'])
@login_required
def tip_post(post_id):
    """Send a USDT tip tied to a specific post."""
    db  = get_db()
    uid = session['user_id']

    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Post not found.'}), 404
    if post['user_id'] == uid:
        return jsonify({'success': False, 'error': 'Cannot tip your own post.'}), 400

    amount  = safe_float(request.form.get('amount'), 0)
    message = (request.form.get('message') or '').strip()[:120]

    if amount < 0.01:
        return jsonify({'success': False, 'error': 'Minimum tip is $0.01.'}), 400

    sender = db.execute('SELECT balance, username FROM users WHERE id=?', (uid,)).fetchone()
    if amount > sender['balance']:
        return jsonify({'success': False, 'error': 'Insufficient balance.'}), 400

    # Debit sender, credit recipient
    db.execute('UPDATE users SET balance=balance-?, total_tips_sent=total_tips_sent+? WHERE id=?',
               (amount, amount, uid))
    db.execute('UPDATE users SET balance=balance+?, total_tips_received=total_tips_received+? WHERE id=?',
               (amount, amount, post['user_id']))

    db.execute("""
        INSERT INTO tips (from_user_id, to_user_id, post_id, amount, message)
        VALUES (?,?,?,?,?)
    """, (uid, post['user_id'], post_id, amount, message or None))

    add_transaction(db, uid, 'tip_sent', amount,
                    f'Tip to @{db.execute("SELECT username FROM users WHERE id=?", (post["user_id"],)).fetchone()["username"]} on post #{post_id}')
    add_transaction(db, post['user_id'], 'tip_received', amount,
                    f'Tip from @{sender["username"]} on post #{post_id}')

    tip_msg = f'💰 @{sender["username"]} tipped you ${amount:.2f} USDT'
    if message:
        tip_msg += f': "{message}"'
    add_notification(db, post['user_id'], tip_msg)
    db.commit()

    new_bal = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()['balance']
    return jsonify({'success': True, 'amount': amount, 'balance': new_bal,
                    'message': f'${amount:.2f} tip sent!'})


@app.route('/user/<username>/tip', methods=['POST'])
@login_required
def tip_user(username):
    """Send a direct USDT tip to a user (not tied to a post)."""
    db  = get_db()
    uid = session['user_id']

    target = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not target:
        return jsonify({'success': False, 'error': 'User not found.'}), 404
    if target['id'] == uid:
        return jsonify({'success': False, 'error': 'Cannot tip yourself.'}), 400

    amount  = safe_float(request.form.get('amount'), 0)
    message = (request.form.get('message') or '').strip()[:120]

    if amount < 0.01:
        return jsonify({'success': False, 'error': 'Minimum tip is $0.01.'}), 400

    sender = db.execute('SELECT balance, username FROM users WHERE id=?', (uid,)).fetchone()
    if amount > sender['balance']:
        return jsonify({'success': False, 'error': 'Insufficient balance.'}), 400

    db.execute('UPDATE users SET balance=balance-?, total_tips_sent=total_tips_sent+? WHERE id=?',
               (amount, amount, uid))
    db.execute('UPDATE users SET balance=balance+?, total_tips_received=total_tips_received+? WHERE id=?',
               (amount, amount, target['id']))

    db.execute("""
        INSERT INTO tips (from_user_id, to_user_id, amount, message)
        VALUES (?,?,?,?)
    """, (uid, target['id'], amount, message or None))

    add_transaction(db, uid, 'tip_sent', amount, f'Tip to @{username}')
    add_transaction(db, target['id'], 'tip_received', amount, f'Tip from @{sender["username"]}')

    notif = f'💰 @{sender["username"]} tipped you ${amount:.2f} USDT'
    if message:
        notif += f': "{message}"'
    add_notification(db, target['id'], notif)
    db.commit()

    new_bal = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()['balance']
    return jsonify({'success': True, 'amount': amount, 'balance': new_bal,
                    'message': f'${amount:.2f} sent to @{username}!'})


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — Subscription tiers
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/creator/setup', methods=['GET', 'POST'])
@login_required
def creator_setup():
    """Create or update the current user's subscription tier."""
    db  = get_db()
    uid = session['user_id']

    if request.method == 'POST':
        price       = safe_float(request.form.get('price_usd'), 0)
        title       = (request.form.get('title') or '').strip()[:60]
        description = (request.form.get('description') or '').strip()[:300]
        perks       = (request.form.get('perks') or '').strip()[:500]
        is_active   = 1 if request.form.get('is_active') else 0

        if price < 0.10:
            return jsonify({'success': False,
                            'error': 'Minimum subscription price is $0.10/month.'}), 400
        if not title:
            return jsonify({'success': False, 'error': 'Tier title is required.'}), 400

        existing = db.execute(
            'SELECT id FROM subscription_tiers WHERE creator_id=?', (uid,)
        ).fetchone()

        if existing:
            db.execute("""
                UPDATE subscription_tiers
                SET price_usd=?, title=?, description=?, perks=?, is_active=?
                WHERE creator_id=?
            """, (price, title, description, perks, is_active, uid))
        else:
            db.execute("""
                INSERT INTO subscription_tiers
                    (creator_id, price_usd, title, description, perks, is_active)
                VALUES (?,?,?,?,?,?)
            """, (uid, price, title, description, perks, is_active))

        db.commit()
        me = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
        return jsonify({'success': True,
                        'redirect': url_for('profile', username=me['username'])})

    tier = db.execute(
        'SELECT * FROM subscription_tiers WHERE creator_id=?', (uid,)
    ).fetchone()
    return render_template('creator_setup.html', tier=dict(tier) if tier else None)


@app.route('/user/<username>/subscribe', methods=['POST'])
@login_required
def subscribe(username):
    """Subscribe to a creator — charge wallet monthly rate immediately."""
    db  = get_db()
    uid = session['user_id']

    creator = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not creator:
        return jsonify({'success': False, 'error': 'User not found.'}), 404
    if creator['id'] == uid:
        return jsonify({'success': False, 'error': 'Cannot subscribe to yourself.'}), 400

    tier = db.execute(
        "SELECT * FROM subscription_tiers WHERE creator_id=? AND is_active=1",
        (creator['id'],)
    ).fetchone()
    if not tier:
        return jsonify({'success': False,
                        'error': 'This creator has no active subscription tier.'}), 400

    existing = db.execute(
        "SELECT * FROM subscriptions WHERE subscriber_id=? AND creator_id=?",
        (uid, creator['id'])
    ).fetchone()
    if existing and existing['status'] == 'active':
        return jsonify({'success': False, 'error': 'Already subscribed.'}), 400

    subscriber = db.execute('SELECT balance, username FROM users WHERE id=?', (uid,)).fetchone()
    price = float(tier['price_usd'])
    if price > subscriber['balance']:
        return jsonify({'success': False,
                        'error': f'Insufficient balance. Need ${price:.2f}.'}), 400

    from datetime import timedelta
    now     = datetime.now(timezone.utc)
    expires = (now + timedelta(days=30)).isoformat()

    # Charge subscriber
    db.execute('UPDATE users SET balance=balance-? WHERE id=?', (price, uid))
    # Pay creator (platform takes 0% — creators keep everything)
    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (price, creator['id']))

    if existing:
        db.execute("""
            UPDATE subscriptions SET status='active', started_at=?, expires_at=?, tier_id=?
            WHERE subscriber_id=? AND creator_id=?
        """, (now.isoformat(), expires, tier['id'], uid, creator['id']))
    else:
        db.execute("""
            INSERT INTO subscriptions (subscriber_id, creator_id, tier_id, started_at, expires_at)
            VALUES (?,?,?,?,?)
        """, (uid, creator['id'], tier['id'], now.isoformat(), expires))
        # Update creator subscriber count
        db.execute("""
            UPDATE users SET subscriber_count=(
                SELECT COUNT(*) FROM subscriptions
                WHERE creator_id=? AND status='active'
            ) WHERE id=?
        """, (creator['id'], creator['id']))

    add_transaction(db, uid, 'subscription', price,
                    f'Subscription to @{username} ({tier["title"]})')
    add_transaction(db, creator['id'], 'earn', price,
                    f'Subscription from @{subscriber["username"]} ({tier["title"]})')
    add_notification(db, creator['id'],
        f'🎉 @{subscriber["username"]} subscribed to your {tier["title"]} tier — '
        f'${price:.2f}/month!')
    add_notification(db, uid,
        f'✅ You\'re subscribed to @{username}\'s {tier["title"]} tier. Expires in 30 days.')
    db.commit()

    return jsonify({'success': True, 'price': price,
                    'message': f'Subscribed to @{username}!'})


@app.route('/user/<username>/unsubscribe', methods=['POST'])
@login_required
def unsubscribe(username):
    """Cancel a subscription (no refund — access remains until expiry)."""
    db  = get_db()
    uid = session['user_id']

    creator = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if not creator:
        return jsonify({'success': False, 'error': 'User not found.'}), 404

    sub = db.execute(
        "SELECT * FROM subscriptions WHERE subscriber_id=? AND creator_id=? AND status='active'",
        (uid, creator['id'])
    ).fetchone()
    if not sub:
        return jsonify({'success': False, 'error': 'No active subscription found.'}), 404

    db.execute(
        "UPDATE subscriptions SET status='cancelled' WHERE subscriber_id=? AND creator_id=?",
        (uid, creator['id'])
    )
    db.execute("""
        UPDATE users SET subscriber_count=(
            SELECT COUNT(*) FROM subscriptions WHERE creator_id=? AND status='active'
        ) WHERE id=?
    """, (creator['id'], creator['id']))
    add_notification(db, uid,
        f'↩️ Subscription to @{username} cancelled. Access lasts until {sub["expires_at"][:10]}.')
    db.commit()

    return jsonify({'success': True, 'expires_at': sub['expires_at'][:10],
                    'message': f'Subscription cancelled. Access until {sub["expires_at"][:10]}.'})


@app.route('/user/<username>/subscribers')
@login_required
def subscriber_list(username):
    """Show a creator's subscriber list (visible to creator + admin only)."""
    db  = get_db()
    uid = session['user_id']

    creator = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not creator:
        return render_template('error.html', code=404, message='User not found.'), 404

    viewer = db.execute('SELECT is_admin FROM users WHERE id=?', (uid,)).fetchone()
    if creator['id'] != uid and not viewer['is_admin']:
        return render_template('error.html', code=403, message='Access denied.'), 403

    subs = db.execute("""
        SELECT s.*, u.username, u.display_name, u.avatar_url, u.is_verified,
               u.follower_count, t.title as tier_title, t.price_usd
        FROM subscriptions s
        JOIN users u ON u.id=s.subscriber_id
        JOIN subscription_tiers t ON t.id=s.tier_id
        WHERE s.creator_id=?
        ORDER BY s.started_at DESC
    """, (creator['id'],)).fetchall()

    active_count    = sum(1 for s in subs if s['status'] == 'active')
    monthly_revenue = sum(float(s['price_usd']) for s in subs if s['status'] == 'active')

    return render_template('subscriber_list.html',
                           creator=dict(creator),
                           subs=[dict(s) for s in subs],
                           active_count=active_count,
                           monthly_revenue=monthly_revenue)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — Creator earnings dashboard
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/creator/earnings')
@login_required
def creator_earnings():
    db  = get_db()
    uid = session['user_id']

    tier = db.execute(
        'SELECT * FROM subscription_tiers WHERE creator_id=?', (uid,)
    ).fetchone()

    # Tips received
    tips_received = db.execute("""
        SELECT t.*, u.username as sender_name, u.avatar_url as sender_avatar,
               p.body as post_body
        FROM tips t
        JOIN users u ON u.id=t.from_user_id
        LEFT JOIN posts p ON p.id=t.post_id
        WHERE t.to_user_id=?
        ORDER BY t.created_at DESC LIMIT 50
    """, (uid,)).fetchall()

    # Tips sent
    tips_sent = db.execute("""
        SELECT t.*, u.username as recipient_name
        FROM tips t JOIN users u ON u.id=t.to_user_id
        WHERE t.from_user_id=? ORDER BY t.created_at DESC LIMIT 20
    """, (uid,)).fetchall()

    # Active subscribers
    subscribers = db.execute("""
        SELECT s.*, u.username, u.display_name, u.avatar_url,
               t.title as tier_title, t.price_usd
        FROM subscriptions s
        JOIN users u ON u.id=s.subscriber_id
        JOIN subscription_tiers t ON t.id=s.tier_id
        WHERE s.creator_id=? AND s.status='active'
        ORDER BY s.started_at DESC
    """, (uid,)).fetchall()

    # Subscription revenue (all time)
    sub_revenue = db.execute("""
        SELECT COALESCE(SUM(amount),0) FROM transactions
        WHERE user_id=? AND type='earn'
          AND description LIKE 'Subscription from %'
    """, (uid,)).fetchone()[0]

    # Boost earnings (engaging with others)
    boost_earned = db.execute("""
        SELECT COALESCE(SUM(reward),0) FROM boost_engagements WHERE worker_id=?
    """, (uid,)).fetchone()[0]

    me = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    total_tips_received = float(me['total_tips_received'] or 0)
    monthly_subs = sum(float(s['price_usd']) for s in subscribers)

    return render_template('creator_earnings.html',
                           tier=dict(tier) if tier else None,
                           tips_received=[dict(t) for t in tips_received],
                           tips_sent=[dict(t) for t in tips_sent],
                           subscribers=[dict(s) for s in subscribers],
                           total_tips=total_tips_received,
                           sub_revenue=float(sub_revenue),
                           boost_earned=float(boost_earned),
                           monthly_subs=monthly_subs,
                           me=dict(me))


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — Creator stats API (for profile sidebar)
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/creator/stats/<username>')
@login_required
def api_creator_stats(username):
    db = get_db()
    uid = session['user_id']

    creator = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not creator:
        return jsonify({'success': False}), 404

    tier = db.execute(
        "SELECT * FROM subscription_tiers WHERE creator_id=? AND is_active=1",
        (creator['id'],)
    ).fetchone()

    is_subscribed = bool(db.execute(
        "SELECT 1 FROM subscriptions WHERE subscriber_id=? AND creator_id=? AND status='active'",
        (uid, creator['id'])
    ).fetchone()) if uid != creator['id'] else False

    top_tips = db.execute("""
        SELECT t.amount, t.message, u.username, u.avatar_url
        FROM tips t JOIN users u ON u.id=t.from_user_id
        WHERE t.to_user_id=?
        ORDER BY t.amount DESC LIMIT 3
    """, (creator['id'],)).fetchall()

    return jsonify({
        'success': True,
        'tier': dict(tier) if tier else None,
        'is_subscribed': is_subscribed,
        'subscriber_count': creator['subscriber_count'] or 0,
        'total_tips': float(creator['total_tips_received'] or 0),
        'top_tips': [dict(t) for t in top_tips],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — Upgrade post creation to support subscriber-only posts
# ─────────────────────────────────────────────────────────────────────────────
# (handled via is_subscriber_only flag in create_post — injected below)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — Discovery: upgraded Explore, search, trending, recommendations
# ─────────────────────────────────────────────────────────────────────────────

def _save_search(db, uid, query, result_type='mixed'):
    """Persist a search query for history and ranking."""
    if not query or len(query) < 2:
        return
    db.execute(
        'INSERT INTO search_history (user_id, query, result_type) VALUES (?,?,?)',
        (uid, query[:100], result_type)
    )
    # Increment searched-for users' search_count for ranking
    db.execute("""
        UPDATE users SET search_count = search_count + 1
        WHERE username LIKE ? OR display_name LIKE ?
    """, (f'%{query}%', f'%{query}%'))


def _trending_hashtags(db, hours=48, limit=15):
    """Return list of {name, cnt, velocity} sorted by velocity."""
    rows = db.execute("""
        SELECT h.name,
               COUNT(ph.post_id)                                             AS cnt,
               COUNT(CASE WHEN p.created_at >= datetime('now','-6 hours')
                          THEN 1 END)                                        AS recent_cnt
        FROM hashtags h
        JOIN post_hashtags ph ON ph.hashtag_id = h.id
        JOIN posts p          ON p.id = ph.post_id
        WHERE p.created_at >= datetime('now', ? || ' hours')
        GROUP BY h.id
        HAVING cnt > 0
        ORDER BY (recent_cnt * 3 + cnt) DESC
        LIMIT ?
    """, (f'-{hours}', limit)).fetchall()
    return [dict(r) for r in rows]


def _who_to_follow(db, uid, limit=8):
    """
    Ranked "who to follow" recommendations:
    - People followed by your followees (second-degree)
    - Filtered: not already followed, not self
    - Sorted by: mutual-follower count DESC, then follower_count DESC
    """
    rows = db.execute("""
        SELECT u.id, u.username, u.display_name, u.avatar_url,
               u.is_verified, u.follower_count, u.bio, u.subscriber_count,
               COUNT(DISTINCT f2.follower_id) AS mutual_count
        FROM users u
        JOIN follows f1  ON f1.following_id = u.id          -- someone follows u
        JOIN follows f2  ON f2.following_id = f1.follower_id -- viewer also follows that someone
        WHERE f2.follower_id = ?
          AND u.id != ?
          AND u.id NOT IN (SELECT following_id FROM follows WHERE follower_id=?)
        GROUP BY u.id
        ORDER BY mutual_count DESC, u.follower_count DESC
        LIMIT ?
    """, (uid, uid, uid, limit)).fetchall()

    if len(rows) < limit:
        # Top accounts the viewer isn't following yet
        existing_ids = [r['id'] for r in rows] + [uid]
        ph = ','.join('?' * len(existing_ids))
        extra = db.execute(
            f'SELECT id,username,display_name,avatar_url,is_verified,'
            f'follower_count,bio,subscriber_count, 0 AS mutual_count '
            f'FROM users WHERE id NOT IN ({ph}) '
            f'AND id NOT IN (SELECT following_id FROM follows WHERE follower_id=?) '
            f'ORDER BY follower_count DESC LIMIT ?',
            existing_ids + [uid, limit - len(rows)]
        ).fetchall()
        rows = list(rows) + list(extra)

    return [dict(r) for r in rows]


@app.route('/explore')
@login_required
def explore():
    db   = get_db()
    uid  = session['user_id']
    q    = request.args.get('q', '').strip()
    tab  = request.args.get('tab', 'top')   # top | people | posts | tags | latest

    posts = []
    users = []
    tags  = []

    if q:
        _save_search(db, uid, q)
        like = f'%{q}%'

        if tab in ('top', 'posts', 'latest'):
            order = 'p.created_at DESC' if tab == 'latest' else 'p.score DESC, p.like_count DESC'
            post_rows = db.execute(f"""
                SELECT p.* FROM posts p
                WHERE p.body LIKE ? AND p.reply_to_id IS NULL
                ORDER BY {order} LIMIT 40
            """, (like,)).fetchall()
            posts = [_format_post(r, uid, db) for r in post_rows]

        if tab in ('top', 'people'):
            user_rows = db.execute("""
                SELECT id, username, display_name, avatar_url, is_verified,
                       follower_count, bio, subscriber_count,
                       EXISTS(SELECT 1 FROM follows
                              WHERE follower_id=? AND following_id=id) AS you_follow
                FROM users
                WHERE (username LIKE ? OR display_name LIKE ?) AND id != ?
                ORDER BY follower_count DESC LIMIT 12
            """, (uid, like, like, uid)).fetchall()
            users = [dict(u) for u in user_rows]

        if tab in ('top', 'tags'):
            tag_q = q.lstrip('#').lower()
            tag_rows = db.execute("""
                SELECT h.name,
                       COUNT(ph.post_id) AS cnt
                FROM hashtags h
                JOIN post_hashtags ph ON ph.hashtag_id = h.id
                WHERE h.name LIKE ?
                GROUP BY h.id
                ORDER BY cnt DESC LIMIT 10
            """, (f'%{tag_q}%',)).fetchall()
            tags = [dict(t) for t in tag_rows]

        db.commit()   # persist search_history insert

    # Always fetch: trending tags, who to follow, top posts for sidebar
    trending_tags  = _trending_hashtags(db, hours=48, limit=12)
    who_to_follow  = _who_to_follow(db, uid, limit=6)

    # Recent search history (last 8 unique queries)
    history = db.execute("""
        SELECT DISTINCT query FROM search_history
        WHERE user_id=? ORDER BY created_at DESC LIMIT 8
    """, (uid,)).fetchall()
    recent_searches = [r['query'] for r in history]

    # Trending posts (last 6h, scored)
    trending_posts = db.execute("""
        SELECT p.* FROM posts p
        WHERE p.reply_to_id IS NULL
          AND p.created_at >= datetime('now', '-6 hours')
        ORDER BY p.score DESC LIMIT 8
    """).fetchall()
    trending_posts = [_format_post(r, uid, db) for r in trending_posts]

    return render_template('explore.html',
                           q=q, tab=tab,
                           posts=posts, users=users, tags=tags,
                           trending_tags=trending_tags,
                           who_to_follow=who_to_follow,
                           recent_searches=recent_searches,
                           trending_posts=trending_posts)


@app.route('/api/search/autocomplete')
@login_required
def search_autocomplete():
    """Live autocomplete: return matching users and hashtags."""
    db  = get_db()
    uid = session['user_id']
    q   = request.args.get('q', '').strip()
    if len(q) < 1:
        return jsonify({'users': [], 'tags': []})
    like = f'{q}%'

    users = db.execute("""
        SELECT username, display_name, avatar_url, is_verified, follower_count
        FROM users WHERE (username LIKE ? OR display_name LIKE ?) AND id != ?
        ORDER BY follower_count DESC LIMIT 5
    """, (like, like, uid)).fetchall()

    tags = db.execute("""
        SELECT h.name, COUNT(ph.post_id) AS cnt
        FROM hashtags h JOIN post_hashtags ph ON ph.hashtag_id=h.id
        WHERE h.name LIKE ?
        GROUP BY h.id ORDER BY cnt DESC LIMIT 5
    """, (like,)).fetchall()

    return jsonify({
        'users': [dict(u) for u in users],
        'tags':  [dict(t) for t in tags],
    })


@app.route('/api/trending/posts')
@login_required
def api_trending_posts():
    """Top scored posts in a rolling window — used by explore sidebar."""
    db     = get_db()
    uid    = session['user_id']
    window = request.args.get('window', '24h')
    hours  = {'6h': 6, '24h': 24, '48h': 48, '7d': 168}.get(window, 24)

    rows = db.execute("""
        SELECT p.* FROM posts p
        WHERE p.reply_to_id IS NULL
          AND p.created_at >= datetime('now', ? || ' hours')
        ORDER BY p.score DESC LIMIT 10
    """, (f'-{hours}',)).fetchall()
    return jsonify([_format_post(r, uid, db) for r in rows])


@app.route('/api/trending/tags')
@login_required
def api_trending_tags():
    db = get_db()
    return jsonify(_trending_hashtags(db, hours=48, limit=15))


@app.route('/api/who-to-follow')
@login_required
def api_who_to_follow():
    db  = get_db()
    uid = session['user_id']
    recs = _who_to_follow(db, uid, limit=8)
    # add you_follow flag
    for u in recs:
        u['you_follow'] = bool(db.execute(
            'SELECT 1 FROM follows WHERE follower_id=? AND following_id=?',
            (uid, u['id'])
        ).fetchone())
    return jsonify(recs)


@app.route('/api/post/<int:post_id>/view', methods=['POST'])
@login_required
def record_post_view(post_id):
    """Record that the viewer has seen a post (for personalisation + view counts)."""
    db  = get_db()
    uid = session['user_id']
    try:
        db.execute(
            'INSERT OR IGNORE INTO post_views (post_id, user_id) VALUES (?,?)',
            (post_id, uid)
        )
        db.execute(
            'UPDATE posts SET view_count=view_count+1 WHERE id=? '
            'AND NOT EXISTS (SELECT 1 FROM post_views WHERE post_id=? AND user_id=?)',
            (post_id, post_id, uid)
        )
        _recalc_post_score(db, post_id)
        db.commit()
    except Exception:
        pass
    return jsonify({'ok': True})


@app.route('/trending')
@login_required
def trending():
    """Dedicated trending page — top posts + tags across multiple time windows."""
    db     = get_db()
    uid    = session['user_id']
    window = request.args.get('w', '24h')
    hours  = {'6h': 6, '24h': 24, '48h': 48, '7d': 168}.get(window, 24)

    top_posts = db.execute("""
        SELECT p.* FROM posts p
        WHERE p.reply_to_id IS NULL
          AND p.created_at >= datetime('now', ? || ' hours')
        ORDER BY p.score DESC LIMIT 30
    """, (f'-{hours}',)).fetchall()
    top_posts = [_format_post(r, uid, db) for r in top_posts]

    top_tags       = _trending_hashtags(db, hours=hours, limit=20)
    who_to_follow  = _who_to_follow(db, uid, limit=6)

    # Rising creators: accounts who gained most followers recently
    rising = db.execute("""
        SELECT u.id, u.username, u.display_name, u.avatar_url,
               u.is_verified, u.follower_count, u.bio,
               COUNT(f.follower_id) AS new_followers
        FROM users u
        JOIN follows f ON f.following_id = u.id
        WHERE f.created_at >= datetime('now', ? || ' hours')
          AND u.id != ?
          AND u.id NOT IN (SELECT following_id FROM follows WHERE follower_id=?)
        GROUP BY u.id
        ORDER BY new_followers DESC
        LIMIT 5
    """, (f'-{hours}', uid, uid)).fetchall()
    rising = [dict(r) for r in rising]

    return render_template('trending.html',
                           top_posts=top_posts,
                           top_tags=top_tags,
                           who_to_follow=who_to_follow,
                           rising=rising,
                           window=window)


@app.route('/api/search/history/clear', methods=['POST'])
@login_required
def clear_search_history():
    db  = get_db()
    uid = session['user_id']
    db.execute('DELETE FROM search_history WHERE user_id=?', (uid,))
    db.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────────────────────────────────────────
# Direct Messages
# ─────────────────────────────────────────────────────────────────────────────

def _get_or_create_conversation(db, uid, other_id):
    """Return conversation row, creating it if needed. Always orders user_a < user_b."""
    a, b = min(uid, other_id), max(uid, other_id)
    conv = db.execute(
        'SELECT * FROM conversations WHERE user_a=? AND user_b=?', (a, b)
    ).fetchone()
    if not conv:
        db.execute(
            'INSERT INTO conversations (user_a, user_b, last_msg_at) VALUES (?,?,datetime("now"))',
            (a, b)
        )
        conv_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        conv = db.execute('SELECT * FROM conversations WHERE id=?', (conv_id,)).fetchone()
    return conv


def _format_conversation(conv, uid, db):
    """Enrich a conversation row with the other user's info and last message."""
    other_id = conv['user_b'] if conv['user_a'] == uid else conv['user_a']
    other = db.execute(
        'SELECT id, username, display_name, avatar_url, is_verified FROM users WHERE id=?',
        (other_id,)
    ).fetchone()
    try:
        last_msg = db.execute(
            'SELECT id, conversation_id, sender_id, body, msg_type, file_name, is_read, created_at '
            'FROM messages WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1',
            (conv['id'],)
        ).fetchone()
        unread = db.execute(
            'SELECT COUNT(*) FROM messages '
            'WHERE conversation_id=? AND sender_id!=? AND is_read=0',
            (conv['id'], uid)
        ).fetchone()[0]
    except Exception:
        last_msg = None
        unread   = 0
    return {
        'id':          conv['id'],
        'other':       dict(other) if other else {},
        'last_msg':    dict(last_msg) if last_msg else None,
        'unread':      unread,
        'last_msg_at': conv['last_msg_at'],
    }


@app.route('/messages')
@login_required
def messages_inbox():
    """Conversation list — all threads the current user is part of."""
    db  = get_db()
    uid = session['user_id']

    rows = db.execute("""
        SELECT * FROM conversations
        WHERE user_a=? OR user_b=?
        ORDER BY last_msg_at DESC
    """, (uid, uid)).fetchall()

    convs = [_format_conversation(r, uid, db) for r in rows]

    # Mark unread DM count to 0 when inbox is opened
    db.execute('UPDATE users SET unread_dm_count=0 WHERE id=?', (uid,))
    db.commit()

    now_str = datetime.now(timezone.utc).isoformat()[:10]
    return render_template('messages.html', conversations=convs, now_str=now_str)


@app.route('/messages/<username>')
@login_required
def message_thread(username):
    """Individual DM thread with a specific user."""
    db  = get_db()
    uid = session['user_id']

    other = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not other:
        return render_template('error.html', code=404, message='User not found.'), 404
    if other['id'] == uid:
        return redirect(url_for('messages_inbox'))

    conv = _get_or_create_conversation(db, uid, other['id'])

    # Load messages (newest last)
    msgs = db.execute("""
        SELECT m.*,
               u.username as sender_username, u.avatar_url as sender_avatar
        FROM messages m JOIN users u ON u.id = m.sender_id
        WHERE m.conversation_id=?
        ORDER BY m.created_at ASC LIMIT 100
    """, (conv['id'],)).fetchall()
    msgs = [dict(m) for m in msgs]
    # Ensure new optional columns are always present in the dict
    for m in msgs:
        m.setdefault('edited_at',   None)
        m.setdefault('reactions',   None)
        m.setdefault('is_pinned',   0)
        m.setdefault('reply_to_id', None)
        m.setdefault('deleted_at',  None)

    # Mark all as read
    db.execute(
        'UPDATE messages SET is_read=1 WHERE conversation_id=? AND sender_id!=?',
        (conv['id'], uid)
    )
    # Recompute unread_dm_count
    total_unread = db.execute("""
        SELECT COUNT(*) FROM messages m
        JOIN conversations c ON c.id=m.conversation_id
        WHERE (c.user_a=? OR c.user_b=?) AND m.sender_id!=? AND m.is_read=0
    """, (uid, uid, uid)).fetchone()[0]
    db.execute('UPDATE users SET unread_dm_count=? WHERE id=?', (total_unread, uid))
    db.commit()

    return render_template('message_thread.html',
                           other=dict(other), messages=msgs,
                           conv_id=conv['id'])


@app.route('/messages/<username>/send', methods=['POST'])
@login_required
def send_message(username):
    db  = get_db()
    uid = session['user_id']

    other = db.execute('SELECT id, username FROM users WHERE username=?', (username,)).fetchone()
    if not other or other['id'] == uid:
        return jsonify({'success': False, 'error': 'Invalid recipient.'}), 400

    # Accept both form-data and JSON payloads
    ct = request.content_type or ''
    if 'application/json' in ct:
        _d = request.get_json(silent=True) or {}
        body      = (_d.get('body') or '').strip() or None
        msg_type  = (_d.get('msg_type') or 'text').strip().lower()
        file_name = (_d.get('file_name') or '') or None
        file_mime = (_d.get('file_mime') or '') or None
        file_data = _d.get('file_data') or None  # base64 data-URI
    else:
        body      = (request.form.get('body') or '').strip() or None
        msg_type  = (request.form.get('msg_type') or 'text').strip().lower()
        file_name = (request.form.get('file_name') or '') or None
        file_mime = (request.form.get('file_mime') or '') or None
        file_data = request.form.get('file_data') or None  # base64 data-URI

    if msg_type not in ('text', 'image', 'file', 'voice', 'video'):
        msg_type = 'text'

    # For text messages body is required; for media file_data is required
    if msg_type == 'text' and not body:
        return jsonify({'success': False, 'error': 'Message cannot be empty.'}), 400
    if msg_type != 'text' and not file_data:
        return jsonify({'success': False, 'error': 'No file data received.'}), 400
    if body and len(body) > 2000:
        return jsonify({'success': False, 'error': 'Message too long (max 2000 chars).'}), 400

    conv = _get_or_create_conversation(db, uid, other['id'])
    now  = datetime.now(timezone.utc).isoformat()

    db.execute(
        '''INSERT INTO messages
           (conversation_id, sender_id, body, msg_type, file_data, file_name, file_mime, created_at)
           VALUES (?,?,?,?,?,?,?,?)''',
        (conv['id'], uid, body, msg_type, file_data, file_name, file_mime, now)
    )
    db.execute('UPDATE conversations SET last_msg_at=? WHERE id=?', (now, conv['id']))
    db.execute('UPDATE users SET unread_dm_count=unread_dm_count+1 WHERE id=?', (other['id'],))

    # Update sender's online_at heartbeat
    db.execute('UPDATE users SET online_at=? WHERE id=?', (now, uid))

    me = db.execute('SELECT username, avatar_url FROM users WHERE id=?', (uid,)).fetchone()
    # DM messages do NOT create notifications — they use the unread_dm_count badge only
    db.commit()

    msg_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    return jsonify({
        'success': True,
        'message': {
            'id':              msg_id,
            'body':            body,
            'msg_type':        msg_type,
            'file_data':       file_data,
            'file_name':       file_name,
            'file_mime':       file_mime,
            'sender_id':       uid,
            'sender_username': me['username'],
            'sender_avatar':   me['avatar_url'],
            'created_at':      now,
            'is_read':         0,
        }
    })


@app.route('/api/messages/<username>/poll')
@login_required
def poll_messages(username):
    """Long-poll endpoint — returns messages newer than ?after=<id>."""
    db    = get_db()
    uid   = session['user_id']
    after = request.args.get('after', 0, type=int)

    other = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if not other:
        return jsonify({'messages': []}), 404

    a, b = min(uid, other['id']), max(uid, other['id'])
    conv = db.execute('SELECT id FROM conversations WHERE user_a=? AND user_b=?', (a, b)).fetchone()
    if not conv:
        return jsonify({'messages': []})

    rows = db.execute("""
        SELECT m.*, u.username as sender_username, u.avatar_url as sender_avatar
        FROM messages m JOIN users u ON u.id=m.sender_id
        WHERE m.conversation_id=? AND m.id > ?
        ORDER BY m.created_at ASC LIMIT 50
    """, (conv['id'], after)).fetchall()

    if rows:
        # Mark as read
        db.execute(
            'UPDATE messages SET is_read=1 WHERE conversation_id=? AND sender_id!=? AND id > ?',
            (conv['id'], uid, after)
        )
        total_unread = db.execute("""
            SELECT COUNT(*) FROM messages m
            JOIN conversations c ON c.id=m.conversation_id
            WHERE (c.user_a=? OR c.user_b=?) AND m.sender_id!=? AND m.is_read=0
        """, (uid, uid, uid)).fetchone()[0]
        db.execute('UPDATE users SET unread_dm_count=? WHERE id=?', (total_unread, uid))
        db.commit()

    return jsonify({'messages': [dict(r) for r in rows]})


@app.route('/api/messages/unread')
@login_required
def api_unread_dms():
    db  = get_db()
    uid = session['user_id']
    count = db.execute('SELECT unread_dm_count FROM users WHERE id=?', (uid,)).fetchone()
    return jsonify({'count': int((count['unread_dm_count'] or 0)) if count else 0})


# ─────────────────────────────────────────────────────────────────────────────
# Photo upload for profile avatar / banner
# ─────────────────────────────────────────────────────────────────────────────

import base64 as _b64

ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # 5 MB


@app.route('/profile/upload-photo', methods=['POST'])
@login_required
def upload_profile_photo():
    """
    Accept a photo file upload (multipart/form-data) for avatar or banner.
    Stores as a data-URI in the database so no file system / CDN required.
    field: 'photo' (file), 'type': 'avatar' | 'banner'
    """
    db    = get_db()
    uid   = session['user_id']
    photo = request.files.get('photo')
    kind  = (request.form.get('type') or 'avatar').strip().lower()

    if kind not in ('avatar', 'banner'):
        return jsonify({'success': False, 'error': 'Invalid photo type.'}), 400
    if not photo or not photo.filename:
        return jsonify({'success': False, 'error': 'No file selected.'}), 400

    mime = photo.mimetype or ''
    if mime not in ALLOWED_IMAGE_TYPES:
        return jsonify({'success': False,
                        'error': 'Unsupported file type. Please upload JPG, PNG, WebP or GIF.'}), 400

    data = photo.read()
    if len(data) > MAX_IMAGE_BYTES:
        return jsonify({'success': False,
                        'error': f'File too large. Maximum size is 5 MB.'}), 400

    data_uri = f'data:{mime};base64,' + _b64.b64encode(data).decode('ascii')

    col = 'avatar_url' if kind == 'avatar' else 'banner_url'
    db.execute(f'UPDATE users SET {col}=? WHERE id=?', (data_uri, uid))
    db.commit()

    return jsonify({'success': True, 'url': data_uri, 'type': kind})



# ─────────────────────────────────────────────────────────────────────────────
# Messaging — Online status + typing + new helpers
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/online/heartbeat', methods=['POST'])
@login_required
def online_heartbeat():
    """Browser pings this every 30s to indicate the user is online."""
    db  = get_db()
    uid = session['user_id']
    now = datetime.now(timezone.utc).isoformat()
    db.execute('UPDATE users SET online_at=? WHERE id=?', (now, uid))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/online/status', methods=['POST'])
@login_required
def toggle_online_status():
    """Toggle whether this user's online status is visible to others."""
    db  = get_db()
    uid = session['user_id']
    data = request.get_json(silent=True) or {}
    show = 1 if data.get('show', True) else 0
    db.execute('UPDATE users SET show_online=? WHERE id=?', (show, uid))
    db.commit()
    return jsonify({'show_online': bool(show)})


@app.route('/api/online/check/<username>')
@login_required
def check_online(username):
    """Return whether a user is currently online (last heartbeat < 90s ago)."""
    db = get_db()
    row = db.execute(
        'SELECT online_at, show_online FROM users WHERE username=?', (username,)
    ).fetchone()
    if not row or not row['show_online']:
        return jsonify({'online': False})
    if not row['online_at']:
        return jsonify({'online': False})
    try:
        from datetime import timedelta
        last = datetime.fromisoformat(row['online_at'].replace('Z', ''))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        online = (datetime.now(timezone.utc) - last).total_seconds() < 90
        return jsonify({'online': online, 'last_seen': row['online_at'][:16]})
    except Exception:
        return jsonify({'online': False})


@app.route('/api/messages/<username>/typing', methods=['POST'])
@login_required
def set_typing(username):
    """
    Store a typing timestamp so the other user can poll and see the indicator.
    Uses a lightweight in-memory dict — good enough for single-server deploys.
    """
    uid = session['user_id']
    now = datetime.now(timezone.utc).timestamp()
    # Store in a module-level dict; lightweight alternative to Redis
    _typing_state[(uid, username)] = now
    return jsonify({'ok': True})


@app.route('/api/messages/<username>/is-typing')
@login_required
def is_typing(username):
    """Return True if the other user has typed in the last 3 seconds."""
    db    = get_db()
    uid   = session['user_id']
    other = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if not other:
        return jsonify({'typing': False})
    key = (other['id'], db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()['username'])
    ts  = _typing_state.get(key, 0)
    typing = (datetime.now(timezone.utc).timestamp() - ts) < 3
    return jsonify({'typing': typing})


# ─────────────────────────────────────────────────────────────────────────────
# Edit Post
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/post/<int:post_id>/edit', methods=['POST'])
@login_required
def edit_post(post_id):
    db  = get_db()
    uid = session['user_id']

    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Post not found.'}), 404
    if post['user_id'] != uid:
        return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    body = (request.form.get('body') or '').strip()
    if len(body) > 500:
        return jsonify({'success': False, 'error': 'Max 500 characters.'}), 400
    if not body:
        # only allow empty if media exists
        keys = post.keys()
        has_media = 'media_data' in keys and post['media_data']
        if not has_media:
            return jsonify({'success': False, 'error': 'Post cannot be empty.'}), 400

    now = datetime.now(timezone.utc).isoformat()
    db.execute('UPDATE posts SET body=?, edited_at=? WHERE id=?', (body or None, now, post_id))
    db.commit()

    updated = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    return jsonify({'success': True, 'post': _format_post(updated, uid, db)})


# ─────────────────────────────────────────────────────────────────────────────
# Message: Edit / Delete / React / Pin / Forward / Info
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/messages/edit/<int:msg_id>', methods=['POST'])
@login_required
def edit_message(msg_id):
    db  = get_db()
    uid = session['user_id']

    msg = db.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not msg:
        return jsonify({'success': False, 'error': 'Message not found.'}), 404
    if msg['sender_id'] != uid:
        return jsonify({'success': False, 'error': 'You can only edit your own messages.'}), 403

    keys = msg.keys()
    mt   = msg['msg_type'] if 'msg_type' in keys else 'text'
    if mt != 'text':
        return jsonify({'success': False, 'error': 'Only text messages can be edited.'}), 400

    data = request.get_json(silent=True) or {}
    body = (data.get('body') or '').strip()
    if not body:
        return jsonify({'success': False, 'error': 'Message cannot be empty.'}), 400
    if len(body) > 2000:
        return jsonify({'success': False, 'error': 'Message too long.'}), 400

    now = datetime.now(timezone.utc).isoformat()
    db.execute('UPDATE messages SET body=?, edited_at=? WHERE id=?', (body, now, msg_id))
    db.commit()
    return jsonify({'success': True, 'body': body, 'edited_at': now})


@app.route('/api/messages/delete/<int:msg_id>', methods=['POST'])
@login_required
def delete_message(msg_id):
    db  = get_db()
    uid = session['user_id']
    msg = db.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not msg:
        return jsonify({'success': False, 'error': 'Message not found.'}), 404
    if msg['sender_id'] != uid:
        return jsonify({'success': False, 'error': 'You can only delete your own messages.'}), 403
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE messages SET body='(deleted)', msg_type='text', file_data=NULL, "
        "file_name=NULL, file_mime=NULL, deleted_at=? WHERE id=?",
        (now, msg_id)
    )
    db.commit()
    return jsonify({'success': True})


@app.route('/api/messages/react/<int:msg_id>', methods=['POST'])
@login_required
def react_message(msg_id):
    import json as _json
    db    = get_db()
    uid   = session['user_id']
    data  = request.get_json(silent=True) or {}
    emoji = (data.get('emoji') or '').strip()
    if not emoji or len(emoji) > 8:
        return jsonify({'success': False, 'error': 'Invalid emoji.'}), 400

    msg = db.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not msg:
        return jsonify({'success': False, 'error': 'Message not found.'}), 404

    # Verify viewer is in the conversation
    conv = db.execute('SELECT * FROM conversations WHERE id=?', (msg['conversation_id'],)).fetchone()
    if not conv or (conv['user_a'] != uid and conv['user_b'] != uid):
        return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    raw = msg['reactions'] if 'reactions' in msg.keys() else None
    try:
        reactions = _json.loads(raw) if raw else {}
    except Exception:
        reactions = {}

    users = reactions.get(emoji, [])
    if uid in users:
        users.remove(uid)
    else:
        users.append(uid)
    if users:
        reactions[emoji] = users
    else:
        reactions.pop(emoji, None)

    db.execute('UPDATE messages SET reactions=? WHERE id=?', (_json.dumps(reactions), msg_id))
    db.commit()
    return jsonify({'success': True, 'reactions': reactions})


@app.route('/api/messages/pin/<int:msg_id>', methods=['POST'])
@login_required
def pin_message(msg_id):
    db  = get_db()
    uid = session['user_id']
    msg = db.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not msg:
        return jsonify({'success': False, 'error': 'Message not found.'}), 404

    conv = db.execute('SELECT * FROM conversations WHERE id=?', (msg['conversation_id'],)).fetchone()
    if not conv or (conv['user_a'] != uid and conv['user_b'] != uid):
        return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    current = msg['is_pinned'] if 'is_pinned' in msg.keys() else 0
    new_state = 0 if current else 1
    db.execute('UPDATE messages SET is_pinned=? WHERE id=?', (new_state, msg_id))
    db.commit()
    return jsonify({'success': True, 'pinned': bool(new_state)})


@app.route('/api/messages/info/<int:msg_id>')
@login_required
def message_info(msg_id):
    db  = get_db()
    uid = session['user_id']
    msg = db.execute(
        "SELECT m.*, u.username as sender_username, u.display_name as sender_display "
        "FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.id=?",
        (msg_id,)
    ).fetchone()
    if not msg:
        return jsonify({'success': False}), 404

    conv = db.execute('SELECT * FROM conversations WHERE id=?', (msg['conversation_id'],)).fetchone()
    if not conv or (conv['user_a'] != uid and conv['user_b'] != uid):
        return jsonify({'success': False}), 403

    keys = msg.keys()
    return jsonify({
        'success':   True,
        'sender':    msg['sender_username'],
        'sent_at':   msg['created_at'],
        'is_read':   bool(msg['is_read']),
        'edited_at': msg['edited_at'] if 'edited_at' in keys else None,
        'msg_type':  msg['msg_type'] if 'msg_type' in keys else 'text',
        'pinned':    bool(msg['is_pinned']) if 'is_pinned' in keys else False,
    })


@app.route('/api/messages/forward', methods=['POST'])
@login_required
def forward_message():
    db   = get_db()
    uid  = session['user_id']
    data = request.get_json(silent=True) or {}
    msg_id = data.get('msg_id')
    recipients = data.get('recipients') or []
    if not msg_id or not recipients:
        return jsonify({'success': False, 'error': 'Missing data.'}), 400

    src = db.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not src:
        return jsonify({'success': False, 'error': 'Source message not found.'}), 404

    src_conv = db.execute('SELECT * FROM conversations WHERE id=?', (src['conversation_id'],)).fetchone()
    if not src_conv or (src_conv['user_a'] != uid and src_conv['user_b'] != uid):
        return jsonify({'success': False, 'error': 'Not authorized.'}), 403

    keys = src.keys()
    body      = src['body']
    msg_type  = src['msg_type']  if 'msg_type'  in keys else 'text'
    file_data = src['file_data'] if 'file_data' in keys else None
    file_name = src['file_name'] if 'file_name' in keys else None
    file_mime = src['file_mime'] if 'file_mime' in keys else None

    sent = 0
    now  = datetime.now(timezone.utc).isoformat()
    for username in recipients[:10]:
        u = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if not u or u['id'] == uid:
            continue
        conv = _get_or_create_conversation(db, uid, u['id'])
        db.execute(
            "INSERT INTO messages (conversation_id, sender_id, body, msg_type, "
            "file_data, file_name, file_mime, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (conv['id'], uid, body, msg_type, file_data, file_name, file_mime, now)
        )
        db.execute('UPDATE conversations SET last_msg_at=? WHERE id=?', (now, conv['id']))
        db.execute('UPDATE users SET unread_dm_count=unread_dm_count+1 WHERE id=?', (u['id'],))
        sent += 1
    db.commit()
    return jsonify({'success': True, 'sent': sent})


# ─────────────────────────────────────────────────────────────────────────────
# User search for new-message picker
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/users/search')
@login_required
def search_users_for_dm():
    db  = get_db()
    uid = session['user_id']
    q   = (request.args.get('q') or '').strip()
    if len(q) < 1:
        return jsonify({'users': []})
    like = f'%{q}%'
    rows = db.execute(
        "SELECT username, display_name, avatar_url, is_verified, follower_count "
        "FROM users WHERE (username LIKE ? OR display_name LIKE ?) AND id != ? "
        "ORDER BY follower_count DESC LIMIT 10",
        (like, like, uid)
    ).fetchall()
    return jsonify({'users': [dict(u) for u in rows]})



# ─────────────────────────────────────────────────────────────────────────────
# Settings — allow_post_saves
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/settings/saves', methods=['POST'])
@login_required
def toggle_post_saves():
    db  = get_db()
    uid = session['user_id']
    data = request.get_json(silent=True) or {}
    allow = 1 if data.get('allow', True) else 0
    db.execute('UPDATE users SET allow_post_saves=? WHERE id=?', (allow, uid))
    db.commit()
    return jsonify({'success': True, 'allow_post_saves': bool(allow)})


# ─────────────────────────────────────────────────────────────────────────────
# Polls — vote
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/post/<int:post_id>/poll/vote', methods=['POST'])
@login_required
def poll_vote(post_id):
    db        = get_db()
    uid       = session['user_id']
    option_id = safe_int(request.form.get('option_id'), 0)

    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Post not found.'}), 404

    keys = post.keys()
    pt   = post['post_type'] if 'post_type' in keys else 'post'
    if pt != 'poll':
        return jsonify({'success': False, 'error': 'Not a poll.'}), 400

    exp = post['poll_expires_at'] if 'poll_expires_at' in keys else None
    if exp:
        try:
            from datetime import datetime as _dt, timezone as _tz
            if _dt.fromisoformat(exp.replace('Z','')) < _dt.now(_tz.utc):
                return jsonify({'success': False, 'error': 'This poll has ended.'}), 400
        except Exception:
            pass

    # Check valid option
    opt = db.execute('SELECT * FROM poll_options WHERE id=? AND post_id=?',
                     (option_id, post_id)).fetchone()
    if not opt:
        return jsonify({'success': False, 'error': 'Invalid option.'}), 400

    # Remove existing vote then insert
    existing = db.execute('SELECT option_id FROM poll_votes WHERE post_id=? AND user_id=?',
                          (post_id, uid)).fetchone()
    if existing:
        db.execute('UPDATE poll_options SET votes=MAX(0,votes-1) WHERE id=?', (existing['option_id'],))
        db.execute('DELETE FROM poll_votes WHERE post_id=? AND user_id=?', (post_id, uid))

    db.execute('INSERT INTO poll_votes (post_id,option_id,user_id) VALUES (?,?,?)',
               (post_id, option_id, uid))
    db.execute('UPDATE poll_options SET votes=votes+1 WHERE id=?', (option_id,))
    db.commit()

    options = db.execute('SELECT * FROM poll_options WHERE post_id=? ORDER BY id', (post_id,)).fetchall()
    total   = sum(o['votes'] for o in options)
    result  = [{'id': o['id'], 'label': o['label'], 'votes': o['votes'],
                'pct': round(o['votes']*100/total) if total else 0} for o in options]
    return jsonify({'success': True, 'options': result, 'total': total, 'user_vote': option_id})


# ─────────────────────────────────────────────────────────────────────────────
# Channels
# ─────────────────────────────────────────────────────────────────────────────

def _format_channel(ch, uid, db):
    row = dict(ch)
    row['is_member'] = bool(db.execute(
        'SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?', (ch['id'], uid)
    ).fetchone())
    row['is_owner'] = ch['owner_id'] == uid
    return row


@app.route('/channels')
@login_required
def channels_browse():
    db  = get_db()
    uid = session['user_id']
    q   = (request.args.get('q') or '').strip()
    tab = request.args.get('tab', 'discover')   # discover | joined | owned

    if tab == 'joined':
        rows = db.execute("""
            SELECT c.* FROM channels c
            JOIN channel_members cm ON cm.channel_id=c.id
            WHERE cm.user_id=?
            ORDER BY c.member_count DESC, c.created_at DESC LIMIT 40
        """, (uid,)).fetchall()
    elif tab == 'owned':
        rows = db.execute("""
            SELECT * FROM channels WHERE owner_id=?
            ORDER BY created_at DESC LIMIT 40
        """, (uid,)).fetchall()
    else:
        # Discover
        if q:
            rows = db.execute("""
                SELECT * FROM channels WHERE name LIKE ? OR description LIKE ?
                ORDER BY member_count DESC LIMIT 30
            """, (f'%{q}%', f'%{q}%')).fetchall()
        else:
            rows = db.execute("""
                SELECT * FROM channels ORDER BY member_count DESC, created_at DESC LIMIT 40
            """).fetchall()

    channels = [_format_channel(r, uid, db) for r in rows]
    return render_template('channels.html', channels=channels, tab=tab, q=q)


@app.route('/channel/create', methods=['GET', 'POST'])
@login_required
def channel_create():
    db  = get_db()
    uid = session['user_id']

    if request.method == 'POST':
        name        = (request.form.get('name') or '').strip()[:60]
        description = (request.form.get('description') or '').strip()[:300]
        is_public   = 1 if request.form.get('is_public', '1') != '0' else 0

        if not name:
            return jsonify({'success': False, 'error': 'Channel name is required.'}), 400

        import re as _re2
        slug = _re2.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')
        slug = _re2.sub(r'-+', '-', slug)[:50]
        if not slug:
            slug = f'channel-{uid}'

        # Ensure unique slug
        base_slug = slug
        for i in range(1, 10):
            ex = db.execute('SELECT 1 FROM channels WHERE slug=?', (slug,)).fetchone()
            if not ex:
                break
            slug = f'{base_slug}-{i}'

        try:
            db.execute("""
                INSERT INTO channels (name, slug, description, owner_id, is_public, member_count)
                VALUES (?,?,?,?,?,1)
            """, (name, slug, description or None, uid, is_public))
            ch_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            db.execute('INSERT INTO channel_members (channel_id,user_id,role) VALUES (?,?,?)',
                       (ch_id, uid, 'owner'))
            db.commit()
            return jsonify({'success': True, 'redirect': url_for('channel_detail', slug=slug)})
        except Exception as e:
            return jsonify({'success': False, 'error': 'Channel name already taken.'}), 400

    return render_template('channel_create.html')


@app.route('/channel/<slug>')
@login_required
def channel_detail(slug):
    db  = get_db()
    uid = session['user_id']

    ch = db.execute('SELECT * FROM channels WHERE slug=?', (slug,)).fetchone()
    if not ch:
        return render_template('error.html', code=404, message='Channel not found.'), 404

    is_member = bool(db.execute(
        'SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?', (ch['id'], uid)
    ).fetchone())

    if not ch['is_public'] and not is_member:
        return render_template('error.html', code=403, message='This channel is private.'), 403

    post_rows = db.execute("""
        SELECT p.* FROM posts p
        JOIN channel_posts cp ON cp.post_id=p.id
        WHERE cp.channel_id=?
        ORDER BY p.created_at DESC LIMIT 40
    """, (ch['id'],)).fetchall()
    posts = [_format_post_with_poll(r, uid, db) for r in post_rows]

    members = db.execute("""
        SELECT u.username, u.display_name, u.avatar_url, u.is_verified, cm.role
        FROM channel_members cm JOIN users u ON u.id=cm.user_id
        WHERE cm.channel_id=?
        ORDER BY CASE cm.role WHEN 'owner' THEN 0 WHEN 'mod' THEN 1 ELSE 2 END, cm.joined_at
        LIMIT 20
    """, (ch['id'],)).fetchall()

    return render_template('channel_detail.html',
                           ch=dict(ch), posts=posts,
                           members=[dict(m) for m in members],
                           is_member=is_member, is_owner=ch['owner_id']==uid)


@app.route('/channel/<slug>/join', methods=['POST'])
@login_required
def channel_join(slug):
    db  = get_db()
    uid = session['user_id']
    ch  = db.execute('SELECT * FROM channels WHERE slug=?', (slug,)).fetchone()
    if not ch:
        return jsonify({'success': False, 'error': 'Channel not found.'}), 404

    existing = db.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',
                          (ch['id'], uid)).fetchone()
    if existing:
        return jsonify({'success': False, 'error': 'Already a member.'}), 400

    db.execute('INSERT INTO channel_members (channel_id,user_id,role) VALUES (?,?,?)',
               (ch['id'], uid, 'member'))
    db.execute('UPDATE channels SET member_count=member_count+1 WHERE id=?', (ch['id'],))
    db.commit()
    return jsonify({'success': True, 'member_count': db.execute(
        'SELECT member_count FROM channels WHERE id=?', (ch['id'],)
    ).fetchone()[0]})


@app.route('/channel/<slug>/leave', methods=['POST'])
@login_required
def channel_leave(slug):
    db  = get_db()
    uid = session['user_id']
    ch  = db.execute('SELECT * FROM channels WHERE slug=?', (slug,)).fetchone()
    if not ch:
        return jsonify({'success': False, 'error': 'Channel not found.'}), 404
    if ch['owner_id'] == uid:
        return jsonify({'success': False, 'error': 'Owner cannot leave. Delete the channel instead.'}), 400

    db.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?', (ch['id'], uid))
    db.execute('UPDATE channels SET member_count=MAX(0,member_count-1) WHERE id=?', (ch['id'],))
    db.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────────────────────────────────────────
# _format_post_with_poll — extends _format_post with poll data
# ─────────────────────────────────────────────────────────────────────────────

def _format_post_with_poll(row, uid, db):
    p = _format_post(row, uid, db)
    if p and p.get('post_type') == 'poll':
        options = db.execute(
            'SELECT * FROM poll_options WHERE post_id=? ORDER BY id', (p['id'],)
        ).fetchall()
        total = sum(o['votes'] for o in options)
        user_vote = db.execute(
            'SELECT option_id FROM poll_votes WHERE post_id=? AND user_id=?',
            (p['id'], uid)
        ).fetchone()
        p['poll_options'] = [
            {'id': o['id'], 'label': o['label'], 'votes': o['votes'],
             'pct': round(o['votes']*100/total) if total else 0}
            for o in options
        ]
        p['poll_total']     = total
        p['poll_user_vote'] = user_vote['option_id'] if user_vote else None
        exp = p.get('poll_expires_at')
        if exp:
            try:
                from datetime import datetime as _dt2, timezone as _tz2
                p['poll_ended'] = _dt2.fromisoformat(exp.replace('Z','')) < _dt2.now(_tz2.utc)
            except Exception:
                p['poll_ended'] = False
        else:
            p['poll_ended'] = False
    return p


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
