"""
X-API-Key resolution.

Reads are anonymous — there are no user accounts in this API — so a key
identifies an *application*, not a person. That makes it request context rather
than authentication, which is why resolution happens in middleware
(`apps.clients.middleware.AppKeyMiddleware`) and not in a DRF authenticator:

  * DRF authenticates lazily, on first touch of `request.user`. A permission
    class that only reads `request.app_client` would otherwise run before
    authentication had happened at all.
  * Attributes set on DRF's request wrapper never reach plain Django middleware,
    so the access log would lose the client name.

This module holds the resolution logic itself. The resolved client is cached, so
a valid request costs zero queries for auth, and `last_used_at` is written at
most once per APP_CLIENT_TOUCH_INTERVAL_SECONDS — a naive implementation turns
every read into a write, which is precisely what a free-tier Postgres cannot
absorb.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from .models import KEY_PREFIX_LENGTH, AppClient, hash_key

logger = logging.getLogger("clients.auth")

API_KEY_HEADER = "X-API-Key"
API_KEY_META = "HTTP_X_API_KEY"

_CACHE_TTL = 300
_NEGATIVE_CACHE_TTL = 30
_MISS = "MISS"


class ResolvedClient:
    """
    Lightweight, cacheable snapshot of an AppClient.

    Caching the model instance would pickle a queryset and its M2M; this holds
    only what a request actually needs.
    """

    __slots__ = (
        "pk",
        "name",
        "slug",
        "rate_limit_per_min",
        "allowed_feature_slugs",
        "key_hash",
    )

    def __init__(
        self,
        pk,  # noqa: ANN001
        name: str,
        slug: str,
        rate_limit_per_min: int,
        allowed_feature_slugs: frozenset[str],
        key_hash: str,
    ) -> None:
        self.pk = pk
        self.name = name
        self.slug = slug
        self.rate_limit_per_min = rate_limit_per_min
        self.allowed_feature_slugs = allowed_feature_slugs
        self.key_hash = key_hash

    def may_read_feature(self, feature_slug: str) -> bool:
        """An empty allowlist means every type is permitted."""
        if not self.allowed_feature_slugs:
            return True
        return feature_slug in self.allowed_feature_slugs

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ResolvedClient {self.slug}>"


def _cache_key(prefix: str) -> str:
    return f"appclient:{prefix}"


def resolve_client(key_prefix: str) -> ResolvedClient | None:
    """Look up an active client by key prefix, memoised in the cache."""
    cache_key = _cache_key(key_prefix)
    cached = cache.get(cache_key)
    if cached is not None:
        # Negative caching, so a flood of invalid keys cannot hammer the database.
        return None if cached == _MISS else cached

    client = (
        AppClient.objects.filter(key_prefix=key_prefix, is_active=True)
        .prefetch_related("allowed_features")
        .first()
    )
    if client is None:
        cache.set(cache_key, _MISS, _NEGATIVE_CACHE_TTL)
        return None

    resolved = ResolvedClient(
        pk=client.pk,
        name=client.name,
        slug=client.slug,
        rate_limit_per_min=client.rate_limit_per_min,
        allowed_feature_slugs=frozenset(
            f.slug for f in client.allowed_features.all() if f.is_active
        ),
        key_hash=client.key_hash,
    )
    cache.set(cache_key, resolved, _CACHE_TTL)
    return resolved


def invalidate_client_cache(key_prefix: str) -> None:
    """Called from admin/save hooks so a revoked key stops working immediately."""
    cache.delete(_cache_key(key_prefix))


def touch_client(resolved: ResolvedClient) -> None:
    """Update last_used_at at most once per interval, guarded by the cache."""
    interval = getattr(settings, "APP_CLIENT_TOUCH_INTERVAL_SECONDS", 600)
    if interval <= 0:
        return
    guard = f"appclient:touched:{resolved.pk}"
    # add() only succeeds when the key is absent, making this a cheap lock.
    if cache.add(guard, 1, interval):
        AppClient.objects.filter(pk=resolved.pk).update(last_used_at=timezone.now())


def resolve_from_request(request) -> ResolvedClient | None:  # noqa: ANN001
    """
    Validate the X-API-Key header on a plain Django request.

    Returns None for absent, malformed, unknown, inactive, or wrong-secret keys.
    The caller decides whether that is fatal; public endpoints treat it as 401.
    """
    raw_key = request.META.get(API_KEY_META, "").strip()
    if not raw_key:
        return None

    if len(raw_key) < KEY_PREFIX_LENGTH:
        logger.warning("Rejected malformed API key", extra={"event": "api_key_malformed"})
        return None

    prefix = raw_key[:KEY_PREFIX_LENGTH]
    resolved = resolve_client(prefix)
    if resolved is None:
        logger.warning(
            "Rejected unknown API key",
            extra={"event": "api_key_unknown", "key_prefix": prefix},
        )
        return None

    # Constant-time so response timing cannot leak the stored hash.
    if not constant_time_compare(hash_key(raw_key), resolved.key_hash):
        logger.warning(
            "Rejected API key with bad secret",
            extra={"event": "api_key_bad_secret", "app_client": resolved.slug},
        )
        return None

    return resolved
