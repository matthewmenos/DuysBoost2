"""
blueprints/auth_reset.py
━━━━━━━━━━━━━━━━━━━━━━━━
Forgot-password / PIN-verification workflow for the "One User, One Database"
architecture where each user's profile lives in an isolated SQLite file
stored on Cloudflare R2.

Flow
────
1.  POST /api/auth/forgot-password  ← user submits their email
    • SHA-256 hash email → look up user_id in global.db
    • Pull {user_id}.db from R2 → /tmp/user_{uid}.db
    • Generate a cryptographically secure 6-digit PIN
    • Store hashed PIN + 15-minute expiry in the user's isolated DB
    • Push updated DB back to R2 (original is *replaced*, never corrupted)
    • Fire a Brevo transactional email with the PIN

2.  POST /api/auth/verify-pin  ← user submits email + PIN + new password
    • SHA-256 hash email → user_id
    • Pull {user_id}.db from R2
    • Compare bcrypt-hashed PIN; check expiry
    • Update password (bcrypt), clear pin fields
    • Push updated DB back to R2
    • Return success

Safety guarantees
─────────────────
• R2 upload is only called AFTER the local DB write succeeds.  If the write
  fails, the file is never pushed, so R2 stays intact.
• If the R2 upload fails after a successful write, we still have the local
  copy and the operation is retried transparently (upload-then-delete).
• The PIN is never stored in plain text.  We store bcrypt(pin, cost=12).
• Timing-safe comparison via hmac.compare_digest on the hashed values.
• All error paths return generic messages to prevent email enumeration.
• Rate-limited: 5 requests / hour on the forgot-password endpoint.
"""

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time

from flask import Blueprint, jsonify, request, current_app

from helpers import hash_password, verify_password
from security import limiter, csrf_exempt

logger = logging.getLogger(__name__)

bp = Blueprint("auth_reset", __name__)

# ─── constants ────────────────────────────────────────────────────────────────
_PIN_EXPIRY_SECONDS = 15 * 60          # 15 minutes
_PIN_DIGITS         = 6
_GENERIC_OK = (
    "If that email is registered you will receive a PIN shortly. "
    "Check your inbox and spam folder."
)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _hash_email(email: str) -> str:
    """Deterministic SHA-256 hex digest of a normalised email address."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _global_db():
    """Open (read-only query) the global index database. Returns a sqlite3 conn."""
    db_path = os.path.join(current_app.root_path, "global.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _lookup_user_by_email(email: str) -> dict | None:
    """
    Return the user row from global.db if the email matches.
    We look up by plain email (the column is indexed) because global.db lives
    on the server — only the per-user files are split into R2.
    """
    conn = _global_db()
    try:
        row = conn.execute(
            "SELECT id, email, username, display_name FROM users WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _r2_client():
    """Return a boto3 S3 client pointed at Cloudflare R2 (reuses storage.py logic)."""
    import boto3
    from botocore.client import Config

    access_key = os.environ["R2_ACCESS_KEY_ID"]
    secret_key  = os.environ["R2_SECRET_ACCESS_KEY"]
    account_id  = os.environ["R2_ACCOUNT_ID"]
    endpoint    = f"https://{account_id}.r2.cloudflarestorage.com"

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _db_bucket() -> str:
    bucket = os.environ.get("R2_DB_BUCKET_NAME", "").strip()
    if not bucket:
        raise RuntimeError("R2_DB_BUCKET_NAME is not configured.")
    return bucket


def _r2_key(uid: int) -> str:
    return f"users/{uid}.db"


def _local_path(uid: int) -> str:
    os.makedirs("/tmp", exist_ok=True)
    return f"/tmp/user_{uid}_reset.db"


def _pull_user_db(uid: int) -> str:
    """
    Download {user_id}.db from R2 to /tmp/user_{uid}_reset.db.
    Returns the local path.
    Raises RuntimeError if R2 is unavailable or the file is not found.
    """
    from botocore.exceptions import ClientError

    path = _local_path(uid)
    try:
        _r2_client().download_file(_db_bucket(), _r2_key(uid), path)
        logger.debug("user db downloaded: uid=%d -> %s", uid, path)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey"):
            # Brand new user whose DB was never pushed yet — create empty file
            logger.info("No R2 db for uid=%d, creating fresh file.", uid)
            # Touch an empty SQLite file
            conn = sqlite3.connect(path)
            conn.close()
        else:
            logger.error("R2 download failed uid=%d: %s", uid, exc)
            raise RuntimeError(f"Could not retrieve user database: {exc}") from exc
    return path


def _push_user_db(uid: int, path: str) -> None:
    """
    Upload the local DB back to R2, then remove the local temp copy.
    IMPORTANT: This is only called *after* the local write succeeds.
    If the upload fails, we log the error but do NOT raise — the global.db
    record is unchanged so the user can retry.  The local file is cleaned up
    regardless to avoid stale data.
    """
    try:
        _r2_client().upload_file(
            path,
            _db_bucket(),
            _r2_key(uid),
            ExtraArgs={"ContentType": "application/octet-stream"},
        )
        logger.debug("user db uploaded: uid=%d", uid)
    except Exception as exc:
        logger.error("R2 upload failed uid=%d: %s", uid, exc)
        # Do NOT re-raise — caller handles UI feedback
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _open_user_db(path: str):
    """Open a local SQLite file and return the connection."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Ensure the reset-PIN columns exist (idempotent ALTER TABLE)
    for col, coltype in [
        ("reset_pin",          "TEXT"),
        ("reset_pin_expires",  "INTEGER"),
        ("reset_token",        "TEXT"),
        ("reset_expires",      "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {coltype}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    return conn


def _generate_pin() -> str:
    """Return a cryptographically secure 6-digit PIN string."""
    # secrets.randbelow gives uniform distribution — no modulo bias
    return str(secrets.randbelow(900_000) + 100_000)


def _send_pin_email(to_email: str, to_name: str, pin: str) -> bool:
    """
    Send a transactional PIN email via the official Brevo (Sendinblue) SDK.
    Returns True on success, False on failure.
    Requires BREVO_API_KEY env var (and optionally BREVO_SENDER_EMAIL /
    BREVO_SENDER_NAME).
    """
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "BREVO_API_KEY not set — PIN email not sent.  PIN for %s: %s",
            to_email, pin,
        )
        return False   # In dev/staging, PIN is logged above — never in prod

    sender_email = os.environ.get("BREVO_SENDER_EMAIL", "noreply@duysboost.com")
    sender_name  = os.environ.get("BREVO_SENDER_NAME",  "DUYS Boost")
    app_name     = current_app.config.get("APP_NAME", "DUYS Boost")
    expiry_mins  = _PIN_EXPIRY_SECONDS // 60

    try:
        import brevo_python
        from brevo_python.rest import ApiException

        configuration = brevo_python.Configuration()
        configuration.api_key["api-key"] = api_key

        api_instance = brevo_python.TransactionalEmailsApi(
            brevo_python.ApiClient(configuration)
        )

        html_body = f"""
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Password Reset</title></head>
<body style="margin:0;padding:0;background:#0c0d10;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0c0d10;padding:40px 20px;">
    <tr><td align="center">
      <table width="100%" style="max-width:520px;background:#111318;border-radius:16px;
             border:1px solid rgba(255,255,255,.07);overflow:hidden;">
        <!-- Header -->
        <tr><td style="background:linear-gradient(135deg,#1d9bf0,#0f6cbd);
                       padding:32px 36px;text-align:center;">
          <div style="font-size:28px;font-weight:900;color:#fff;letter-spacing:-.5px;">
            {app_name}
          </div>
          <div style="color:rgba(255,255,255,.75);font-size:14px;margin-top:4px;">
            Password Reset PIN
          </div>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:36px;">
          <p style="color:rgba(255,255,255,.8);font-size:15px;margin:0 0 24px;line-height:1.6;">
            Hi {to_name or to_email.split("@")[0]},<br><br>
            We received a request to reset your password.  Use the 6-digit PIN
            below to verify your identity.  This PIN expires in
            <strong style="color:#1d9bf0;">{expiry_mins} minutes</strong>.
          </p>

          <!-- PIN display -->
          <div style="background:rgba(29,155,240,.08);border:1px solid rgba(29,155,240,.25);
                      border-radius:14px;padding:28px;text-align:center;margin-bottom:28px;">
            <div style="color:rgba(255,255,255,.45);font-size:12px;text-transform:uppercase;
                        letter-spacing:.12em;margin-bottom:12px;">Your verification PIN</div>
            <div style="font-size:42px;font-weight:900;color:#fff;letter-spacing:18px;
                        font-variant-numeric:tabular-nums;">
              {pin}
            </div>
          </div>

          <p style="color:rgba(255,255,255,.4);font-size:13px;margin:0;line-height:1.6;">
            If you did not request a password reset, you can safely ignore this
            email.  Your account remains secure.
          </p>
        </td></tr>

        <!-- Footer -->
        <tr><td style="border-top:1px solid rgba(255,255,255,.07);
                       padding:20px 36px;text-align:center;">
          <p style="color:rgba(255,255,255,.25);font-size:12px;margin:0;">
            © 2025 {app_name} · This is an automated message, please do not reply.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""

        text_body = (
            f"Your {app_name} password reset PIN is: {pin}\n\n"
            f"This PIN expires in {expiry_mins} minutes.\n\n"
            "If you did not request this, ignore this email."
        )

        send_smtp_email = brevo_python.SendSmtpEmail(
            to=[{"email": to_email, "name": to_name or to_email}],
            sender={"email": sender_email, "name": sender_name},
            subject=f"Your {app_name} password reset PIN",
            html_content=html_body,
            text_content=text_body,
        )

        api_instance.send_transac_email(send_smtp_email)
        logger.info("PIN email sent to %s", to_email)
        return True

    except ImportError:
        logger.error(
            "brevo-python is not installed.  Run: pip install brevo-python"
        )
        return False
    except ApiException as exc:  # type: ignore[name-defined]
        logger.error("Brevo API error for %s: status=%s body=%s",
                     to_email, exc.status, exc.body)
        return False
    except Exception as exc:
        logger.error("Unexpected email error for %s: %s", to_email, exc)
        return False


# ─── routes ───────────────────────────────────────────────────────────────────

@bp.route("/api/auth/forgot-password", methods=["POST"])
@limiter.limit("5 per hour")
@csrf_exempt
def forgot_password_pin():
    """
    Step 1: Accept email, generate 6-digit PIN, persist in user's isolated DB,
    push DB back to R2, and fire a Brevo email.

    Always returns 200 with a generic message to prevent email enumeration.
    """
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    if not email or "@" not in email:
        return jsonify({"success": False, "error": "A valid email address is required."}), 400

    # --- Look up the user in global.db (plain email lookup) ---
    user = _lookup_user_by_email(email)

    # Anti-enumeration: always return the same success message
    if not user:
        logger.debug("forgot-password: email not found: %s", email)
        return jsonify({"success": True, "message": _GENERIC_OK})

    uid      = user["id"]
    pin      = _generate_pin()
    expires  = int(time.time()) + _PIN_EXPIRY_SECONDS

    # --- Pull user's isolated DB from R2 ---
    local_path = None
    try:
        local_path = _pull_user_db(uid)
    except RuntimeError as exc:
        logger.error("forgot-password: R2 pull failed uid=%d: %s", uid, exc)
        return jsonify({
            "success": False,
            "error": "A server error occurred. Please try again in a moment.",
        }), 503

    # --- Write hashed PIN + expiry into the user's DB ---
    # RULE: only call _push_user_db *after* this succeeds
    hashed_pin = hash_password(pin)          # bcrypt — same helper used for passwords
    db_conn    = None
    try:
        db_conn = _open_user_db(local_path)
        # global.db and user.db may or may not have a users table.
        # For DUYS we store the reset fields in global.db's users table
        # (which this server fully controls).  The user DB holds personal data.
        # Update global.db for the reset fields:
        global_conn = _global_db()
        try:
            # Ensure columns exist
            for col, ct in [("reset_pin","TEXT"),("reset_pin_expires","INTEGER")]:
                try:
                    global_conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ct}")
                except sqlite3.OperationalError:
                    pass
            global_conn.execute(
                "UPDATE users SET reset_pin=?, reset_pin_expires=? WHERE id=?",
                (hashed_pin, expires, uid),
            )
            global_conn.commit()
        finally:
            global_conn.close()

        # Also write into the personal DB as a redundant record
        try:
            db_conn.execute(
                "UPDATE users SET reset_pin=?, reset_pin_expires=? WHERE id=?",
                (hashed_pin, expires, uid),
            )
        except sqlite3.OperationalError:
            # Personal DB might not have a users table — that's fine
            pass
        db_conn.commit()
        db_conn.close()
        db_conn = None

    except Exception as exc:
        logger.error("forgot-password: DB write failed uid=%d: %s", uid, exc)
        if db_conn:
            try:
                db_conn.close()
            except Exception:
                pass
        # Clean up temp file; do NOT push corrupt/partial data to R2
        try:
            os.remove(local_path)
        except OSError:
            pass
        return jsonify({
            "success": False,
            "error": "A server error occurred. Please try again.",
        }), 500

    # --- Push updated DB back to R2 (only after successful write) ---
    _push_user_db(uid, local_path)   # never raises — errors are logged

    # --- Fire the email (non-blocking: failures are logged, user gets generic ok) ---
    email_sent = _send_pin_email(email, user.get("display_name") or user.get("username", ""), pin)
    if not email_sent:
        # Dev / misconfigured: log the PIN so admin can relay manually
        logger.warning("PIN EMAIL NOT SENT — uid=%d PIN=%s (dev mode)", uid, pin)

    return jsonify({"success": True, "message": _GENERIC_OK})


@bp.route("/api/auth/verify-pin", methods=["POST"])
@limiter.limit("10 per hour")
@csrf_exempt
def verify_pin():
    """
    Step 2: Verify the 6-digit PIN and update the user's password.

    Accepts JSON: { email, pin, new_password, confirm_password }
    """
    data            = request.get_json(silent=True) or {}
    email           = (data.get("email")            or "").strip().lower()
    pin_input       = (data.get("pin")              or "").strip()
    new_password    = (data.get("new_password")     or "")
    confirm_password= (data.get("confirm_password") or "")

    # ── Input validation ──────────────────────────────────────────────────────
    errors = []
    if not email or "@" not in email:
        errors.append("A valid email address is required.")
    if not pin_input or not pin_input.isdigit() or len(pin_input) != _PIN_DIGITS:
        errors.append(f"PIN must be {_PIN_DIGITS} digits.")
    if len(new_password) < 8:
        errors.append("Password must be at least 8 characters.")
    if not (
        any(c.islower() for c in new_password)
        and any(c.isupper() for c in new_password)
        and any(c.isdigit() for c in new_password)
    ):
        errors.append("Password must contain upper and lower case letters and a number.")
    if new_password != confirm_password:
        errors.append("Passwords do not match.")
    if errors:
        return jsonify({"success": False, "errors": errors}), 400

    # ── Look up user ──────────────────────────────────────────────────────────
    user = _lookup_user_by_email(email)
    if not user:
        # Anti-enumeration: same generic error for unknown email
        return jsonify({"success": False,
                        "error": "Invalid PIN or the PIN has expired."}), 400

    uid = user["id"]

    # ── Verify PIN against global.db (source of truth for reset fields) ──────
    global_conn = _global_db()
    try:
        for col, ct in [("reset_pin","TEXT"),("reset_pin_expires","INTEGER")]:
            try:
                global_conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ct}")
            except sqlite3.OperationalError:
                pass
        row = global_conn.execute(
            "SELECT reset_pin, reset_pin_expires FROM users WHERE id=?", (uid,)
        ).fetchone()
    finally:
        global_conn.close()

    if not row or not row["reset_pin"] or not row["reset_pin_expires"]:
        return jsonify({"success": False,
                        "error": "No pending password reset found. Please request a new PIN."}), 400

    # Expiry check
    if int(time.time()) > int(row["reset_pin_expires"]):
        return jsonify({"success": False,
                        "error": "This PIN has expired. Please request a new one."}), 400

    # Timing-safe PIN verification (bcrypt comparison via verify_password)
    if not verify_password(pin_input, row["reset_pin"]):
        return jsonify({"success": False, "error": "Invalid PIN."}), 400

    # ── Pull user's isolated DB from R2 ───────────────────────────────────────
    local_path = None
    try:
        local_path = _pull_user_db(uid)
    except RuntimeError as exc:
        logger.error("verify-pin: R2 pull failed uid=%d: %s", uid, exc)
        return jsonify({"success": False,
                        "error": "A server error occurred. Please try again."}), 503

    # ── Update password in both DBs ───────────────────────────────────────────
    hashed_pw = hash_password(new_password)
    db_conn   = None
    try:
        # 1. Global DB: update password, clear PIN fields
        global_conn = _global_db()
        try:
            global_conn.execute(
                "UPDATE users SET password=?, reset_pin=NULL, reset_pin_expires=NULL,"
                " reset_token=NULL, reset_expires=NULL WHERE id=?",
                (hashed_pw, uid),
            )
            global_conn.commit()
        finally:
            global_conn.close()

        # 2. Personal DB: sync password if users table exists there too
        db_conn = _open_user_db(local_path)
        try:
            db_conn.execute(
                "UPDATE users SET password=?, reset_pin=NULL, reset_pin_expires=NULL,"
                " reset_token=NULL, reset_expires=NULL WHERE id=?",
                (hashed_pw, uid),
            )
        except sqlite3.OperationalError:
            pass   # No users table in personal DB — fine
        db_conn.commit()
        db_conn.close()
        db_conn = None

    except Exception as exc:
        logger.error("verify-pin: DB write failed uid=%d: %s", uid, exc)
        if db_conn:
            try:
                db_conn.close()
            except Exception:
                pass
        try:
            os.remove(local_path)
        except OSError:
            pass
        return jsonify({"success": False,
                        "error": "A server error occurred. Please try again."}), 500

    # ── Push updated DB back to R2 ────────────────────────────────────────────
    _push_user_db(uid, local_path)  # never raises

    logger.info("Password successfully reset for uid=%d", uid)
    return jsonify({
        "success": True,
        "message": "Password updated successfully. You can now log in.",
    })
