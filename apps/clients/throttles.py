"""
Throttling.

Rates come from the AppClient row rather than settings, so one noisy app can be
tightened without a redeploy and without affecting the others. Requests with no
key fall back to a conservative per-IP rate.
"""

from __future__ import annotations

from rest_framework.throttling import SimpleRateThrottle


class AppClientRateThrottle(SimpleRateThrottle):
    """
    Per-client limit, read from AppClient.rate_limit_per_min.

    SimpleRateThrottle normally parses a static "N/min" string once at import
    time; here the numerator is per-client, so the rate is resolved per request.
    """

    scope = "public_read"

    def get_cache_key(self, request, view) -> str | None:  # noqa: ANN001
        client = getattr(request, "app_client", None)
        if client is None:
            return None  # AnonBurstThrottle handles keyless traffic.
        return f"throttle:client:{client.pk}"

    def allow_request(self, request, view) -> bool:  # noqa: ANN001
        client = getattr(request, "app_client", None)
        if client is None:
            return True

        limit = int(client.rate_limit_per_min or 0)
        if limit <= 0:
            return True  # 0 means "unlimited" for this client.

        self.num_requests = limit
        self.duration = 60
        self.rate = f"{limit}/min"
        return super().allow_request(request, view)


class AnonBurstThrottle(SimpleRateThrottle):
    """Per-IP ceiling for requests that carry no valid key (DEBUG, admin preview)."""

    scope = "public_read"

    def get_cache_key(self, request, view) -> str | None:  # noqa: ANN001
        if getattr(request, "app_client", None) is not None:
            return None  # AppClientRateThrottle owns this request.
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class IngestRateThrottle(SimpleRateThrottle):
    """
    Tighter limit for the staff ingest endpoints.

    Keyed on the staff user so one admin cannot exhaust the presign path for
    everyone else, and so a runaway bulk-upload script trips a limit instead of
    filling the bucket.
    """

    scope = "ingest"

    def get_cache_key(self, request, view) -> str | None:  # noqa: ANN001
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return self.cache_format % {
                "scope": self.scope,
                "ident": self.get_ident(request),
            }
        return f"throttle:ingest:{user.pk}"
