"""
security.py — CSRF protection and rate limiting for DUYS Boost.

Issue 5: CSRF protection
    Every state-changing POST request (forms, AJAX) is protected with a
    per-session HMAC token. The token is:
      • Injected into every Jinja2 template via a context processor
      • Validated automatically on every POST by a before_request hook
      • Exempt for pure-API JSON endpoints (they rely on same-origin CORS
        + session cookie with SameSite=Lax, which already blocks CSRF)
      • Exempt for the OAuth callback (no session yet)

Issue 6: Rate limiting
    Uses Flask-Limiter with an in-memory store (suitable for single-worker
    deployments). For multi-worker Gunicorn, set REDIS_URL in .env and
    the limiter automatically switches to Redis storage.

    Rate limit tiers:
      STRICT  — auth and money routes (brute-force / fraud prevention)
      NORMAL  — social actions (spam prevention)
      RELAXED — read-heavy polling (bandwidth control)
      GENEROUS— heartbeats and lightweight API calls

Usage:
    from security import init_security, csrf_exempt, limiter
    init_security(app)              # call inside create_app()
    @limiter.limit("5 per minute")  # add to any route
    @csrf_exempt                    # skip CSRF for a specific route
"""

import hmac
import hashlib
import functools
import logging

from flask import (
    abort, g, jsonify, request, session,
    current_app, has_request_context,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

logger = logging.getLogger(__name__)

# ── Rate limiter (singleton, initialised in init_security) ───────────────────

def _rate_limit_key() -> str:
    """
    Key function for Flask-Limiter.
    Authenticated users are keyed by user_id to prevent IP-sharing bypass.
    Anonymous users are keyed by IP.
    """
    uid = session.get('user_id')
    if uid:
        return f'user:{uid}'
    return get_remote_address()


limiter = Limiter(
    key_func=_rate_limit_key,
    default_limits=[],          # no blanket limit — applied per-route
    storage_uri=None,           # set dynamically in init_security
    strategy='fixed-window',
)

# ── Per-route limits (applied as decorators in blueprints) ───────────────────

# Auth — brute-force / credential stuffing prevention
LIMIT_LOGIN    = '10 per minute; 30 per hour'
LIMIT_SIGNUP   = '5 per minute; 20 per hour'

# Money — fraud prevention
LIMIT_WITHDRAW = '3 per hour; 10 per day'
LIMIT_DEPOSIT  = '10 per hour'
LIMIT_TIP      = '20 per hour'
LIMIT_SUBSCRIBE= '20 per hour'

# Boost marketplace
LIMIT_TASK     = '30 per minute; 200 per hour'
LIMIT_AD       = '10 per minute'
LIMIT_BOOST    = '10 per minute'

# Social actions
LIMIT_POST     = '20 per minute; 100 per hour'
LIMIT_FOLLOW   = '60 per minute'
LIMIT_LIKE     = '120 per minute'
LIMIT_DM       = '30 per minute; 200 per hour'
LIMIT_UPLOAD   = '20 per hour'

# Lightweight polling / heartbeats
LIMIT_POLL     = '60 per minute'
LIMIT_HEARTBEAT= '2 per minute'

# ── CSRF protection ───────────────────────────────────────────────────────────

_CSRF_TOKEN_KEY  = '_csrf_token'
_CSRF_HEADER     = 'X-CSRF-Token'
_CSRF_FORM_FIELD = 'csrf_token'

# Routes that skip CSRF validation entirely
_CSRF_EXEMPT_ENDPOINTS: set[str] = set()


def csrf_exempt(view_func):
    """
    Decorator — mark a route as exempt from CSRF validation.
    Use for OAuth callbacks, webhooks, or pure JSON API endpoints
    that are already protected by SameSite=Lax cookies + CORS.
    """
    _CSRF_EXEMPT_ENDPOINTS.add(view_func.__name__)

    @functools.wraps(view_func)
    def wrapper(*args, **kwargs):
        return view_func(*args, **kwargs)
    return wrapper


def _generate_csrf_token() -> str:
    """Generate or retrieve the CSRF token for the current session."""
    if _CSRF_TOKEN_KEY not in session:
        import secrets as _sec
        session[_CSRF_TOKEN_KEY] = _sec.token_hex(32)
    return session[_CSRF_TOKEN_KEY]


def _validate_csrf_token() -> bool:
    """Return True if the request carries a valid CSRF token."""
    expected = session.get(_CSRF_TOKEN_KEY)
    if not expected:
        return False

    # Check header first (AJAX / fetch), then form field
    submitted = (
        request.headers.get(_CSRF_HEADER) or
        request.form.get(_CSRF_FORM_FIELD) or
        (request.get_json(silent=True) or {}).get('csrf_token')
    )
    if not submitted:
        return False

    return hmac.compare_digest(expected, submitted)


def _is_json_api_request() -> bool:
    """
    Pure-JSON API requests (Content-Type: application/json or /api/ paths)
    are protected by SameSite=Lax + same-origin policy instead of tokens.
    """
    if request.path.startswith('/api/'):
        return True
    ct = request.content_type or ''
    return 'application/json' in ct


# ── init_security: wire everything into the Flask app ────────────────────────

def init_security(app) -> None:
    """
    Call once inside create_app() after blueprints are registered.
    Attaches CSRF validation and rate limiting to the app.
    """
    import os

    # ── Rate limiter storage ──────────────────────────────────────────────────
    redis_url = os.environ.get('REDIS_URL', '')
    if redis_url:
        storage_uri = redis_url
        logger.info('Rate limiter using Redis: %s', redis_url[:30])
    else:
        storage_uri = 'memory://'
        logger.info('Rate limiter using in-memory store (single-worker only)')

    limiter.storage_uri = storage_uri
    limiter.init_app(app)

    # ── CSRF context processor ────────────────────────────────────────────────
    # Makes csrf_token() available in every Jinja2 template
    @app.context_processor
    def inject_csrf():
        return {'csrf_token': _generate_csrf_token}

    # ── CSRF before_request hook ──────────────────────────────────────────────
    @app.before_request
    def enforce_csrf():
        if request.method != 'POST':
            return  # only validate POST

        endpoint = request.endpoint or ''

        # Skip exempt endpoints (OAuth callbacks, webhooks, etc.)
        # Strip blueprint prefix: 'auth.google_auth_callback' → 'google_auth_callback'
        short_ep = endpoint.split('.')[-1] if '.' in endpoint else endpoint
        if short_ep in _CSRF_EXEMPT_ENDPOINTS:
            return

        # Skip pure-JSON API routes (protected by SameSite=Lax)
        if _is_json_api_request():
            return

        # Skip if user not logged in (login/signup validate by design)
        # But we DO check CSRF on login and signup — prevents pre-auth CSRF
        if not _validate_csrf_token():
            logger.warning(
                'CSRF validation failed: endpoint=%s ip=%s',
                endpoint, request.remote_addr
            )
            # All POST requests from JS fetch() expect JSON — never return HTML 403
            return jsonify({'success': False, 'error': 'Invalid CSRF token.'}), 403

    # ── Rate limit error handlers ─────────────────────────────────────────────
    @app.errorhandler(429)
    def ratelimit_handler(e):
        retry = getattr(e, 'retry_after', 60)
        # Always return JSON for mutating requests so fetch() callers get a proper error object
        if (request.method in ('POST', 'PUT', 'PATCH', 'DELETE')
                or request.path.startswith('/api/')
                or request.is_json):
            return jsonify({
                'success':     False,
                'error':       'Too many requests. Please slow down.',
                'retry_after': retry,
            }), 429
        return (
            f'<h2>Too many requests</h2>'
            f'<p>Please wait {retry} seconds before trying again.</p>',
            429,
        )
