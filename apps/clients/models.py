"""
API clients.

Each Android app gets its own key. That buys three things a shared secret does
not: traffic is attributable per app, one app can be throttled or revoked
without touching the others, and a key can be scoped so the wallpaper app cannot
enumerate Shimeji packs.

Keys are stored hashed. A leaked database dump therefore does not hand over
working credentials.
"""

from __future__ import annotations

import hashlib
import secrets

from django.db import models
from django.utils import timezone

from apps.catalog.models import Feature
from apps.core.models import TimeStampedModel

KEY_PREFIX_LENGTH = 12
_SECRET_BYTES = 32


def hash_key(raw_key: str) -> str:
    """
    SHA-256 of the raw key.

    Deliberately not a password hasher: these are 256-bit random secrets, not
    human-chosen passwords, so there is nothing to brute-force and the cost of a
    slow KDF would land on every single API request.
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_key(prefix_hint: str = "app") -> tuple[str, str, str]:
    """
    Mint a new key.

    Returns (raw_key, key_prefix, key_hash). The raw key is shown once and never
    stored; only its prefix and hash are persisted.
    """
    cleaned = "".join(ch for ch in prefix_hint.lower() if ch.isalnum())[:6] or "app"
    body = secrets.token_urlsafe(_SECRET_BYTES)
    raw_key = f"{cleaned}_{body}"
    return raw_key, raw_key[:KEY_PREFIX_LENGTH], hash_key(raw_key)


class AppClient(TimeStampedModel):
    """A credential belonging to one consuming application."""

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=64, unique=True)

    # Indexed prefix turns authentication into one indexed lookup rather than a
    # scan over every key row comparing hashes.
    key_prefix = models.CharField(max_length=KEY_PREFIX_LENGTH, unique=True, db_index=True)
    key_hash = models.CharField(max_length=64, editable=False)

    is_active = models.BooleanField(default=True)

    allowed_features = models.ManyToManyField(
        Feature,
        blank=True,
        related_name="app_clients",
        help_text="Types this key may read. Leave empty to allow all.",
    )
    rate_limit_per_min = models.PositiveIntegerField(default=120)

    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ("name",)
        indexes = [
            models.Index(fields=["key_prefix", "is_active"], name="appclient_prefix_act_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.key_prefix}…)"

    def set_key(self, raw_key: str) -> None:
        self.key_prefix = raw_key[:KEY_PREFIX_LENGTH]
        self.key_hash = hash_key(raw_key)

    def touch(self) -> None:
        """Record last use without triggering a full save."""
        now = timezone.now()
        AppClient.objects.filter(pk=self.pk).update(last_used_at=now)
        self.last_used_at = now
