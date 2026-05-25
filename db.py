"""
db.py — PostgreSQL connection layer.

Replaces the sqlite3 get_db() pattern.
Provides a psycopg2 connection that:
  - Is stored per-request on Flask's g object (same lifetime as before)
  - Uses a RealDictCursor so rows behave like dicts (same as sqlite3.Row)
  - Reads DATABASE_URL from the environment (Render, Heroku, Railway all set this)

Required env var:
    DATABASE_URL=postgresql://user:password@host:5432/dbname
    (Render/Heroku also accept POSTGRES_URL — we check both)
"""
import os
import psycopg2
import psycopg2.extras
from flask import g, current_app


def _get_dsn() -> str:
    dsn = (
        os.environ.get('DATABASE_URL') or
        os.environ.get('POSTGRES_URL') or
        current_app.config.get('DATABASE_URL', '')
    )
    if not dsn:
        raise RuntimeError(
            'DATABASE_URL environment variable is not set. '
            'Set it to your PostgreSQL connection string, e.g. '
            'postgresql://user:password@localhost:5432/duys_boost'
        )
    # Render/Heroku sometimes give postgres:// — psycopg2 needs postgresql://
    if dsn.startswith('postgres://'):
        dsn = 'postgresql://' + dsn[len('postgres://'):]
    return dsn


def get_db():
    """
    Return the per-request DB wrapper (opens connection on first call).

    Returns a _CursorWrapper so callers can use the familiar
    db.execute(sql, params).fetchone() / .fetchall() pattern,
    identical to the old sqlite3 interface.
    """
    if 'db' not in g:
        conn = psycopg2.connect(
            _get_dsn(),
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        g.db      = conn           # raw connection — for commit/close
        g.db_wrap = _CursorWrapper(conn)  # wrapped — for execute()
    return g.db_wrap


def close_db(_e=None):
    """Teardown hook — close connection at end of request."""
    conn = g.pop('db', None)
    g.pop('db_wrap', None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


class _CursorWrapper:
    """
    Thin shim so callers can still write db.execute(sql, params).fetchone()
    exactly as with sqlite3.  Returns a _ResultWrapper that mimics sqlite3's
    cursor/Row API (dict-style access by column name, .fetchone(), .fetchall()).
    """
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        # Use RealDictCursor so rows are dict-like, matching sqlite3.Row behaviour
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        return _ResultWrapper(cur)

    @property
    def lastrowid(self):
        """Return the last auto-generated id via currval of the sequence."""
        cur = self._conn.cursor()
        cur.execute('SELECT lastval()')
        return cur.fetchone()[0]

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


class _ResultWrapper:
    """Wraps a psycopg2 cursor to look like sqlite3's cursor."""
    def __init__(self, cursor):
        self._cur = cursor

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return _DictRow(row)

    def fetchall(self):
        return [_DictRow(r) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount


class _DictRow:
    """
    Wraps a RealDictRow so it supports both dict-style (row['col'])
    AND integer indexing (row[0]) — matching sqlite3.Row's full interface
    used throughout the app.
    """
    def __init__(self, data):
        self._d = dict(data)
        self._values_cache = None

    def _values_list(self):
        if self._values_cache is None:
            self._values_cache = list(self._d.values())
        return self._values_cache

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values_list()[key]
        return self._d[key]

    def __contains__(self, key):
        return key in self._d

    def __iter__(self):
        return iter(self._values_list())

    def __len__(self):
        return len(self._d)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def keys(self):
        return self._d.keys()

    def values(self):
        return self._d.values()

    def items(self):
        return self._d.items()

    def get(self, key, default=None):
        return self._d.get(key, default)

    def __iter__(self):
        return iter(self._d)

    def items(self):
        return self._d.items()

    def values(self):
        return self._d.values()


def init_app(app):
    """Register the teardown hook with a Flask app."""
    app.teardown_appcontext(close_db)
