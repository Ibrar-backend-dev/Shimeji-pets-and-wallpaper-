"""
Local-media endpoints. Mounted only when MEDIA_LOCAL_STORAGE is on.

These stand in for the two things B2 provides and a filesystem does not: a URL a
client can PUT to, and a URL a client can GET from. Plain Django views rather
than DRF ones — the bodies are raw bytes, not JSON, and DRF's parsers would try
to interpret them.

Authorisation is the signed token minted by `local_storage.presign_put`, exactly
as a presigned S3 URL carries its own authority. There is deliberately no
session or JWT check: the admin browser PUTs straight here, and requiring a
second credential would diverge from how the B2 path behaves.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseNotAllowed,
    JsonResponse,
)
from django.views.decorators.csrf import csrf_exempt

from . import local_storage
from .storage import StorageError

logger = logging.getLogger("storage.local")


def _enabled_or_404() -> None:
    """
    Second gate.

    urls.py already declines to mount these when the flag is off; this makes a
    routing mistake fail closed rather than exposing an unauthenticated write
    endpoint against a production B2 deployment.
    """
    if not settings.MEDIA_LOCAL_STORAGE:
        raise Http404("Local media storage is not enabled.")


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"detail": message}, status=status)


@csrf_exempt
def upload_object(request: HttpRequest) -> HttpResponse:
    """PUT a whole object against a token from `presign_put`."""
    _enabled_or_404()
    if request.method != "PUT":
        return HttpResponseNotAllowed(["PUT"])

    token = request.GET.get("token", "")
    if not token:
        return _error("Missing upload token.")

    try:
        expires_in = int(request.GET.get("expires_in") or settings.PRESIGN_EXPIRY_SECONDS)
    except ValueError:
        expires_in = settings.PRESIGN_EXPIRY_SECONDS

    try:
        payload = local_storage.verify_upload_token(
            token, salt=local_storage.UPLOAD_SALT, max_age=expires_in
        )
    except StorageError as exc:
        return _error(str(exc), status=403)

    body = request.body

    # The signature covers content type and length, so both must match what was
    # presigned. This is what stops a presign issued for a 2 MB PNG being
    # redeemed for something larger or of a different type — the same guarantee
    # B2 gets by putting them in SignedHeaders.
    declared_type = (request.headers.get("Content-Type") or "").split(";")[0].strip()
    if declared_type and declared_type != payload["content_type"]:
        return _error(
            f"Content-Type {declared_type!r} does not match the presigned "
            f"{payload['content_type']!r}."
        )
    if len(body) != int(payload["content_length"]):
        return _error(
            f"Body is {len(body)} bytes, but {payload['content_length']} was presigned."
        )

    try:
        meta = local_storage.put_bytes(
            key=payload["key"], data=body, content_type=payload["content_type"]
        )
    except StorageError as exc:
        logger.error(
            "Local upload failed",
            extra={"event": "local_put_failed", "key": payload.get("key"), "error": str(exc)},
        )
        return _error("Could not store the upload.", status=500)

    response = HttpResponse(status=200)
    response["ETag"] = f'"{meta.etag}"'
    return response


@csrf_exempt
def upload_part(request: HttpRequest) -> HttpResponse:
    """PUT one part of a multipart upload against a token from `presign_upload_part`."""
    _enabled_or_404()
    if request.method != "PUT":
        return HttpResponseNotAllowed(["PUT"])

    token = request.GET.get("token", "")
    if not token:
        return _error("Missing upload token.")

    try:
        payload = local_storage.verify_upload_token(
            token,
            salt=local_storage.PART_SALT,
            max_age=settings.MULTIPART_SESSION_EXPIRY_SECONDS,
        )
    except StorageError as exc:
        return _error(str(exc), status=403)

    try:
        etag = local_storage.write_part(
            upload_id=payload["upload_id"],
            part_number=payload["part_number"],
            data=request.body,
        )
    except StorageError as exc:
        return _error(str(exc), status=400)

    # The client collects these and sends them back on finalize, so the header
    # has to be here and has to be quoted the way S3 quotes it.
    response = HttpResponse(status=200)
    response["ETag"] = f'"{etag}"'
    return response


def serve_media(request: HttpRequest, key: str) -> HttpResponse:
    """
    GET a stored object. Stands in for the CDN.

    Django serving media is a development convenience and nothing more; in
    production this path does not exist and Cloudflare fronts B2 instead.
    """
    _enabled_or_404()
    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET", "HEAD"])

    try:
        meta = local_storage.head_object(key)
        path = local_storage._resolve(key)
    except StorageError as exc:
        # Covers both "not found" and a key that tried to escape the root. They
        # are deliberately indistinguishable from outside.
        logger.debug("Local media miss", extra={"key": key, "error": str(exc)})
        raise Http404("Not found.") from exc

    response = FileResponse(
        path.open("rb"), content_type=meta.content_type or "application/octet-stream"
    )
    response["Content-Length"] = str(meta.size)
    response["ETag"] = f'"{meta.etag}"'
    # Short, unlike the immutable CDN policy: local files get overwritten during
    # development and a long cache would hide that.
    response["Cache-Control"] = "public, max-age=60"
    return response
