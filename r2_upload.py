"""
Cloudflare R2 upload helper for Arc Comic.

Kept as its own module (not jammed into bot.py) so the R2 logic is easy
to read, test, and swap out later if you ever change storage providers —
bot.py just calls upload_cover() and get_cover_url() and doesn't need to
know anything about S3/boto3 internals.
"""
import os
import sys
import subprocess

try:
    import boto3
    from botocore.config import Config
except ImportError:
    print("📦 Installing boto3 (needed for R2 uploads)...")
    subprocess.run([sys.executable, "-m", "pip", "install", "boto3",
                    "--break-system-packages", "-q"], check=False)
    import boto3
    from botocore.config import Config


def _get_client(cfg):
    """Builds an S3-compatible client pointed at the R2 account endpoint.
    Returns None if R2 isn't configured yet (so callers can fall back to
    local-only storage instead of crashing)."""
    account_id = cfg.get("r2_account_id", "")
    access_key = cfg.get("r2_access_key_id", "")
    secret_key = cfg.get("r2_secret_access_key", "")
    if not (account_id and access_key and secret_key):
        return None

    endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def is_configured(cfg):
    """True once all 4 R2 fields are filled in the dashboard."""
    return bool(
        cfg.get("r2_account_id") and cfg.get("r2_access_key_id")
        and cfg.get("r2_secret_access_key") and cfg.get("r2_bucket_name")
        and cfg.get("r2_public_url")
    )


def get_cover_url(cfg, code):
    """Builds the public URL a cover will live at once uploaded, without
    needing to actually upload it. Used when writing .md front matter."""
    public_base = cfg.get("r2_public_url", "").rstrip("/")
    return f"{public_base}/covers/{code}.jpg"


def upload_cover(cfg, local_path, code):
    """
    Uploads a single cover file to R2 under covers/<code>.jpg.
    Returns the public URL on success, or None on failure (caller should
    treat None as "upload failed, local file is the fallback" rather than
    crash the posting flow over an R2 hiccup).
    """
    client = _get_client(cfg)
    if client is None:
        return None
    bucket = cfg.get("r2_bucket_name", "")
    key = f"covers/{code}.jpg"
    try:
        client.upload_file(
            local_path, bucket, key,
            ExtraArgs={"ContentType": "image/jpeg", "CacheControl": "public, max-age=31536000"},
        )
        return get_cover_url(cfg, code)
    except Exception as e:
        print(f"⚠️ R2 upload failed for {code}: {e}")
        return None


def delete_cover(cfg, code):
    """Removes a cover from R2 (used by delete_post_record). Safe to call
    even if the object doesn't exist or R2 isn't configured."""
    client = _get_client(cfg)
    if client is None:
        return
    bucket = cfg.get("r2_bucket_name", "")
    key = f"covers/{code}.jpg"
    try:
        client.delete_object(Bucket=bucket, Key=key)
    except Exception as e:
        print(f"⚠️ R2 delete failed for {code}: {e}")


def list_covers(cfg):
    """Returns the set of codes already uploaded to R2 (from the
    covers/<code>.jpg keys), so migration can skip ones already done."""
    client = _get_client(cfg)
    if client is None:
        return set()
    bucket = cfg.get("r2_bucket_name", "")
    codes = set()
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix="covers/"):
            for obj in page.get("Contents", []):
                fname = obj["Key"].split("/")[-1]
                if fname.endswith(".jpg"):
                    codes.add(fname[:-4])
    except Exception as e:
        print(f"⚠️ R2 list failed: {e}")
    return codes
