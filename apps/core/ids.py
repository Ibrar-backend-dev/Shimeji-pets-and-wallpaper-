"""
Time-ordered UUID generation.

The API contract exposes UUID strings, but random uuid4 primary keys scatter
B-tree inserts across the whole index: every insert dirties a different page,
indexes bloat, and bulk uploads get progressively slower. UUIDv7 (RFC 9562)
puts a millisecond timestamp in the leading 48 bits, so inserts stay at the
right edge of the index like an autoincrement while still looking like a UUID
to every client.

A useful side effect: `ORDER BY id` is a valid creation-time tiebreaker, which
is exactly what the list ordering relies on.
"""

from __future__ import annotations

import secrets
import time
import uuid

# Python 3.14 ships uuid.uuid7() with in-process monotonicity guarantees.
# Prefer it when present; the fallback below is the same RFC 9562 layout.
_stdlib_uuid7 = getattr(uuid, "uuid7", None)


def _uuid7_fallback() -> uuid.UUID:
    """RFC 9562 v7: 48-bit unix_ts_ms | ver 7 | rand_a | variant | rand_b."""
    timestamp_ms = time.time_ns() // 1_000_000
    raw = bytearray(timestamp_ms.to_bytes(6, "big") + secrets.token_bytes(10))
    raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80  # variant 0b10
    return uuid.UUID(bytes=bytes(raw))


def uuid7() -> uuid.UUID:
    """Return a time-ordered UUID suitable for use as a primary key."""
    if _stdlib_uuid7 is not None:
        return _stdlib_uuid7()
    return _uuid7_fallback()
