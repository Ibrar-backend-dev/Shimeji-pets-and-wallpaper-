"""Permission classes for the public and admin surfaces."""

from __future__ import annotations

from django.conf import settings
from django.utils.crypto import constant_time_compare
from rest_framework.permissions import BasePermission

from .authentication import API_KEY_HEADER


class HasAppKey(BasePermission):
    """
    Requires a valid X-API-Key, resolved by AppKeyMiddleware.

    In DEBUG the key may be omitted so the API is explorable from a browser
    during development. That relaxation is gated on settings.DEBUG, which prod.py
    pins to False and refuses to start otherwise.
    """

    message = f"A valid {API_KEY_HEADER} header is required."

    def has_permission(self, request, view) -> bool:
        if getattr(request, "app_client", None) is not None:
            return True
        if settings.DEBUG and not getattr(settings, "REQUIRE_API_KEY_IN_DEBUG", False):
            return True
        # Staff sessions can browse the public API too, which keeps the admin
        # preview links working without minting a key.
        user = getattr(request, "user", None)
        return bool(user and getattr(user, "is_staff", False))


class HasCronSecret(BasePermission):
    """
    Guards the internal cron endpoints with a shared secret.

    Compared in constant time, and a blank configured secret denies everything
    rather than allowing everything — the failure mode of a missing env var must
    be a closed door.
    """

    message = "Invalid or missing cron secret."

    def has_permission(self, request, view) -> bool:
        expected = getattr(settings, "CRON_SECRET", "") or ""
        if len(expected) < 8:
            return False
        provided = request.META.get("HTTP_X_CRON_SECRET", "")
        return constant_time_compare(provided, expected)
