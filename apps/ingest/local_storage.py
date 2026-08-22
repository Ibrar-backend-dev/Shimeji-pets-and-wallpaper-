"""
Filesystem storage backend, used when MEDIA_LOCAL_STORAGE is on.

A drop-in twin of the B2 functions in `storage.py`, so nothing upstream has to
know which backend is live. `storage.py` dispatches to this module; callers keep
using `storage.head_object(...)` and never import this directly.

Why this exists: B2 needs a public bucket, real credentials and a CDN host
before a single image renders. That is the right production shape and the wrong
development one — it makes a laptop, a LAN phone test, or a CI run depend on a
third party. Flipping MEDIA_LOCAL_STORAGE puts the same bytes under
LOCAL_MEDIA_ROOT and serves them from Django.

Two things genuinely differ from S3, and both are handled rather than faked:

1. **There are no presigned URLs.** S3 signs a URL that the client PUTs to
   directly. Here, `presign_put` mints a signed token and points the client at
   a local upload view (`views_media.py`) that verifies it. The signature covers
   the object key, content type and length, so — exactly as with B2 — a presign
   issued for a 2 MB PNG cannot be redeemed for something else.

2. **There is no multipart upload.** Parts are written as separate files under
   `.multipart/{upload_id}/` and concatenated on completion. The part-size and
   ordering contract the v2 API expects is preserved.

NOT FOR PRODUCTION. Bytes land on the instance's own disk, so they die with the
container, are not replicated, and are served by Django instead of a CDN.
`check_deploy` refuses to pass with this enabled and DEBUG off.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.core import signing

# Imported at module level on purpose: these types must be *identical* to the
# ones storage.py exposes, or an `except storage.ObjectNotFound` around a local
# call would silently not match. storage.py imports this module lazily, inside
# the dispatch helper, so the cycle never closes at import time.
from .storage import ObjectMeta, ObjectNotFound, StorageError

logger = logging.getLogger("storage.local")

# Namespaces the signature so a token minted for a single PUT cannot be replayed
# against the multipart part endpoint, or vice versa.
UPLOAD_SALT = "ingest.local_storage.upload"
PART_SALT = "ingest.local_storage.part"

MULTIPART_DIRNAME = ".multipart"


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def media_root() -> Path:
    return Path(settings.LOCAL_MEDIA_ROOT)


def _feature_dir(slug: str) -> str:
    """
    Translate a feature slug into its on-disk folder name.

    Object keys stay slug-based everywhere — in the database, in B2, and in the
    URLs handed to clients — because they are an interface and renaming them
    would be a data migration. This mapping applies to the local filesystem
    only, where a browsable `media/battery emoji/` beats `media/battery/`.

    An unmapped slug falls through to itself, so adding a fourth feature works
    without touching this.
    """
    mapping = getattr(settings, "LOCAL_MEDIA_FEATURE_DIRS", {}) or {}
    return str(mapping.get(slug, slug))


def _resolve(key: str) -> Path:
    """
    Map an object key to a path under LOCAL_MEDIA_ROOT.

    Keys are minted server-side by build_object_key, so the safety checks should
    never fire. They are here anyway because the upload views accept a key
    carried in a signed token, and "signed" means unmodified, not harmless — a
    bug in key construction would otherwise become an arbitrary-file-write.
    """
    if not key or key.startswith("/") or "\\" in key or ".." in key.split("/"):
        raise StorageError(f"Refusing to resolve an unsafe object key: {key!r}")

    # Only the leading feature segment is renamed; category, kind, year, month
    # and filename pass through untouched.
    head, sep, tail = key.lstrip("/").partition("/")
    on_disk = f"{_feature_dir(head)}{sep}{tail}" if sep else _feature_dir(head)

    root = media_root().resolve()
    target = (root / on_disk).resolve()
    if target != root and root not in target.parents:
        raise StorageError(f"Object key escapes the media root: {key!r}")
    return target


def _multipart_dir(upload_id: str) -> Path:
    if not upload_id or "/" in upload_id or "\\" in upload_id or ".." in upload_id:
        raise StorageError(f"Unsafe upload id: {upload_id!r}")
    return media_root().resolve() / MULTIPART_DIRNAME / upload_id


def _write_atomic(path: Path, data: bytes) -> None:
    """
    Write via a temp file and rename.

    A half-written object is worse than a missing one: validation would read
    truncated bytes and reject the upload for the wrong reason.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise StorageError(f"Could not write object: {exc}") from exc


def _meta_for(key: str, path: Path) -> ObjectMeta:
    stat = path.stat()
    return ObjectMeta(
        key=key,
        size=stat.st_size,
        content_type=_read_content_type(path),
        etag=_etag_for(path),
        last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


def _sidecar(path: Path) -> Path:
    """Content type is not a filesystem attribute, so it is stored beside the file."""
    return path.with_name(path.name + ".type")


def _read_content_type(path: Path) -> str:
    sidecar = _sidecar(path)
    if sidecar.is_file():
        try:
            return sidecar.read_text(encoding="utf-8").strip()
        except OSError:  # pragma: no cover - defensive
            return ""
    return ""


def _etag_for(path: Path) -> str:
    """MD5 of the contents, matching what S3 reports for a single-part upload."""
    digest = hashlib.md5()  # noqa: S324 - an S3 ETag, not a security primitive
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def is_configured() -> bool:
    """True when the media root exists (or can be made) and is writable."""
    try:
        root = media_root()
        root.mkdir(parents=True, exist_ok=True)
        return os.access(root, os.W_OK)
    except OSError:
        return False


def reset_client() -> None:
    """No client to reset. Present so the module stays interface-compatible."""
    return None


def storage_health() -> str:
    if not is_configured():
        return "unconfigured"
    try:
        probe = media_root() / ".health"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
        return "ok"
    except OSError as exc:
        logger.error(
            "Local storage health check failed",
            extra={"event": "health_storage_failed", "error": str(exc)},
        )
        return "error"


def public_url(key: str) -> str:
    """
    Serve URL for a key.

    Deliberately built from LOCAL_MEDIA_ORIGIN rather than MEDIA_CDN_BASE_URL:
    in local mode there is no CDN, and silently reusing the CDN variable would
    produce URLs pointing at a bucket that does not hold these bytes.
    """
    if not key:
        return ""
    base = (settings.LOCAL_MEDIA_ORIGIN or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/media/{quote(key.lstrip('/'))}"


# --------------------------------------------------------------------------- #
# Signed upload tokens
# --------------------------------------------------------------------------- #


def _sign(payload: dict, salt: str) -> str:
    return signing.dumps(payload, salt=salt)


def verify_upload_token(token: str, *, salt: str, max_age: int) -> dict:
    """Called by the upload views. Raises StorageError on anything suspect."""
    try:
        return signing.loads(token, salt=salt, max_age=max_age)
    except signing.SignatureExpired as exc:
        raise StorageError("Upload URL has expired.") from exc
    except signing.BadSignature as exc:
        raise StorageError("Upload URL is not valid.") from exc


def presign_put(
    *, key: str, content_type: str, content_length: int, expires_in: int | None = None
) -> tuple[str, dict[str, str]]:
    """
    Mint a signed local upload URL.

    Mirrors the B2 contract: content type and length are bound into the
    signature, and the view rejects a request whose headers disagree.
    """
    _resolve(key)  # fail now, not after the client has uploaded
    expiry = expires_in if expires_in is not None else settings.PRESIGN_EXPIRY_SECONDS
    token = _sign(
        {"key": key, "content_type": content_type, "content_length": int(content_length)},
        UPLOAD_SALT,
    )
    base = (settings.LOCAL_MEDIA_ORIGIN or "").rstrip("/")
    url = f"{base}/media/upload/?token={quote(token)}&expires_in={expiry}"
    return url, {"Content-Type": content_type, "Content-Length": str(content_length)}


def put_bytes(*, key: str, data: bytes, content_type: str) -> ObjectMeta:
    path = _resolve(key)
    _write_atomic(path, data)
    _sidecar(path).write_text(content_type, encoding="utf-8")
    return ObjectMeta(
        key=key,
        size=len(data),
        content_type=content_type,
        etag=hashlib.md5(data).hexdigest(),  # noqa: S324
        last_modified=datetime.now(tz=UTC),
    )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def head_object(key: str) -> ObjectMeta:
    path = _resolve(key)
    if not path.is_file():
        raise ObjectNotFound(f"Object not found: {key}")
    return _meta_for(key, path)


def get_range(key: str, start: int = 0, length: int = 4096) -> bytes:
    path = _resolve(key)
    if not path.is_file():
        raise ObjectNotFound(f"Object not found: {key}")
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            return handle.read(length)
    except OSError as exc:
        raise StorageError(f"Could not read object: {exc}") from exc


def get_object_bytes(key: str, max_bytes: int) -> bytes:
    meta = head_object(key)
    if meta.size > max_bytes:
        raise StorageError(
            f"Object is {meta.size} bytes, above the {max_bytes} byte processing limit."
        )
    return get_range(key, 0, meta.size)


def delete_object(key: str) -> bool:
    """Returns False rather than raising, so a reaper is never stopped by one key."""
    if not key:
        return False
    try:
        path = _resolve(key)
    except StorageError:
        return False
    try:
        if not path.is_file():
            return False
        path.unlink()
        _sidecar(path).unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.warning(
            "delete_object failed",
            extra={"event": "delete_failed", "key": key, "error": str(exc)},
        )
        return False


# --------------------------------------------------------------------------- #
# Multipart
# --------------------------------------------------------------------------- #


def create_multipart_upload(*, key: str, content_type: str) -> str:
    _resolve(key)
    upload_id = uuid.uuid4().hex
    directory = _multipart_dir(upload_id)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "meta.json").write_text(
            json.dumps({"key": key, "content_type": content_type}), encoding="utf-8"
        )
    except OSError as exc:
        raise StorageError(f"Could not start multipart upload: {exc}") from exc
    return upload_id


def _multipart_meta(upload_id: str) -> dict:
    meta_path = _multipart_dir(upload_id) / "meta.json"
    if not meta_path.is_file():
        raise StorageError(f"Unknown multipart upload: {upload_id}")
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StorageError("Multipart session metadata is unreadable.") from exc


def presign_upload_part(*, key: str, upload_id: str, part_number: int) -> str:
    _multipart_meta(upload_id)
    token = _sign(
        {"key": key, "upload_id": upload_id, "part_number": int(part_number)}, PART_SALT
    )
    base = (settings.LOCAL_MEDIA_ORIGIN or "").rstrip("/")
    return f"{base}/media/upload-part/?token={quote(token)}"


def _part_path(upload_id: str, part_number: int) -> Path:
    return _multipart_dir(upload_id) / f"{int(part_number):05d}.part"


def write_part(*, upload_id: str, part_number: int, data: bytes) -> str:
    """Called by the part-upload view. Returns the part's ETag."""
    _multipart_meta(upload_id)
    _write_atomic(_part_path(upload_id, part_number), data)
    return hashlib.md5(data).hexdigest()  # noqa: S324


def list_multipart_parts(*, key: str, upload_id: str) -> list[dict]:
    """Shaped like boto3's list_parts output, which is what the v2 views expect."""
    directory = _multipart_dir(upload_id)
    if not directory.is_dir():
        return []
    parts: list[dict] = []
    for part_file in sorted(directory.glob("*.part")):
        try:
            number = int(part_file.stem)
        except ValueError:  # pragma: no cover - defensive
            continue
        parts.append(
            {
                "PartNumber": number,
                "ETag": _etag_for(part_file),
                "Size": part_file.stat().st_size,
            }
        )
    return parts


def complete_multipart_upload(*, key: str, upload_id: str, parts: list[dict]) -> ObjectMeta:
    """
    Concatenate the parts, in the order the client declared, into the final key.

    Streamed rather than joined in memory: a multipart upload is by definition
    the large-file path, and buffering it would defeat the point.
    """
    meta = _multipart_meta(upload_id)
    directory = _multipart_dir(upload_id)
    target = _resolve(key)
    target.parent.mkdir(parents=True, exist_ok=True)

    ordered = sorted(parts, key=lambda p: int(p["PartNumber"]))
    tmp = target.with_suffix(target.suffix + f".{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as out:
            for part in ordered:
                part_path = _part_path(upload_id, int(part["PartNumber"]))
                if not part_path.is_file():
                    raise StorageError(f"Missing part {part['PartNumber']} for {key}.")
                with part_path.open("rb") as handle:
                    shutil.copyfileobj(handle, out, length=1024 * 1024)
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise StorageError(f"Could not complete multipart upload: {exc}") from exc
    except StorageError:
        tmp.unlink(missing_ok=True)
        raise

    _sidecar(target).write_text(meta.get("content_type", ""), encoding="utf-8")
    shutil.rmtree(directory, ignore_errors=True)
    return head_object(key)


def abort_multipart_upload(*, key: str, upload_id: str) -> bool:
    try:
        directory = _multipart_dir(upload_id)
    except StorageError:
        return False
    if not directory.is_dir():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return True
