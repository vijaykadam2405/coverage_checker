"""Object store for crawler artifacts — MinIO/S3 when configured, else local disk.

Artifacts (images, PDFs, screenshots) are addressed by an
``s3://mallory-raw/<kind>/<sha>.<ext>`` URI, mirroring the contract's storage_path
shape so Layer 2 sees familiar paths. When ``MINIO_ENDPOINT`` is set the bytes go to
MinIO; otherwise they land under ``data/storage/``. The URI is identical either way, so
nothing downstream changes.
"""
from __future__ import annotations

import hashlib
import io
import re

from . import config

# kind -> subdir (mirrors the contract examples: img/, doc/, shot/)
_KIND_DIR = {"img": "img", "doc": "doc", "shot": "shot"}

_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
         "webp": "image/webp", "pdf": "application/pdf"}

_minio_client = None
_minio_warned = False  # so the fallback logs loudly ONCE, not on every asset


def _client():
    """Lazily build (and cache) the MinIO client; ensure the bucket exists once."""
    global _minio_client
    if _minio_client is None:
        from minio import Minio
        _minio_client = Minio(
            config.MINIO_ENDPOINT, access_key=config.MINIO_ACCESS_KEY,
            secret_key=config.MINIO_SECRET_KEY, secure=config.MINIO_SECURE,
        )
        if not _minio_client.bucket_exists(config.MINIO_BUCKET):
            _minio_client.make_bucket(config.MINIO_BUCKET)
    return _minio_client


def _safe_ext(ext: str) -> str:
    """Filesystem-safe extension: live URLs yield things like 'jpg?' or 'png#x'."""
    cleaned = re.sub(r"[^a-z0-9]", "", ext.lstrip(".").lower())[:5]
    return cleaned or "bin"


def put(data: bytes, kind: str, ext: str) -> str:
    """Store bytes content-addressably; return the s3://... storage_path URI.

    Writes to MinIO when MINIO_ENDPOINT is set, else to local disk. URI unchanged.
    """
    sub = _KIND_DIR.get(kind, kind)
    sha = hashlib.sha256(data).hexdigest()[:32]
    rel = f"{sub}/{sha}.{_safe_ext(ext)}"

    if config.MINIO_ENDPOINT:
        try:
            client = _client()
            client.put_object(
                config.MINIO_BUCKET, rel, io.BytesIO(data), length=len(data),
                content_type=_MIME.get(_safe_ext(ext), "application/octet-stream"),
            )
            return f"{config.STORAGE_URI_PREFIX}/{rel}"
        except Exception as exc:
            # Keep the blob (fall through to local disk) so a transient MinIO blip never drops
            # it — but SHOUT once. A silent fallback hid a missing `minio` package for a whole
            # crawl: assets landed on ephemeral container disk L2 can't read.
            global _minio_warned
            if not _minio_warned:
                _minio_warned = True
                import sys
                print(f"[storage] MinIO write FAILED ({type(exc).__name__}: {exc}) — falling "
                      f"back to LOCAL disk. Assets will NOT be in MinIO / visible to L2.",
                      file=sys.stderr, flush=True)

    config.ensure_dirs()
    dest = config.STORAGE_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return f"{config.STORAGE_URI_PREFIX}/{rel}"


def local_path(storage_uri: str):
    """Resolve an s3://mallory-raw/... URI back to its local file (for tests)."""
    rel = storage_uri.replace(config.STORAGE_URI_PREFIX + "/", "")
    return config.STORAGE_DIR / rel


def get(storage_uri: str) -> bytes | None:
    """Read stored bytes back by their s3://... URI — MinIO when configured, else local disk.
    The inverse of put(); None if the object exists nowhere (e.g. flushed)."""
    rel = storage_uri.replace(config.STORAGE_URI_PREFIX + "/", "")
    if ".." in rel:                       # never traverse outside our namespace
        return None
    if config.MINIO_ENDPOINT:
        try:
            resp = _client().get_object(config.MINIO_BUCKET, rel)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()
        except Exception:
            pass                          # object not in MinIO → try local disk
    p = config.STORAGE_DIR / rel
    try:
        return p.read_bytes()
    except Exception:
        return None


def content_type_for(storage_uri: str, data: bytes) -> str:
    """Best-effort MIME for inline display. SNIFF the actual bytes FIRST — the file extension
    lies (a ``.pdf`` link that returned an HTML error page must be served as text/html, or the
    browser shows 'Failed to load PDF'). Falls back to extension, then kind subdir."""
    head = data[:512]
    if head[:4] == b"%PDF":
        return "application/pdf"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    low = head.lstrip().lower()
    if low.startswith((b"<!doctype", b"<html", b"<head", b"<body", b"<?xml", b"<")):
        return "text/html; charset=utf-8"      # a mislabeled .pdf that's really an HTML page

    rel = storage_uri.replace(config.STORAGE_URI_PREFIX + "/", "")
    fname = rel.rsplit("/", 1)[-1]
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext in _MIME:
        return _MIME[ext]
    if rel.startswith("shot/"):
        return "image/png"
    if rel.startswith("img/"):
        return "image/jpeg"
    return "application/octet-stream"
