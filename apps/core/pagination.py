"""
Offset pagination, shaped to the API contract.

    "data": {"items": [...], "total": 69, "skip": 0, "limit": 20}

Offset paging is what the Android clients expect, so it is what ships. It has
two real costs, and both are handled here rather than left to bite later:

1. `total` means a COUNT(*) on every page request. A user scrolling twenty pages
   would trigger twenty counts over the same filter, so counts are cached for
   API_COUNT_CACHE_SECONDS keyed by the compiled SQL of the filtered queryset.

2. A large OFFSET makes Postgres walk and discard every skipped row, so deep
   paging degrades to a scan. `skip` is therefore capped at API_MAX_SKIP with an
   explicit 400, and `?cursor=` offers keyset paging for anything deeper. The cap
   lands well past where a human scrolls while denying a scraper cheap full scans.

Invalid `skip`/`limit` values are rejected with a 400 rather than silently
clamped: a client asking for limit=500 should learn that it cannot, not receive
20 rows and quietly assume it got 500.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import EmptyResultSet
from django.db.models import Q, QuerySet
from django.utils.dateparse import parse_datetime
from rest_framework.pagination import BasePagination
from rest_framework.response import Response

from .exceptions import InvalidQueryParams


def _positive_int(raw: str, name: str) -> int:
    """Parse a non-negative integer or raise a 400 naming the offending param."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise InvalidQueryParams(
            message=f"'{name}' must be an integer.",
            errors={name: [f"'{raw}' is not an integer."]},
        ) from None
    if value < 0:
        raise InvalidQueryParams(
            message=f"'{name}' must not be negative.",
            errors={name: ["Must be >= 0."]},
        )
    return value


def cached_count(queryset: QuerySet) -> int:
    """
    COUNT(*) for a filtered queryset, memoised by the SQL it compiles to.

    Keying on the compiled SQL (not the request path) means two different
    endpoints that happen to produce the same filter share one cached count, and
    a change in any filter value produces a different key automatically.
    """
    ttl = getattr(settings, "API_COUNT_CACHE_SECONDS", 60)
    if ttl <= 0:
        return queryset.count()

    try:
        # Compile a clone: as_sql() can annotate the query it is given, and this
        # queryset is about to be sliced and executed for real.
        compiler = queryset.query.clone().get_compiler(using=queryset.db)
        sql, params = compiler.as_sql()
    except EmptyResultSet:
        # Django proved the queryset can match nothing (e.g. `id__in=[]`).
        return 0
    except Exception:  # pragma: no cover - never fail a request over a cache key
        return queryset.count()

    digest = hashlib.sha256(f"{sql}|{params!r}".encode()).hexdigest()[:32]
    key = f"count:{digest}"

    total = cache.get(key)
    if total is None:
        total = queryset.count()
        cache.set(key, total, ttl)
    return total


def _encode_cursor(values: dict[str, Any]) -> str:
    raw = json.dumps(values, default=str, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(token: str) -> dict[str, Any]:
    padded = token + "=" * (-len(token) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise InvalidQueryParams(
            message="'cursor' is not a valid cursor.",
            errors={"cursor": ["Malformed cursor. Omit it to start from the beginning."]},
        ) from None


class SkipLimitPagination(BasePagination):
    """
    `?skip=&limit=` by default; `?cursor=` for deep keyset paging.

    Views may override `cursor_fields` to match their own ordering. The default
    matches MediaItem: lower priority first, then newest first.
    """

    skip_query_param = "skip"
    limit_query_param = "limit"
    cursor_query_param = "cursor"

    # (field, descending) pairs, most significant first.
    cursor_fields: tuple[tuple[str, bool], ...] = (
        ("priority", False),
        ("created_at", True),
        ("id", True),
    )

    def __init__(self) -> None:
        self.total = 0
        self.skip = 0
        self.limit = 0
        self.cursor_token: str | None = None
        self.next_cursor: str | None = None
        self._page: list[Any] = []

    # -- parameter parsing ------------------------------------------------- #

    def _get_limit(self, request) -> int:  # noqa: ANN001
        default = getattr(settings, "API_PAGE_SIZE_DEFAULT", 20)
        maximum = getattr(settings, "API_PAGE_SIZE_MAX", 100)

        raw = request.query_params.get(self.limit_query_param)
        if raw is None:
            return default

        limit = _positive_int(raw, self.limit_query_param)
        if limit == 0:
            raise InvalidQueryParams(
                message=f"'{self.limit_query_param}' must be at least 1.",
                errors={self.limit_query_param: ["Must be >= 1."]},
            )
        if limit > maximum:
            raise InvalidQueryParams(
                message=f"'{self.limit_query_param}' must not exceed {maximum}.",
                errors={self.limit_query_param: [f"Must be <= {maximum}."]},
            )
        return limit

    def _get_skip(self, request) -> int:  # noqa: ANN001
        raw = request.query_params.get(self.skip_query_param)
        if raw is None:
            return 0

        skip = _positive_int(raw, self.skip_query_param)
        max_skip = getattr(settings, "API_MAX_SKIP", 10_000)
        if skip > max_skip:
            raise InvalidQueryParams(
                message=(
                    f"'{self.skip_query_param}' must not exceed {max_skip}. "
                    "Use 'cursor' for deeper paging."
                ),
                errors={
                    self.skip_query_param: [f"Must be <= {max_skip}."],
                    "hint": ["Pass the 'next_cursor' from the previous page as 'cursor'."],
                },
            )
        return skip

    # -- keyset paging ----------------------------------------------------- #

    def _apply_cursor(self, queryset: QuerySet, token: str) -> QuerySet:
        """
        Narrow the queryset to rows strictly after the cursor position.

        For ordering (a ASC, b DESC, c DESC) the predicate is
            a > a0 OR (a = a0 AND (b < b0 OR (b = b0 AND c < c0)))
        built from the innermost comparison outwards.
        """
        position = _decode_cursor(token)

        missing = [name for name, _ in self.cursor_fields if name not in position]
        if missing:
            raise InvalidQueryParams(
                message="'cursor' does not match this endpoint's ordering.",
                errors={"cursor": [f"Missing keys: {', '.join(missing)}."]},
            )

        predicate = Q()
        for field, descending in reversed(self.cursor_fields):
            value = position[field]
            if field == "created_at" and isinstance(value, str):
                value = parse_datetime(value) or value
            strict = Q(**{f"{field}__{'lt' if descending else 'gt'}": value})
            predicate = strict | (Q(**{field: value}) & predicate) if predicate else strict

        return queryset.filter(predicate)

    def _build_next_cursor(self, last_obj: Any) -> str:
        return _encode_cursor(
            {field: getattr(last_obj, field) for field, _ in self.cursor_fields}
        )

    # -- BasePagination ---------------------------------------------------- #

    def paginate_queryset(self, queryset: QuerySet, request, view=None):  # noqa: ANN001
        self.limit = self._get_limit(request)
        self.cursor_token = request.query_params.get(self.cursor_query_param)

        # `total` is part of the contract in both modes, and is computed against
        # the unpaginated queryset so it does not shift as the client pages.
        self.total = cached_count(queryset)

        if self.cursor_token:
            self.skip = 0
            window = list(self._apply_cursor(queryset, self.cursor_token)[: self.limit + 1])
            has_more = len(window) > self.limit
            self._page = window[: self.limit]
            self.next_cursor = (
                self._build_next_cursor(self._page[-1]) if has_more and self._page else None
            )
        else:
            self.skip = self._get_skip(request)
            self._page = list(queryset[self.skip : self.skip + self.limit])
            self.next_cursor = None

        return self._page

    def get_paginated_response(self, data) -> Response:  # noqa: ANN001
        if self.cursor_token:
            # Cursor mode reports its own position instead of an offset.
            body = {
                "items": data,
                "total": self.total,
                "limit": self.limit,
                "cursor": self.cursor_token,
                "next_cursor": self.next_cursor,
            }
        else:
            # Exactly the four keys the reference contract specifies.
            body = {
                "items": data,
                "total": self.total,
                "skip": self.skip,
                "limit": self.limit,
            }
        return Response(body)

    def get_paginated_response_schema(self, schema: dict) -> dict:
        return {
            "type": "object",
            "properties": {
                "items": schema,
                "total": {"type": "integer", "example": 69},
                "skip": {"type": "integer", "example": 0},
                "limit": {"type": "integer", "example": 20},
            },
        }
