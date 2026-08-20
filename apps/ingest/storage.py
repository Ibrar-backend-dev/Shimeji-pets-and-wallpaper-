"""
Backblaze B2 access, through its S3-compatible API.

Two rules shape everything here:

1. **Bulk media bytes never pass through this process.** The admin browser PUTs
   directly to B2 against a presigned URL. A free-tier dyno has ~512 MB of RAM
   and a 30-60 s request ceiling; proxying a fifty-file bulk upload through it
   would fail, and fail slowly. This module therefore moves kilobytes: presigned
   URLs out, object metadata and small byte ranges in.

2. **Object keys are minted server-side, always.** The uploader's filename never
   reaches a key, which removes path traversal and key collisions as categories
   of bug rather than as things to remember to check.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

logger = logging.getLogger("storage.b2")

_client_lock = threading.Lock()
_client = None


class StorageError(RuntimeError):
    """Raised when B2 cannot service a request. Never leaked to a client verbatim."""


class ObjectNotFound(StorageError):
    pass


@dataclass(frozen=True)
class ObjectMeta:
    """What B2 reports about a stored object."""

    key: str
    size: int
    content_type: str
    etag: str
    last_modified: datetime | None


def is_configured() -> bool:
    return bool(
        settings.B2_KEY_ID
        and settings.B2_APPLICATION_KEY
        and settings.B2_BUCKET_NAME
        and settings.B2_ENDPOINT_URL
    )


def get_client():
    """
    Lazily built, process-wide boto3 client.

    Constructing an S3 client parses botocore's JSON service model and costs
    tens of milliseconds; doing that per request would show up directly in p99.
    """
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        if _client is not None:  # pragma: no cover - double-checked locking
            return _client
        if not is_configured():
            raise StorageError(
                "B2 storage is not configured. Set B2_KEY_ID, B2_APPLICATION_KEY, "
                "B2_BUCKET_NAME and B2_ENDPOINT_URL."
            )
        _client = boto3.client(
            "s3",
            endpoint_url=settings.B2_ENDPOINT_URL,
            aws_access_key_id=settings.B2_KEY_ID,
            aws_secret_access_key=settings.B2_APPLICATION_KEY,
            region_name=settings.B2_REGION,
            config=Config(
                signature_version="s3v4",
                # B2 uses path-style addressing, not virtual-host buckets.
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=15,
            ),
        )
        return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests that swap credentials or mocks."""
    global _client
    with _client_lock:
        _client = None


# --------------------------------------------------------------------------- #
# Key construction
# --------------------------------------------------------------------------- #

_EXT_RE = re.compile(r"^[a-z0-9]{1,8}$")

# Canonical extension per accepted MIME type. The uploader's own extension is
# never trusted; this mapping is the only source of the stored suffix.
MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
}


def extension_for_mime(mime: str) -> str:
    return MIME_EXTENSIONS.get(mime.lower(), "bin")


def build_object_key(
    *,
    feature_slug: str,
    category_slug: str,
    kind: str,
    unique_id: str,
    mime: str,
) -> str:
    """
    Compose a storage key: {feature}/{category}/{kind}/{YYYY}/{MM}/{uuid}.{ext}

    Every component is either a validated slug, a fixed literal, a date, or a
    server-generated UUID. Nothing here originates from a client, so the result
    cannot escape its prefix.
    """
    now = datetime.now(tz=UTC)
    ext = extension_for_mime(mime)
    if not _EXT_RE.match(ext):  # pragma: no cover - defensive
        ext = "bin"
    return f"{feature_slug}/{category_slug}/{kind}/{now:%Y}/{now:%m}/{unique_id}.{ext}"


def public_url(key: str) -> str:
    """
    Build the CDN URL for a key.

    URLs are computed, never stored, so moving CDN hostnames is one environment
    variable rather than a data migration over every row.
    """
    if not key:
        return ""
    base = (settings.MEDIA_CDN_BASE_URL or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/{key.lstrip('/')}"


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


def presign_put(
    *, key: str, content_type: str, content_length: int, expires_in: int | None = None
) -> tuple[str, dict[str, str]]:
    """
    Presign a single-object PUT.

    Both ContentType and ContentLength are signed into the URL, so they become
    part of SignedHeaders: the browser must reproduce them exactly or B2 rejects
    the upload. That is what stops a presign issued for a 2 MB PNG from being
    redeemed for a 5 GB blob of something else.

    Returns (url, headers the client must send).
    """
    client = get_client()
    expiry = expires_in if expires_in is not None else settings.PRESIGN_EXPIRY_SECONDS

    try:
        url = client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": settings.B2_BUCKET_NAME,
                "Key": key,
                "ContentType": content_type,
                "ContentLength": content_length,
            },
            ExpiresIn=expiry,
            HttpMethod="PUT",
        )
    except (BotoCoreError, ClientError) as exc:
        logger.error(
            "Failed to presign upload",
            extra={"event": "presign_failed", "key": key, "error": str(exc)},
        )
        raise StorageError("Could not presign the upload.") from exc

    return url, {"Content-Type": content_type, "Content-Length": str(content_length)}


def create_multipart_upload(*, key: str, content_type: str) -> str:
    try:
        response = get_client().create_multipart_upload(
            Bucket=settings.B2_BUCKET_NAME, Key=key, ContentType=content_type
        )
        return str(response["UploadId"])
    except (BotoCoreError, ClientError, KeyError) as exc:
        raise StorageError("Could not start multipart upload.") from exc


def presign_upload_part(*, key: str, upload_id: str, part_number: int) -> str:
    try:
        return get_client().generate_presigned_url(
            ClientMethod="upload_part",
            Params={
                "Bucket": settings.B2_BUCKET_NAME,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=settings.PRESIGN_EXPIRY_SECONDS,
            HttpMethod="PUT",
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError("Could not presign upload part.") from exc


def list_multipart_parts(*, key: str, upload_id: str) -> list[dict]:
    try:
        paginator = get_client().get_paginator("list_parts")
        parts: list[dict] = []
        for page in paginator.paginate(
            Bucket=settings.B2_BUCKET_NAME, Key=key, UploadId=upload_id
        ):
            parts.extend(page.get("Parts", []))
        return parts
    except (BotoCoreError, ClientError) as exc:
        raise StorageError("Could not list uploaded parts.") from exc


def complete_multipart_upload(*, key: str, upload_id: str, parts: list[dict]) -> ObjectMeta:
    try:
        get_client().complete_multipart_upload(
            Bucket=settings.B2_BUCKET_NAME,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError("Could not complete multipart upload.") from exc
    return head_object(key)


def abort_multipart_upload(*, key: str, upload_id: str) -> bool:
    try:
        get_client().abort_multipart_upload(
            Bucket=settings.B2_BUCKET_NAME, Key=key, UploadId=upload_id
        )
        return True
    except (BotoCoreError, ClientError) as exc:
        logger.warning(
            "Multipart abort failed",
            extra={"event": "multipart_abort_failed", "key": key, "error": str(exc)},
        )
        return False


def head_object(key: str) -> ObjectMeta:
    """Fetch object metadata. Raises ObjectNotFound if it is not there."""
    client = get_client()
    try:
        response = client.head_object(Bucket=settings.B2_BUCKET_NAME, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            raise ObjectNotFound(f"Object not found: {key}") from exc
        logger.error(
            "head_object failed",
            extra={"event": "head_failed", "key": key, "error": str(exc)},
        )
        raise StorageError("Could not read the uploaded object.") from exc
    except BotoCoreError as exc:
        raise StorageError("Could not reach storage.") from exc

    return ObjectMeta(
        key=key,
        size=int(response.get("ContentLength", 0)),
        content_type=response.get("ContentType", "") or "",
        etag=(response.get("ETag", "") or "").strip('"'),
        last_modified=response.get("LastModified"),
    )


def get_range(key: str, start: int = 0, length: int = 4096) -> bytes:
    """
    Read a byte range.

    Used for magic-byte sniffing and for reading a zip's central directory
    without ever pulling the whole archive into memory.
    """
    client = get_client()
    end = start + length - 1
    try:
        response = client.get_object(
            Bucket=settings.B2_BUCKET_NAME, Key=key, Range=f"bytes={start}-{end}"
        )
        return response["Body"].read()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            raise ObjectNotFound(f"Object not found: {key}") from exc
        # A range beyond EOF is not an error worth failing a commit over.
        if code in {"InvalidRange", "416"}:
            return b""
        raise StorageError("Could not read the uploaded object.") from exc
    except BotoCoreError as exc:
        raise StorageError("Could not reach storage.") from exc


def get_object_bytes(key: str, max_bytes: int) -> bytes:
    """
    Download a whole object, refusing anything over max_bytes.

    The guard exists so a large file can never be pulled into a 512 MB dyno by
    accident: callers that need bytes are working on small images and previews.
    """
    meta = head_object(key)
    if meta.size > max_bytes:
        raise StorageError(
            f"Object is {meta.size} bytes, above the {max_bytes} byte processing limit."
        )
    return get_range(key, 0, meta.size)


def put_bytes(*, key: str, data: bytes, content_type: str) -> ObjectMeta:
    """Upload a small server-generated object, i.e. a Pillow-made preview."""
    client = get_client()
    try:
        client.put_object(
            Bucket=settings.B2_BUCKET_NAME,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
    except (BotoCoreError, ClientError) as exc:
        logger.error(
            "put_object failed",
            extra={"event": "put_failed", "key": key, "error": str(exc)},
        )
        raise StorageError("Could not store the generated preview.") from exc

    return ObjectMeta(
        key=key,
        size=len(data),
        content_type=content_type,
        etag="",
        last_modified=None,
    )


def delete_object(key: str) -> bool:
    """
    Delete one object. Returns False instead of raising, so a reaper sweeping
    hundreds of orphans is never stopped by one stubborn key.
    """
    if not key:
        return False
    try:
        get_client().delete_object(Bucket=settings.B2_BUCKET_NAME, Key=key)
        return True
    except (BotoCoreError, ClientError, StorageError) as exc:
        logger.warning(
            "delete_object failed",
            extra={"event": "delete_failed", "key": key, "error": str(exc)},
        )
        return False


def storage_health() -> str:
    """Return "ok", "error", or "unconfigured" for the readiness probe."""
    if not is_configured():
        return "unconfigured"
    try:
        get_client().head_bucket(Bucket=settings.B2_BUCKET_NAME)
        return "ok"
    except (BotoCoreError, ClientError, StorageError) as exc:
        logger.error(
            "Storage health check failed",
            extra={"event": "health_storage_failed", "error": str(exc)},
        )
        return "error"
