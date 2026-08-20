"""
Time-ordered UUID generation.

The API contract exposes UUID strings, but random uuid4 primary keys scatter
B-tree inserts across the whole index: every insert dirties a different page,
indexes bloat, and bulk uploads get progressively slower. UUIDv7 (RFC 9562) puts
a millisecond timestamp in the leading 48 bits, so inserts stay at the right
edge of the index like an autoincrement while still looking like a UUID to every
client.

The generator is also **monotonic**, which matters more than it first appears.
Item ordering is `priority, -created_at, -id`, so `id` is the final tiebreaker.
Two rows inserted in the same millisecond share a `created_at` — and on Windows
this is routine, not theoretical: twelve rapid inserts were measured producing
only eight distinct timestamps. With purely random low bits those rows would
order arbitrarily; with a monotonic counter in `rand_a` they order by insertion,
so "newest first" is exact and deep paging cannot skip or repeat a row.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid

# Python 3.14 ships uuid.uuid7(), which is monotonic per RFC 9562. Prefer it
# when available; the fallback below provides the same guarantees.
_stdlib_uuid7 = getattr(uuid, "uuid7", None)

# 12 bits of rand_a are used as a within-millisecond sequence counter.
_MAX_SEQUENCE = 0x0FFF

_lock = threading.Lock()
_last_timestamp_ms = -1
_sequence = 0


def _next_position() -> tuple[int, int]:
    """
    Reserve a (timestamp_ms, sequence) slot that is strictly greater than the last.

    Handles the three awkward cases explicitly:
      * same millisecond   -> advance the sequence
      * sequence exhausted -> borrow the next millisecond
      * clock went backwards (NTP step, VM migration) -> hold the previous
        timestamp and advance the sequence, so ids never regress
    """
    global _last_timestamp_ms, _sequence

    with _lock:
        now_ms = time.time_ns() // 1_000_000

        if now_ms > _last_timestamp_ms:
            _last_timestamp_ms = now_ms
            _sequence = 0
        else:
            # Covers both now_ms == _last_timestamp_ms and a backwards clock.
            _sequence += 1
            if _sequence > _MAX_SEQUENCE:
                _last_timestamp_ms += 1
                _sequence = 0

        return _last_timestamp_ms, _sequence


def _uuid7_fallback() -> uuid.UUID:
    """
    RFC 9562 v7 layout:
        bytes 0-5   48-bit unix timestamp in milliseconds
        byte  6     version (4 bits) | sequence high nibble
        byte  7     sequence low byte
        byte  8     variant (2 bits) | random
        bytes 9-15  random
    """
    timestamp_ms, sequence = _next_position()
    random_bytes = secrets.token_bytes(8)

    raw = bytearray(16)
    raw[0:6] = timestamp_ms.to_bytes(6, "big")
    raw[6] = 0x70 | ((sequence >> 8) & 0x0F)  # version 7 + sequence high nibble
    raw[7] = sequence & 0xFF
    raw[8] = (random_bytes[0] & 0x3F) | 0x80  # variant 0b10 + random
    raw[9:16] = random_bytes[1:8]

    return uuid.UUID(bytes=bytes(raw))


def uuid7() -> uuid.UUID:
    """Return a time-ordered, monotonically increasing UUID for use as a pk."""
    if _stdlib_uuid7 is not None:
        return _stdlib_uuid7()
    return _uuid7_fallback()
