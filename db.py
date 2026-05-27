"""
db.py — Hybrid SQLite + Cloudflare R2 architecture.

TWO databases per deployment:

  global.db  (on-disk + synced to R2 as "global.db")
    ├─ Social graph:  users, posts, follows, post_likes, bookmarks,
    │                 post_views, hashtags, post_hashtags
    ├─ Polls:         poll_options, poll_votes
    ├─ Marketplace:   ads, task_completions, post_boosts, boost_engagements
    ├─ Channels:      channels, channel_members, channel_posts
    ├─ Groups:        groups, group_members, group_messages
    ├─ Stories:       stories
    ├─ Admin:         reports, user_bans, platform_reviews,
    │                 admin_audit_log
    └─ Discovery:     search_history

  users/{user_id}.db  (downloaded from R2 on each authenticated request,
                        uploaded back on teardown)
    ├─ Wallet:        transactions, withdrawals, crypto_deposits
    ├─ Inbox:         conversations, messages
    ├─ Notifications: notifications
    └─ Subscriptions: subscription_tiers, subscriptions, tips

This split means:
  • Feed, profiles, explore, channels, groups → read from global.db (fast)
  • Wallet, DMs, notifications → isolated per-user (privacy + no lock contention)
  • Admin queries → always hit global.db (all users visible)

Flask helpers:
  get_db()       → global.db connection (social data)
  get_user_db()  → personal DB for the logged-in user (wallet/inbox)
"""

import os
import sqlite3
import logging
import threading

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from flask import g, current_app

logger = logging.getLogger(__name__)

# ── R2 client (singleton) ─────────────────────────────────────────────────────

_r2_client      = None
_r2_client_lock = threading.Lock()


def _get_r2():
    global _r2_client
    if _r2_client is not None:
        return _r2_client
    with _r2_client_lock:
        if _r2_client is None:
            endpoint = os.environ.get('R2_ENDPOINT_URL', '').strip()
            key_id   = os.environ.get('R2_ACCESS_KEY_ID', '').strip()
            secret   = os.environ.get('R2_SECRET_ACCESS_KEY', '').strip()
            if not all([endpoint, key_id, secret]):
                raise RuntimeError(
                    'R2 not configured. Set R2_ENDPOINT_URL, '
                    'R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY.'
                )
            _r2_client = boto3.client(
                's3',
                endpoint_url=endpoint,
                aws_access_key_id=key_id,
                aws_secret_access_key=secret,
                config=Config(signature_version='s3v4'),
                region_name='auto',
            )
    return _r2_client


def _db_bucket():
    b = os.environ.get('R2_DB_BUCKET_NAME', '').strip()
    if not b:
        raise RuntimeError('R2_DB_BUCKET_NAME is not set.')
    return b


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA A — Global (shared social data)
# ─────────────────────────────────────────────────────────────────────────────

GLOBAL_SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA cache_size   = -16000;

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT UNIQUE NOT NULL,
    email                TEXT UNIQUE NOT NULL,
    password             TEXT,
    display_name         TEXT,
    bio                  TEXT,
    avatar_url           TEXT,
    banner_url           TEXT,
    website              TEXT,
    location             TEXT,
    is_verified          INTEGER DEFAULT 0,
    is_admin             INTEGER DEFAULT 0,
    is_banned            INTEGER DEFAULT 0,
    ban_reason           TEXT,
    balance              REAL    DEFAULT 0,
    follower_count       INTEGER DEFAULT 0,
    following_count      INTEGER DEFAULT 0,
    post_count           INTEGER DEFAULT 0,
    subscriber_count     INTEGER DEFAULT 0,
    total_tips_received  REAL    DEFAULT 0,
    total_tips_sent      REAL    DEFAULT 0,
    unread_dm_count      INTEGER DEFAULT 0,
    unread_group_count   INTEGER DEFAULT 0,
    search_count         INTEGER DEFAULT 0,
    referral_code        TEXT UNIQUE,
    referred_by          INTEGER,
    referral_bonus_awarded INTEGER DEFAULT 0,
    reset_token          TEXT,
    reset_expires        INTEGER,
    theme                TEXT    DEFAULT 'dark',
    crypto_network       TEXT,
    crypto_address       TEXT,
    crypto_name          TEXT,
    online_at            TEXT,
    show_online          INTEGER DEFAULT 1,
    allow_post_saves     INTEGER DEFAULT 1,
    created_at           TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS posts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL,
    body               TEXT,
    reply_to_id        INTEGER,
    repost_of_id       INTEGER,
    quote_body         TEXT,
    media_url          TEXT,
    media_mime         TEXT,
    like_count         INTEGER DEFAULT 0,
    reply_count        INTEGER DEFAULT 0,
    repost_count       INTEGER DEFAULT 0,
    view_count         INTEGER DEFAULT 0,
    score              REAL    DEFAULT 0,
    is_boosted         INTEGER DEFAULT 0,
    is_subscriber_only INTEGER DEFAULT 0,
    hashtags_cached    TEXT,
    post_type          TEXT    DEFAULT 'post',
    poll_expires_at    TEXT,
    edited_at          TEXT,
    created_at         TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS follows (
    follower_id  INTEGER NOT NULL,
    following_id INTEGER NOT NULL,
    created_at   TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (follower_id, following_id)
);
CREATE TABLE IF NOT EXISTS post_likes (
    user_id    INTEGER NOT NULL,
    post_id    INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, post_id)
);
CREATE TABLE IF NOT EXISTS bookmarks (
    user_id    INTEGER NOT NULL,
    post_id    INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, post_id)
);
CREATE TABLE IF NOT EXISTS post_views (
    post_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (post_id, user_id)
);
CREATE TABLE IF NOT EXISTS hashtags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS post_hashtags (
    post_id    INTEGER NOT NULL,
    hashtag_id INTEGER NOT NULL,
    PRIMARY KEY (post_id, hashtag_id)
);
CREATE TABLE IF NOT EXISTS poll_options (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    label   TEXT NOT NULL,
    votes   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS poll_votes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id   INTEGER NOT NULL,
    option_id INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    voted_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (post_id, user_id)
);
CREATE TABLE IF NOT EXISTS ads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    title            TEXT    NOT NULL,
    platform         TEXT    NOT NULL,
    target_url       TEXT    NOT NULL,
    task_type        TEXT    NOT NULL,
    reward_per_task  REAL    DEFAULT 0.05,
    budget           REAL    NOT NULL,
    budget_spent     REAL    DEFAULT 0,
    followers_target INTEGER DEFAULT 0,
    followers_gained INTEGER DEFAULT 0,
    status           TEXT    DEFAULT 'active',
    created_at       TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS task_completions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id        INTEGER NOT NULL,
    worker_id    INTEGER NOT NULL,
    proof_link   TEXT    NOT NULL,
    status       TEXT    DEFAULT 'approved',
    reward       REAL,
    submitted_at TEXT    DEFAULT (datetime('now')),
    reviewed_at  TEXT
);
CREATE TABLE IF NOT EXISTS post_boosts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id           INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    budget            REAL    NOT NULL,
    budget_spent      REAL    DEFAULT 0,
    reward_per_engage REAL    DEFAULT 0.05,
    engage_type       TEXT    DEFAULT 'like',
    target_count      INTEGER DEFAULT 0,
    engaged_count     INTEGER DEFAULT 0,
    status            TEXT    DEFAULT 'active',
    created_at        TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS boost_engagements (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    boost_id  INTEGER NOT NULL,
    post_id   INTEGER NOT NULL,
    worker_id INTEGER NOT NULL,
    reward    REAL,
    earned_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS channels (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    slug         TEXT NOT NULL UNIQUE,
    description  TEXT,
    avatar_url   TEXT,
    owner_id     INTEGER NOT NULL,
    is_public    INTEGER DEFAULT 1,
    member_count INTEGER DEFAULT 0,
    post_count   INTEGER DEFAULT 0,
    created_at   TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS channel_members (
    channel_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    role       TEXT DEFAULT 'member',
    joined_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (channel_id, user_id)
);
CREATE TABLE IF NOT EXISTS channel_posts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    post_id    INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (channel_id, post_id)
);
CREATE TABLE IF NOT EXISTS groups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    slug         TEXT NOT NULL UNIQUE,
    description  TEXT,
    avatar_url   TEXT,
    owner_id     INTEGER NOT NULL,
    is_public    INTEGER DEFAULT 1,
    member_count INTEGER DEFAULT 0,
    created_at   TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS group_members (
    group_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    role         TEXT DEFAULT 'member',
    joined_at    TEXT DEFAULT (datetime('now')),
    last_read_at TEXT,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS group_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL,
    sender_id   INTEGER NOT NULL,
    body        TEXT,
    msg_type    TEXT DEFAULT 'text',
    file_url    TEXT,
    file_name   TEXT,
    file_mime   TEXT,
    reply_to_id INTEGER,
    deleted_at  TEXT,
    edited_at   TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS stories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    media_url  TEXT    NOT NULL,
    media_mime TEXT    NOT NULL DEFAULT 'image/jpeg',
    caption    TEXT,
    viewed_by  TEXT    DEFAULT '[]',
    expires_at TEXT    NOT NULL DEFAULT (datetime('now', '+1 day')),
    created_at TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter_id  INTEGER NOT NULL,
    target_type  TEXT    NOT NULL,
    target_id    INTEGER NOT NULL,
    reason       TEXT    NOT NULL,
    details      TEXT,
    status       TEXT    DEFAULT 'open',
    reviewed_by  INTEGER,
    reviewed_at  TEXT,
    action_taken TEXT,
    created_at   TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS user_bans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL UNIQUE,
    banned_by  INTEGER NOT NULL,
    reason     TEXT    NOT NULL,
    expires_at TEXT,
    is_active  INTEGER DEFAULT 1,
    created_at TEXT    DEFAULT (datetime('now')),
    lifted_at  TEXT,
    lifted_by  INTEGER
);
CREATE TABLE IF NOT EXISTS platform_reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    rating      INTEGER NOT NULL,
    title       TEXT,
    body        TEXT,
    status      TEXT    DEFAULT 'published',
    is_featured INTEGER DEFAULT 0,
    admin_reply TEXT,
    created_at  TEXT    DEFAULT (datetime('now')),
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL,
    action      TEXT    NOT NULL,
    target_type TEXT,
    target_id   INTEGER,
    details     TEXT,
    ip_address  TEXT,
    created_at  TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS search_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    query       TEXT    NOT NULL,
    result_type TEXT    DEFAULT 'mixed',
    created_at  TEXT    DEFAULT (datetime('now'))
);

-- Global indexes
CREATE INDEX IF NOT EXISTS idx_posts_user      ON posts(user_id);
CREATE INDEX IF NOT EXISTS idx_posts_created   ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_score     ON posts(score);
CREATE INDEX IF NOT EXISTS idx_posts_reply     ON posts(reply_to_id);
CREATE INDEX IF NOT EXISTS idx_follows_er      ON follows(follower_id);
CREATE INDEX IF NOT EXISTS idx_follows_ing     ON follows(following_id);
CREATE INDEX IF NOT EXISTS idx_likes_post      ON post_likes(post_id);
CREATE INDEX IF NOT EXISTS idx_likes_user      ON post_likes(user_id);
CREATE INDEX IF NOT EXISTS idx_bm_user         ON bookmarks(user_id);
CREATE INDEX IF NOT EXISTS idx_pv_post         ON post_views(post_id);
CREATE INDEX IF NOT EXISTS idx_ph_post         ON post_hashtags(post_id);
CREATE INDEX IF NOT EXISTS idx_ph_hashtag      ON post_hashtags(hashtag_id);
CREATE INDEX IF NOT EXISTS idx_ads_user        ON ads(user_id);
CREATE INDEX IF NOT EXISTS idx_ads_status      ON ads(status);
CREATE INDEX IF NOT EXISTS idx_tc_worker       ON task_completions(worker_id);
CREATE INDEX IF NOT EXISTS idx_tc_ad           ON task_completions(ad_id);
CREATE INDEX IF NOT EXISTS idx_pb_post         ON post_boosts(post_id);
CREATE INDEX IF NOT EXISTS idx_be_worker       ON boost_engagements(worker_id);
CREATE INDEX IF NOT EXISTS idx_ch_owner        ON channels(owner_id);
CREATE INDEX IF NOT EXISTS idx_chm_user        ON channel_members(user_id);
CREATE INDEX IF NOT EXISTS idx_grp_owner       ON groups(owner_id);
CREATE INDEX IF NOT EXISTS idx_grpm_user       ON group_members(user_id);
CREATE INDEX IF NOT EXISTS idx_grpms_group     ON group_messages(group_id);
CREATE INDEX IF NOT EXISTS idx_stories_user    ON stories(user_id);
CREATE INDEX IF NOT EXISTS idx_stories_exp     ON stories(expires_at);
CREATE INDEX IF NOT EXISTS idx_reports_status  ON reports(status);
CREATE INDEX IF NOT EXISTS idx_bans_user       ON user_bans(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_admin     ON admin_audit_log(admin_id);
CREATE INDEX IF NOT EXISTS idx_sh_user         ON search_history(user_id);
"""

# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA B — Personal (per-user private data)
# ─────────────────────────────────────────────────────────────────────────────

PERSONAL_SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA cache_size   = -4000;

-- Wallet
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    type        TEXT,
    amount      REAL,
    description TEXT,
    status      TEXT DEFAULT 'completed',
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS withdrawals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    amount         REAL,
    method         TEXT,
    account        TEXT,
    network        TEXT,
    status         TEXT DEFAULT 'pending',
    tx_hash        TEXT,
    failure_reason TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    processed_at   TEXT
);
CREATE TABLE IF NOT EXISTS crypto_deposits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    network      TEXT    NOT NULL,
    tx_hash      TEXT UNIQUE NOT NULL,
    amount       REAL    NOT NULL,
    status       TEXT    DEFAULT 'pending',
    confirmed_at TEXT,
    created_at   TEXT    DEFAULT (datetime('now'))
);

-- Inbox
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    message    TEXT,
    read       INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a      INTEGER NOT NULL,
    user_b      INTEGER NOT NULL,
    last_msg_at TEXT    DEFAULT (datetime('now')),
    UNIQUE (user_a, user_b)
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    sender_id       INTEGER NOT NULL,
    body            TEXT,
    msg_type        TEXT    DEFAULT 'text',
    file_url        TEXT,
    file_name       TEXT,
    file_mime       TEXT,
    is_read         INTEGER DEFAULT 0,
    edited_at       TEXT,
    reply_to_id     INTEGER,
    reactions       TEXT,
    is_pinned       INTEGER DEFAULT 0,
    deleted_at      TEXT,
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- Subscriptions & tips
CREATE TABLE IF NOT EXISTS subscription_tiers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id  INTEGER NOT NULL UNIQUE,
    price_usd   REAL    NOT NULL DEFAULT 1.0,
    title       TEXT    NOT NULL DEFAULT 'Supporter',
    description TEXT,
    perks       TEXT,
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL,
    creator_id    INTEGER NOT NULL,
    tier_id       INTEGER NOT NULL,
    status        TEXT    DEFAULT 'active',
    started_at    TEXT    DEFAULT (datetime('now')),
    expires_at    TEXT,
    UNIQUE (subscriber_id, creator_id)
);
CREATE TABLE IF NOT EXISTS tips (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER NOT NULL,
    to_user_id   INTEGER NOT NULL,
    post_id      INTEGER,
    amount       REAL    NOT NULL,
    message      TEXT,
    created_at   TEXT    DEFAULT (datetime('now'))
);

-- Personal indexes
CREATE INDEX IF NOT EXISTS idx_tx_user     ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_wdr_status  ON withdrawals(status);
CREATE INDEX IF NOT EXISTS idx_notif_user  ON notifications(user_id, read);
CREATE INDEX IF NOT EXISTS idx_conv_a      ON conversations(user_a);
CREATE INDEX IF NOT EXISTS idx_conv_b      ON conversations(user_b);
CREATE INDEX IF NOT EXISTS idx_conv_last   ON conversations(last_msg_at);
CREATE INDEX IF NOT EXISTS idx_msg_conv    ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_msg_sender  ON messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_sub_creator ON subscriptions(creator_id);
CREATE INDEX IF NOT EXISTS idx_tips_to     ON tips(to_user_id);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Global DB — R2 sync
# ─────────────────────────────────────────────────────────────────────────────

_global_synced      = False
_global_sync_lock   = threading.Lock()
_migrations_done    = False   # run once per worker boot


def _global_db_path() -> str:
    return os.path.join(current_app.root_path, 'global.db')


def _sync_global_from_r2() -> None:
    """Download global.db from R2 once per worker boot."""
    global _global_synced
    with _global_sync_lock:
        if _global_synced:
            return
        path   = _global_db_path()
        bucket = os.environ.get('R2_DB_BUCKET_NAME', '').strip()
        if bucket:
            try:
                _get_r2().download_file(bucket, 'global.db', path)
                size_kb = os.path.getsize(path) // 1024
                logger.info('global.db downloaded from R2 (%d KB)', size_kb)
            except ClientError as e:
                code = e.response.get('Error', {}).get('Code', '')
                if code in ('404', 'NoSuchKey'):
                    logger.info('global.db not in R2 yet — creating fresh')
                else:
                    logger.error('R2 download error for global.db: %s', e)
            except Exception as e:
                logger.warning('global.db R2 download skipped: %s', e)
        _global_synced = True


def _sync_global_to_r2() -> None:
    """Upload global.db to R2 after each request."""
    path   = _global_db_path()
    bucket = os.environ.get('R2_DB_BUCKET_NAME', '').strip()
    if not bucket or not os.path.exists(path):
        return
    try:
        _get_r2().upload_file(
            path, bucket, 'global.db',
            ExtraArgs={'ContentType': 'application/octet-stream'},
        )
        logger.debug('global.db synced to R2')
    except Exception as e:
        logger.warning('global.db R2 upload failed (data safe on disk): %s', e)


def _open_global_db() -> sqlite3.Connection:
    global _migrations_done
    path = _global_db_path()
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(GLOBAL_SCHEMA)   # CREATE TABLE IF NOT EXISTS (new tables)
    conn.commit()
    if not _migrations_done:
        run_schema_migrations(conn)     # ALTER TABLE ADD COLUMN (new columns, once)
        _migrations_done = True
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# Personal DB — per-user R2 download/upload
# ─────────────────────────────────────────────────────────────────────────────

def _personal_db_key(uid: int) -> str:
    return f'users/{uid}.db'


def _personal_db_path(uid: int) -> str:
    tmp = '/tmp'
    os.makedirs(tmp, exist_ok=True)
    return os.path.join(tmp, f'user_{uid}.db')


def _download_personal_db(uid: int) -> str:
    """Download personal DB from R2. Creates empty file if new user."""
    path   = _personal_db_path(uid)
    bucket = os.environ.get('R2_DB_BUCKET_NAME', '').strip()
    if bucket:
        try:
            _get_r2().download_file(bucket, _personal_db_key(uid), path)
            logger.debug('personal DB downloaded for uid=%d', uid)
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', '')
            if code in ('404', 'NoSuchKey'):
                logger.debug('New personal DB for uid=%d', uid)
            else:
                logger.error('R2 personal DB download error uid=%d: %s', uid, e)
    return path


def _upload_personal_db(uid: int, path: str) -> None:
    """Upload personal DB back to R2, then remove local copy."""
    bucket = os.environ.get('R2_DB_BUCKET_NAME', '').strip()
    if bucket and os.path.exists(path):
        try:
            _get_r2().upload_file(
                path, bucket, _personal_db_key(uid),
                ExtraArgs={'ContentType': 'application/octet-stream'},
            )
            logger.debug('personal DB uploaded for uid=%d', uid)
        except Exception as e:
            logger.warning('personal DB upload failed uid=%d: %s', uid, e)
    try:
        os.remove(path)
    except OSError:
        pass


def _open_personal_db(uid: int) -> tuple:
    """Download, open, and schema-init personal DB. Returns (conn, path)."""
    path = _download_personal_db(uid)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(PERSONAL_SCHEMA)
    conn.commit()
    return conn, path


# ─────────────────────────────────────────────────────────────────────────────
# Flask g helpers
# ─────────────────────────────────────────────────────────────────────────────


def run_schema_migrations(conn: sqlite3.Connection) -> None:
    """
    Idempotent schema migration — adds any columns that exist in the
    current GLOBAL_SCHEMA but are missing from the live database.

    This handles the case where Render's persistent DB was created with
    an older schema version. SQLite does not support IF NOT EXISTS for
    ALTER TABLE ADD COLUMN, so we catch the OperationalError.

    Called automatically by _open_global_db() on every startup.
    """
    # Full list of (table, column, column_definition) to ensure exist
    REQUIRED_COLUMNS = [
        # users — columns added across versions
        ('users', 'is_banned',             'INTEGER DEFAULT 0'),
        ('users', 'ban_reason',            'TEXT'),
        ('users', 'display_name',          'TEXT'),
        ('users', 'bio',                   'TEXT'),
        ('users', 'avatar_url',            'TEXT'),
        ('users', 'banner_url',            'TEXT'),
        ('users', 'website',               'TEXT'),
        ('users', 'location',              'TEXT'),
        ('users', 'is_verified',           'INTEGER DEFAULT 0'),
        ('users', 'verified_tier',         "TEXT DEFAULT 'blue'"),
        ('users', 'follower_count',        'INTEGER DEFAULT 0'),
        ('users', 'following_count',       'INTEGER DEFAULT 0'),
        ('users', 'post_count',            'INTEGER DEFAULT 0'),
        ('users', 'subscriber_count',      'INTEGER DEFAULT 0'),
        ('users', 'total_tips_received',   'REAL DEFAULT 0'),
        ('users', 'total_tips_sent',       'REAL DEFAULT 0'),
        ('users', 'unread_dm_count',       'INTEGER DEFAULT 0'),
        ('users', 'unread_group_count',    'INTEGER DEFAULT 0'),
        ('users', 'search_count',          'INTEGER DEFAULT 0'),
        ('users', 'referral_code',         'TEXT'),
        ('users', 'referred_by',           'INTEGER'),
        ('users', 'referral_bonus_awarded','INTEGER DEFAULT 0'),
        ('users', 'reset_token',           'TEXT'),
        ('users', 'reset_expires',         'INTEGER'),
        ('users', 'theme',                 "TEXT DEFAULT 'dark'"),
        ('users', 'crypto_network',        'TEXT'),
        ('users', 'crypto_address',        'TEXT'),
        ('users', 'crypto_name',           'TEXT'),
        ('users', 'online_at',             'TEXT'),
        ('users', 'show_online',           'INTEGER DEFAULT 1'),
        ('users', 'allow_post_saves',      'INTEGER DEFAULT 1'),
        ('users', 'username_changes',      'INTEGER DEFAULT 0'),
        ('users', 'username_last_changed', 'TEXT'),
        # posts
        ('posts', 'media_mime',            'TEXT'),
        ('posts', 'hashtags_cached',       'TEXT'),
        ('posts', 'quote_body',            'TEXT'),
        ('posts', 'view_count',            'INTEGER DEFAULT 0'),
        ('posts', 'score',                 'REAL DEFAULT 0'),
        ('posts', 'is_boosted',            'INTEGER DEFAULT 0'),
        ('posts', 'is_subscriber_only',    'INTEGER DEFAULT 0'),
        ('posts', 'edited_at',             'TEXT'),
        ('posts', 'repost_count',          'INTEGER DEFAULT 0'),
        # stories
        ('stories', 'media_mime',          "TEXT NOT NULL DEFAULT 'image/jpeg'"),
        ('stories', 'caption',             'TEXT'),
        ('stories', 'reactions_data',     'TEXT DEFAULT "{}"'),
        ('stories', 'viewed_by',           "TEXT DEFAULT '[]'"),
        # ads
        ('ads', 'followers_target',        'INTEGER DEFAULT 0'),
        ('ads', 'followers_gained',        'INTEGER DEFAULT 0'),
        # channels / groups
        ('channels', 'avatar_url',         'TEXT'),
        ('groups',   'avatar_url',         'TEXT'),
        # reports
        ('reports', 'action_taken',        'TEXT'),
        # platform_reviews
        ('platform_reviews', 'admin_reply','TEXT'),
        ('platform_reviews', 'updated_at', 'TEXT'),
        ('platform_reviews', 'is_featured','INTEGER DEFAULT 0'),
        ('messages', 'view_once',          'INTEGER DEFAULT 0'),
        ('messages', 'view_once_opened',   'INTEGER DEFAULT 0'),
        # post_boosts — targeting columns
        ('post_boosts', 'target_location',  'TEXT'),
        ('post_boosts', 'target_age_min',   'INTEGER'),
        ('post_boosts', 'target_age_max',   'INTEGER'),
        ('post_boosts', 'landing_url',      'TEXT'),
        ('post_boosts', 'cta_label',        "TEXT DEFAULT 'Learn More'"),
        ('post_boosts', 'duration_days',    'INTEGER DEFAULT 7'),
        ('post_boosts', 'starts_at',        'TEXT'),
        ('post_boosts', 'ends_at',          'TEXT'),
        # channels/groups verification
        ('channels', 'is_verified', "INTEGER DEFAULT 0"),
        ('channels', 'verified_tier', "TEXT DEFAULT 'gold'"),
        ('groups', 'is_verified', "INTEGER DEFAULT 0"),
        ('groups', 'verified_tier', "TEXT DEFAULT 'gold'"),
    ]

    cur = conn.cursor()
    migrated = 0
    for table, column, definition in REQUIRED_COLUMNS:
        try:
            cur.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
            migrated += 1
            logger.info('Migration: added %s.%s', table, column)
        except sqlite3.OperationalError:
            pass  # Column already exists — that's fine

    if migrated:
        conn.commit()
        logger.info('Schema migration complete: %d column(s) added', migrated)

    # ── BACKFILL_REFERRAL_CODES ──────────────────────────────────────────────
    # Update users whose referral_code looks like a random hex token (10 chars)
    # to use their username instead. Idempotent.
    try:
        rows = conn.execute(
            "SELECT id, username, referral_code FROM users "
            "WHERE referral_code IS NOT NULL"
        ).fetchall()
        updated = 0
        for r in rows:
            rc = r['referral_code']
            # token_hex(5) produces a 10-char hex string
            if rc and len(rc) == 10 and all(c in '0123456789abcdef' for c in rc):
                # Check username isn't already used as someone else's referral
                conflict = conn.execute(
                    'SELECT id FROM users WHERE referral_code=? AND id!=?',
                    (r['username'], r['id'])
                ).fetchone()
                if not conflict:
                    conn.execute('UPDATE users SET referral_code=? WHERE id=?',
                                (r['username'], r['id']))
                    updated += 1
        if updated:
            conn.commit()
            logger.info('Backfilled %d referral codes to usernames', updated)
    except Exception as _e:
        logger.warning('Referral backfill skipped: %s', _e)


def get_db() -> sqlite3.Connection:
    """
    Return the global DB connection for this request.
    Used for all social/public data: feeds, profiles, posts, channels, groups.
    """
    if 'gdb' in g:
        return g.gdb

    if not _global_synced:
        _sync_global_from_r2()

    conn     = _open_global_db()
    g.gdb    = conn
    return conn


def get_user_db() -> sqlite3.Connection:
    """
    Return the personal DB connection for the logged-in user.
    Used for wallet, DMs, notifications, subscriptions.
    Raises RuntimeError if no user is logged in.
    """
    if 'udb' in g:
        return g.udb

    from flask import session as _s
    uid = _s.get('user_id')
    if not uid:
        raise RuntimeError('get_user_db() called without a logged-in user.')

    conn, path = _open_personal_db(uid)
    g.udb      = conn
    g.udb_uid  = uid
    g.udb_path = path
    return conn


def close_db(_e=None) -> None:
    """Teardown: commit+close both DBs, sync global to R2, upload personal."""
    # ── Personal DB ──────────────────────────────────────────────────────────
    udb  = g.pop('udb',      None)
    uid  = g.pop('udb_uid',  None)
    path = g.pop('udb_path', None)
    if udb:
        try:
            udb.commit()
            udb.close()
        except Exception as e:
            logger.warning('personal DB close error: %s', e)
        if uid and path:
            _upload_personal_db(uid, path)

    # ── Global DB ─────────────────────────────────────────────────────────────
    gdb = g.pop('gdb', None)
    if gdb:
        try:
            gdb.commit()
            gdb.close()
        except Exception as e:
            logger.warning('global DB close error: %s', e)
        _sync_global_to_r2()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)


# ─────────────────────────────────────────────────────────────────────────────
# DB maintenance (suggestion #4 — cleanup old data)
# ─────────────────────────────────────────────────────────────────────────────

def run_maintenance(db: sqlite3.Connection) -> dict:
    """
    Prune stale rows from global.db to keep it lean.
    Called by the admin blueprint or the stories cleanup thread.
    Returns counts of deleted rows.
    """
    results = {}

    cur = db.cursor()

    # Expired stories
    cur.execute("DELETE FROM stories WHERE expires_at < datetime('now')")
    results['stories'] = cur.rowcount

    # Old post_views (keep 7 days — only used for view-count dedup)
    cur.execute("DELETE FROM post_views WHERE created_at < datetime('now', '-7 days')")
    results['post_views'] = cur.rowcount

    # Old search history (keep 14 days)
    cur.execute("DELETE FROM search_history WHERE created_at < datetime('now', '-14 days')")
    results['search_history'] = cur.rowcount

    # Resolved/old reports (keep 90 days)
    cur.execute(
        "DELETE FROM reports WHERE status != 'open' "
        "AND created_at < datetime('now', '-90 days')"
    )
    results['reports'] = cur.rowcount

    # Old audit log entries (keep 180 days)
    cur.execute(
        "DELETE FROM admin_audit_log WHERE created_at < datetime('now', '-180 days')"
    )
    results['audit_log'] = cur.rowcount

    db.commit()

    # VACUUM to reclaim freed pages (runs quickly on small DBs)
    try:
        db.execute('VACUUM')
    except Exception:
        pass

    total = sum(results.values())
    logger.info('DB maintenance: deleted %d rows total — %s', total, results)
    return results
