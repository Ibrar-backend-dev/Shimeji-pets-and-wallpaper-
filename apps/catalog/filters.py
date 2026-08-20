"""
Query-parameter parsing for the item list endpoints.

Hand-written rather than django-filter for three reasons: every rejection can
carry a message shaped for the error envelope, `ordering` can be a strict
whitelist instead of anything the ORM would accept, and the exact set of
supported parameters is visible in one place.

Unrecognised parameters are ignored, so cache-busters and analytics tags do not
break a client. Recognised parameters are validated strictly: a bad value is a
400 explaining what was wrong, never a silently empty page.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from django.db.models import Q, QuerySet
from django.utils.dateparse import parse_datetime

from apps.core.exceptions import InvalidQueryParams

from .models import Category, ItemStatus, MediaType, Orientation, Resolution, Subcategory

# Public ordering name -> the ORM fields it maps to. Raw user input never
# reaches order_by(); anything outside this mapping is a 400.
ORDERING_MAP: dict[str, tuple[str, ...]] = {
    "priority": ("priority", "-created_at", "-id"),
    "-priority": ("-priority", "-created_at", "-id"),
    "newest": ("-created_at", "-id"),
    "oldest": ("created_at", "id"),
    "name": ("name", "priority", "-created_at"),
    "-name": ("-name", "priority", "-created_at"),
}
DEFAULT_ORDERING = "priority"

# ?cursor= is keyset paging over the default ordering only. Allowing it with an
# arbitrary ordering would silently return wrong pages rather than an error.
CURSOR_COMPATIBLE_ORDERING = {DEFAULT_ORDERING}

_TRUE = {"true", "1", "yes", "y", "on"}
_FALSE = {"false", "0", "no", "n", "off"}

MAX_QUERY_LENGTH = 100
MAX_LIST_VALUES = 20


def _fail(param: str, message: str, detail: str | None = None) -> None:
    raise InvalidQueryParams(
        message=message, errors={param: [detail or message]}
    )


def parse_bool(raw: str, param: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    _fail(
        param,
        f"'{param}' must be a boolean.",
        f"'{raw}' is not a boolean. Use true or false.",
    )
    raise AssertionError  # unreachable; keeps type checkers happy


def parse_uuid(raw: str, param: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw.strip())
    except (ValueError, AttributeError, TypeError):
        _fail(param, f"'{param}' must be a UUID.", f"'{raw}' is not a valid UUID.")
    raise AssertionError


def parse_csv_choices(
    raw: str, param: str, allowed: set[str], *, upper: bool = True
) -> list[str]:
    """Split a comma list and check every element against `allowed`."""
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if not values:
        _fail(param, f"'{param}' must not be empty.")
    if len(values) > MAX_LIST_VALUES:
        _fail(
            param,
            f"'{param}' accepts at most {MAX_LIST_VALUES} values.",
            f"Received {len(values)}.",
        )

    normalised = [v.upper() if upper else v.lower() for v in values]
    unknown = [v for v in normalised if v not in allowed]
    if unknown:
        _fail(
            param,
            f"'{param}' contains unsupported values.",
            f"Unsupported: {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(allowed))}.",
        )
    # De-duplicate while keeping order stable, so the SQL is cache-key stable.
    return list(dict.fromkeys(normalised))


@dataclass
class ItemFilterResult:
    queryset: QuerySet
    ordering_name: str = DEFAULT_ORDERING
    applied: dict[str, object] = field(default_factory=dict)


class MediaItemFilter:
    """
    Applies the supported filters to a published MediaItem queryset.

    `feature_slugs` scopes the result to the types the route and the API key
    together permit; it is resolved by the view, never taken from the client.
    """

    def __init__(self, query_params, feature_slugs: list[str] | None = None) -> None:  # noqa: ANN001
        self.params = query_params
        self.feature_slugs = feature_slugs or []

    # -- individual filters ------------------------------------------------- #

    def _filter_category(self, qs: QuerySet, applied: dict) -> QuerySet:
        raw = self.params.get("category_id")
        if not raw:
            return qs
        category_id = parse_uuid(raw, "category_id")

        # Checking existence separately turns "id from another type" into an
        # explicit 404-style message rather than an empty list the client has to
        # guess about.
        category = (
            Category.objects.filter(pk=category_id, is_active=True)
            .select_related("feature")
            .first()
        )
        if category is None:
            _fail(
                "category_id",
                "Unknown category.",
                f"No active category with id {category_id}.",
            )
        if self.feature_slugs and category.feature.slug not in self.feature_slugs:
            _fail(
                "category_id",
                "Category does not belong to this content type.",
                f"Category {category_id} belongs to '{category.feature.slug}'.",
            )
        applied["category_id"] = str(category_id)
        return qs.filter(category_id=category_id)

    def _filter_subcategory(self, qs: QuerySet, applied: dict) -> QuerySet:
        raw = self.params.get("subcategory_id")
        if not raw:
            return qs
        subcategory_id = parse_uuid(raw, "subcategory_id")

        subcategory = (
            Subcategory.objects.filter(pk=subcategory_id, is_active=True)
            .select_related("category__feature")
            .first()
        )
        if subcategory is None:
            _fail(
                "subcategory_id",
                "Unknown subcategory.",
                f"No active subcategory with id {subcategory_id}.",
            )
        if (
            self.feature_slugs
            and subcategory.category.feature.slug not in self.feature_slugs
        ):
            _fail(
                "subcategory_id",
                "Subcategory does not belong to this content type.",
            )
        # If both were supplied they must agree, otherwise the client has built a
        # contradictory query and an empty page would hide the mistake.
        category_raw = self.params.get("category_id")
        if category_raw:
            category_id = parse_uuid(category_raw, "category_id")
            if subcategory.category_id != category_id:
                _fail(
                    "subcategory_id",
                    "Subcategory does not belong to the requested category.",
                    f"Subcategory {subcategory_id} belongs to "
                    f"category {subcategory.category_id}.",
                )
        applied["subcategory_id"] = str(subcategory_id)
        return qs.filter(subcategory_id=subcategory_id)

    def _filter_choice_list(
        self, qs: QuerySet, applied: dict, param: str, orm_field: str, allowed: set[str]
    ) -> QuerySet:
        raw = self.params.get(param)
        if not raw:
            return qs
        values = parse_csv_choices(raw, param, allowed)
        applied[param] = values
        return qs.filter(**{f"{orm_field}__in": values})

    def _filter_bool(
        self, qs: QuerySet, applied: dict, param: str, orm_field: str
    ) -> QuerySet:
        raw = self.params.get(param)
        if raw is None or raw == "":
            return qs
        value = parse_bool(raw, param)
        applied[param] = value
        return qs.filter(**{orm_field: value})

    def _filter_tags(self, qs: QuerySet, applied: dict) -> QuerySet:
        raw = self.params.get("tags")
        if not raw:
            return qs
        slugs = [s.strip().lower() for s in raw.split(",") if s.strip()]
        if not slugs:
            return qs
        if len(slugs) > MAX_LIST_VALUES:
            _fail("tags", f"'tags' accepts at most {MAX_LIST_VALUES} values.")
        applied["tags"] = slugs
        # OR semantics across tags. distinct() because the join can duplicate rows.
        return qs.filter(tags__slug__in=slugs).distinct()

    def _filter_search(self, qs: QuerySet, applied: dict) -> QuerySet:
        raw = (self.params.get("q") or "").strip()
        if not raw:
            return qs
        if len(raw) > MAX_QUERY_LENGTH:
            _fail(
                "q",
                f"'q' must be at most {MAX_QUERY_LENGTH} characters.",
                f"Received {len(raw)} characters.",
            )
        applied["q"] = raw
        return qs.filter(Q(name__icontains=raw) | Q(tags__slug__icontains=raw)).distinct()

    def _filter_updated_since(self, qs: QuerySet, applied: dict) -> QuerySet:
        raw = (self.params.get("updated_since") or "").strip()
        if not raw:
            return qs
        parsed = parse_datetime(raw)
        if parsed is None:
            _fail(
                "updated_since",
                "'updated_since' must be an ISO-8601 timestamp.",
                f"'{raw}' could not be parsed. Example: 2026-08-20T04:51:49Z",
            )
        applied["updated_since"] = raw
        return qs.filter(updated_at__gt=parsed)

    # -- ordering ----------------------------------------------------------- #

    def _resolve_ordering(self) -> str:
        raw = (self.params.get("ordering") or DEFAULT_ORDERING).strip()
        if raw not in ORDERING_MAP:
            _fail(
                "ordering",
                "'ordering' is not a supported value.",
                f"Allowed: {', '.join(sorted(ORDERING_MAP))}.",
            )
        if self.params.get("cursor") and raw not in CURSOR_COMPATIBLE_ORDERING:
            _fail(
                "cursor",
                "'cursor' paging requires the default ordering.",
                f"Drop 'ordering' or set it to '{DEFAULT_ORDERING}', "
                "or page with 'skip' instead.",
            )
        return raw

    # -- entry point -------------------------------------------------------- #

    def apply(self, queryset: QuerySet) -> ItemFilterResult:
        applied: dict[str, object] = {}
        qs = queryset

        if self.feature_slugs:
            qs = qs.filter(feature__slug__in=self.feature_slugs)
            applied["type"] = list(self.feature_slugs)

        qs = self._filter_category(qs, applied)
        qs = self._filter_subcategory(qs, applied)
        qs = self._filter_choice_list(
            qs, applied, "media_type", "media_type", set(MediaType.values)
        )
        qs = self._filter_choice_list(
            qs, applied, "orientation", "orientation", set(Orientation.values)
        )
        qs = self._filter_choice_list(
            qs, applied, "resolution", "resolution", set(Resolution.values)
        )
        qs = self._filter_bool(qs, applied, "is_live", "is_live")
        qs = self._filter_bool(qs, applied, "premium", "premium")
        qs = self._filter_tags(qs, applied)
        qs = self._filter_search(qs, applied)
        qs = self._filter_updated_since(qs, applied)

        ordering_name = self._resolve_ordering()
        qs = qs.order_by(*ORDERING_MAP[ordering_name])

        return ItemFilterResult(queryset=qs, ordering_name=ordering_name, applied=applied)


def parse_type_param(raw: str | None, valid_slugs: set[str]) -> list[str]:
    """
    Parse `?type=` on the mixed feed. Empty means "every type the key allows".
    """
    if not raw:
        return []
    return parse_csv_choices(raw, "type", valid_slugs, upper=False)


__all__ = [
    "CURSOR_COMPATIBLE_ORDERING",
    "DEFAULT_ORDERING",
    "ORDERING_MAP",
    "ItemFilterResult",
    "ItemStatus",
    "MediaItemFilter",
    "parse_bool",
    "parse_type_param",
    "parse_uuid",
]
