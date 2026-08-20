"""
Health probes.

Deliberately plain Django views, outside DRF and outside the response envelope:
a load balancer wants a status code and a tiny body, not a parsed contract. They
are also excluded from the access log (see AccessLogMiddleware) because probe
traffic otherwise buries real requests.

  /healthz  liveness  — is the process up? No I/O, so it never fails on a
                        sleeping database and never triggers a needless restart.
  /readyz   readiness — can we actually serve? Checks Postgres and B2, and
                        caches the verdict so probes cannot become a load source.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

logger = logging.getLogger("core.health")

_READY_CACHE_KEY = "health:ready"
_READY_CACHE_TTL = 30


@require_GET
@never_cache
def healthz(_request: HttpRequest) -> JsonResponse:
    """Liveness. Intentionally does no I/O at all."""
    return JsonResponse({"status": "ok"})


@require_GET
@never_cache
def readyz(_request: HttpRequest) -> JsonResponse:
    """Readiness: database reachable, and B2 configured and answering."""
    cached = cache.get(_READY_CACHE_KEY)
    if cached is not None:
        return JsonResponse(cached, status=200 if cached["status"] == "ok" else 503)

    checks: dict[str, str] = {}

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception as exc:
        logger.error(
            "Readiness: database check failed",
            extra={"event": "health_db_failed", "error": str(exc)},
        )
        checks["database"] = "error"

    from apps.ingest.storage import storage_health

    checks["storage"] = storage_health()

    # "unconfigured" is expected during local development, where no B2 keys
    # exist. In production prod.py guarantees DEBUG is False, so an unconfigured
    # bucket correctly reports the service as not ready to serve uploads.
    def _acceptable(name: str, value: str) -> bool:
        if value == "ok":
            return True
        return name == "storage" and value == "unconfigured" and settings.DEBUG

    healthy = all(_acceptable(name, value) for name, value in checks.items())
    payload = {"status": "ok" if healthy else "degraded", "checks": checks}
    cache.set(_READY_CACHE_KEY, payload, _READY_CACHE_TTL)
    return JsonResponse(payload, status=200 if payload["status"] == "ok" else 503)
