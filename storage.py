"""
storage.py — Cloudflare R2 object storage (two-bucket architecture).

Bucket 1 (R2_BUCKET_NAME / existing media bucket):
  Public media files: post images, videos, avatars, banners, stories.
  Served via R2_PUBLIC_URL CDN.

Bucket 2 (R2_DB_BUCKET_NAME):
  Private per-user SQLite databases: users/{user_id}.db
  Not publicly accessible. Only accessed by this server via boto3.

Both buckets share the same boto3 client (same R2 account credentials).

All media previously stored as base64 blobs in PostgreSQL is uploaded
here and replaced with a public CDN URL.

R2 advantages over traditional S3/B2:
  • Zero egress fees — serving files to users is completely free
  • Cloudflare CDN built-in — files are served from 300+ edge locations
  • S3-compatible API — same boto3 code, just a different endpoint
  • Free tier: 10 GB storage, 1M Class-A ops/month, 10M Class-B ops/month

Upload paths inside the bucket:
  posts/{user_id}/{uuid}.{ext}           — post images / video
  messages/{conv_id}/{uuid}.{ext}        — DM file attachments
  group_messages/{group_id}/{uuid}.{ext} — group chat file attachments
  avatars/{user_id}/{uuid}.{ext}         — profile avatars
  banners/{user_id}/{uuid}.{ext}         — profile banners

Required environment variables:
  R2_ACCESS_KEY_ID      — R2 API token Access Key ID
                          (Cloudflare Dashboard → R2 → Manage R2 API Tokens)
  R2_SECRET_ACCESS_KEY  — R2 API token Secret Access Key
  R2_BUCKET_NAME        — Name of your R2 bucket  (e.g. duys-boost-media)
  R2_ACCOUNT_ID         — Your Cloudflare Account ID
                          (shown in the R2 overview page URL and sidebar)
  R2_PUBLIC_URL         — Public bucket URL for serving files
                          Enable "Public Access" on the bucket, then copy
                          the URL shown (e.g. https://pub-xxxx.r2.dev)
                          OR use a custom domain pointed at the bucket.

Optional:
  R2_MAX_FILE_MB        — Max upload size in MB (default: 25)

The S3 endpoint is derived automatically from R2_ACCOUNT_ID:
  https://<account_id>.r2.cloudflarestorage.com
"""

import os
import uuid
import mimetypes
import base64
import io
import logging

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ── Allowed MIME types ────────────────────────────────────────────────────────
ALLOWED_IMAGE_MIMES = {
    'image/jpeg', 'image/png', 'image/webp', 'image/gif',
}
ALLOWED_FILE_MIMES = ALLOWED_IMAGE_MIMES | {
    'video/mp4', 'video/webm', 'video/ogg',
    'audio/mpeg', 'audio/ogg', 'audio/wav', 'audio/webm',
    'application/pdf',
    'text/plain',
}

_MIME_TO_EXT = {
    'image/jpeg':      'jpg',
    'image/png':       'png',
    'image/webp':      'webp',
    'image/gif':       'gif',
    'video/mp4':       'mp4',
    'video/webm':      'webm',
    'video/ogg':       'ogv',
    'audio/mpeg':      'mp3',
    'audio/ogg':       'ogg',
    'audio/wav':       'wav',
    'audio/webm':      'weba',
    'application/pdf': 'pdf',
    'text/plain':      'txt',
}


# ── R2 client ─────────────────────────────────────────────────────────────────

def _get_client():
    """Return a boto3 S3 client pointed at Cloudflare R2."""
    access_key  = os.environ.get('R2_ACCESS_KEY_ID', '')
    secret_key  = os.environ.get('R2_SECRET_ACCESS_KEY', '')
    account_id  = os.environ.get('R2_ACCOUNT_ID', '')

    if not all([access_key, secret_key, account_id]):
        raise RuntimeError(
            'Cloudflare R2 is not configured. '
            'Set R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and R2_ACCOUNT_ID '
            'in your environment.'
        )

    endpoint_url = f'https://{account_id}.r2.cloudflarestorage.com'

    return boto3.client(
        's3',
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        # R2 requires SigV4; region is always 'auto'
        config=Config(signature_version='s3v4'),
        region_name='auto',
    )


def _bucket() -> str:
    name = os.environ.get('R2_BUCKET_NAME', '')
    if not name:
        raise RuntimeError('R2_BUCKET_NAME is not set.')
    return name


def _public_url_base() -> str:
    """
    Return the base CDN URL used to serve uploaded files publicly.

    Priority order:
      1. R2_PUBLIC_URL env var  (recommended — your r2.dev or custom domain)
      2. Constructed from R2_ACCOUNT_ID + R2_BUCKET_NAME
      3. Extracted from R2_ENDPOINT_URL (account_id embedded in hostname)
      4. R2_ENDPOINT_URL itself with bucket name appended
         (private endpoint — files won't be publicly served but URL is stored)

    Never raises — always returns a best-effort URL so posts are never lost.
    """
    # 1. Explicit public URL — best option
    url = os.environ.get('R2_PUBLIC_URL', '').strip().rstrip('/')
    if url:
        return url

    bucket_name = os.environ.get('R2_BUCKET_NAME', '').strip()

    # 2. Construct from R2_ACCOUNT_ID
    account_id = os.environ.get('R2_ACCOUNT_ID', '').strip()
    if account_id and bucket_name:
        constructed = f'https://{account_id}.r2.cloudflarestorage.com/{bucket_name}'
        logger.warning(
            'R2_PUBLIC_URL not set — using private endpoint %s. '
            'Files will not be publicly accessible until R2_PUBLIC_URL is configured.',
            constructed,
        )
        return constructed

    # 3. Extract account_id from R2_ENDPOINT_URL
    # Render users often set R2_ENDPOINT_URL but forget R2_PUBLIC_URL / R2_ACCOUNT_ID
    endpoint = os.environ.get('R2_ENDPOINT_URL', '').strip().rstrip('/')
    if endpoint and bucket_name:
        # e.g. https://abc123.r2.cloudflarestorage.com  →  abc123
        import re as _re
        m = _re.match(r'https?://([^.]+)\.r2\.cloudflarestorage\.com', endpoint)
        if m:
            acct = m.group(1)
            constructed = f'https://{acct}.r2.cloudflarestorage.com/{bucket_name}'
            logger.warning(
                'R2_PUBLIC_URL not set — derived URL from R2_ENDPOINT_URL: %s. '
                'Set R2_PUBLIC_URL to your r2.dev subdomain for public access.',
                constructed,
            )
            return constructed
        # Fallback: endpoint + bucket_name
        constructed = f'{endpoint}/{bucket_name}'
        logger.warning('R2_PUBLIC_URL not set — using endpoint fallback: %s', constructed)
        return constructed

    # 4. Last resort: return a placeholder so the upload isn't lost
    logger.error(
        'R2_PUBLIC_URL is not set and could not be derived. '
        'Set R2_PUBLIC_URL in your Render environment variables. '
        'Media will be uploaded but the stored URL may not be publicly accessible.'
    )
    return 'https://media.placeholder/missing-r2-public-url'  # stored in DB; won't 404 the post


def _max_bytes() -> int:
    try:
        return int(os.environ.get('R2_MAX_FILE_MB', 25)) * 1024 * 1024
    except ValueError:
        return 25 * 1024 * 1024


# ── Core upload ───────────────────────────────────────────────────────────────

def upload_bytes(
    data: bytes,
    mime: str,
    key_prefix: str,
    filename_hint: str = '',
) -> str:
    """
    Upload raw bytes to R2 and return the public URL.

    Args:
        data:          Raw file bytes.
        mime:          MIME type string (e.g. 'image/jpeg').
        key_prefix:    Path prefix inside the bucket (e.g. 'avatars/42').
        filename_hint: Original filename hint for Content-Disposition.

    Returns:
        Public URL string (served via Cloudflare CDN, zero egress cost).

    Raises:
        ValueError:   File too large or unsupported MIME type.
        RuntimeError: R2 credentials missing or upload failed.
    """
    if len(data) > _max_bytes():
        mb = _max_bytes() // (1024 * 1024)
        raise ValueError(f'File too large. Maximum size is {mb} MB.')

    ext = _MIME_TO_EXT.get(mime) or (
        mimetypes.guess_extension(mime, strict=False) or 'bin'
    ).lstrip('.')

    object_key = f'{key_prefix}/{uuid.uuid4().hex}.{ext}'

    # R2 public buckets don't use ACLs — public access is bucket-level
    extra_args = {'ContentType': mime}
    if filename_hint:
        safe_name = filename_hint.replace('"', '_')
        extra_args['ContentDisposition'] = f'inline; filename="{safe_name}"'

    try:
        client = _get_client()
        client.upload_fileobj(
            io.BytesIO(data),
            _bucket(),
            object_key,
            ExtraArgs=extra_args,
        )
    except ClientError as e:
        logger.error('R2 upload failed: %s', e)
        raise RuntimeError(f'Upload to R2 failed: {e}') from e

    return f'{_public_url_base()}/{object_key}'


def upload_file_object(
    file_obj,
    mime: str,
    key_prefix: str,
    filename_hint: str = '',
) -> str:
    """Upload a file-like object (e.g. from Flask request.files)."""
    data = file_obj.read()
    return upload_bytes(data, mime, key_prefix, filename_hint=filename_hint)


def upload_data_uri(data_uri: str, key_prefix: str) -> str:
    """
    Upload a base64 data-URI (e.g. 'data:image/png;base64,...') to R2.
    Returns the public CDN URL.
    """
    if not data_uri or not data_uri.startswith('data:'):
        raise ValueError('Invalid data URI.')

    header, _, encoded = data_uri.partition(',')
    mime = header.split(';')[0][5:]  # strip leading 'data:'
    if not mime:
        raise ValueError('Could not determine MIME type from data URI.')

    try:
        raw = base64.b64decode(encoded)
    except Exception as e:
        raise ValueError(f'Invalid base64 data: {e}') from e

    return upload_bytes(raw, mime, key_prefix)


def delete_object(url: str) -> bool:
    """
    Delete a file from R2 given its public URL.
    Returns True on success, False if not found.
    Silently ignores other errors (best-effort cleanup).
    """
    try:
        base = _public_url_base()
    except RuntimeError:
        return False

    if not url.startswith(base):
        return False  # not our file

    object_key = url[len(base):].lstrip('/')
    try:
        client = _get_client()
        client.delete_object(Bucket=_bucket(), Key=object_key)
        return True
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code == 'NoSuchKey':
            return False
        logger.warning('R2 delete failed for %s: %s', object_key, e)
        return False


# ── Convenience wrappers ──────────────────────────────────────────────────────

def upload_post_media(user_id: int, data_uri: str) -> str:
    """Upload post image/video from a data-URI. Returns public R2 URL."""
    return upload_data_uri(data_uri, f'posts/{user_id}')


def upload_message_file(conv_id: int, data_uri: str) -> str:
    """Upload a DM attachment from a data-URI. Returns public R2 URL."""
    return upload_data_uri(data_uri, f'messages/{conv_id}')


def upload_group_file(group_id: int, data_uri: str) -> str:
    """Upload a group message attachment from a data-URI. Returns public R2 URL."""
    return upload_data_uri(data_uri, f'group_messages/{group_id}')


def upload_avatar(user_id: int, file_obj, mime: str) -> str:
    """Upload a profile avatar from a Flask file upload. Returns public R2 URL."""
    if mime not in ALLOWED_IMAGE_MIMES:
        raise ValueError('Unsupported image type. Use JPG, PNG, WebP or GIF.')
    return upload_file_object(file_obj, mime, f'avatars/{user_id}')


def upload_banner(user_id: int, file_obj, mime: str) -> str:
    """Upload a profile banner from a Flask file upload. Returns public R2 URL."""
    if mime not in ALLOWED_IMAGE_MIMES:
        raise ValueError('Unsupported image type. Use JPG, PNG, WebP or GIF.')
    return upload_file_object(file_obj, mime, f'banners/{user_id}')


# ── Health check ──────────────────────────────────────────────────────────────

def check_connection() -> dict:
    """
    Verify R2 credentials and bucket access.
    Returns {'ok': True, 'bucket': ..., 'public_url': ...}
         or {'ok': False, 'error': '...'}.
    """
    try:
        client = _get_client()
        client.head_bucket(Bucket=_bucket())
        return {
            'ok':         True,
            'bucket':     _bucket(),
            'public_url': _public_url_base(),
            'provider':   'Cloudflare R2',
        }
    except RuntimeError as e:
        return {'ok': False, 'error': str(e)}
    except ClientError as e:
        return {'ok': False, 'error': str(e)}
