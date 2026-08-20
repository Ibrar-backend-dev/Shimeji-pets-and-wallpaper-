"""Helper for writing audit rows without repeating request plumbing in each view."""

from __future__ import annotations

import logging
from typing import Any

from .models import AuditLog

logger = logging.getLogger("core.audit")


def client_ip(request: Any) -> str | None:
    """
    Best-effort client IP.

    Behind Render/Railway/Cloudflare the peer address is the proxy, so the left-
    most X-Forwarded-For entry is used when present. That header is client-
    supplied and therefore only ever used for logging, never for authorisation.
    """
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        candidate = forwarded.split(",")[0].strip()
        if candidate:
            return candidate[:45]
    return request.META.get("REMOTE_ADDR") or None


def record(
    action: str,
    *,
    request: Any = None,
    object_type: str = "",
    object_id: str = "",
    payload: dict[str, Any] | None = None,
) -> AuditLog | None:
    """
    Write an audit row.

    Auditing must never be the reason a successful operation reports failure, so
    any error here is logged and swallowed.
    """
    user = getattr(request, "user", None)
    actor = user if (user is not None and getattr(user, "is_authenticated", False)) else None

    try:
        return AuditLog.objects.create(
            actor=actor,
            actor_label=(getattr(actor, "get_username", lambda: "")() or "")[:150],
            action=action,
            object_type=object_type[:64],
            object_id=str(object_id)[:64],
            payload=payload or {},
            ip=client_ip(request),
            request_id=(getattr(request, "request_id", "") or "")[:64],
        )
    except Exception:  # pragma: no cover
        logger.exception(
            "Failed to write audit row", extra={"event": "audit_write_failed", "action": action}
        )
        return None
