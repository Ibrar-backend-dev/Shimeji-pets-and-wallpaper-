"""
Public read API.

Routes are named per type (`/wallpapers`, `/shimeji`, `/battery`) so each app
calls its own path and cannot be handed another type's content by forgetting a
query parameter. All three are the same view class with `feature_slug` bound,
so there is one queryset builder and one filter implementation, not three.

Every list response carries Cache-Control and an ETag. Behind a CDN that is what
lets the edge absorb the traffic instead of the free-tier dyno — see DEPLOY.md
for the required Cloudflare cache rule.
"""

from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.core.cache import cache
from django.db.models import Max, QuerySet
from django.utils.http import http_date, quote_etag
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.clients.permissions import HasAppKey
from apps.clients.throttles import AnonBurstThrottle, AppClientRateThrottle
from apps.core.exceptions import ResourceNotFound

from .filters import MediaItemFilter, parse_type_param, parse_uuid
from .models import Category, Feature, MediaItem, Subcategory
from .serializers import (
    CategorySerializer,
    MediaItemSerializer,
    SubcategorySerializer,
)

logger = logging.getLogger("catalog.api")


# --------------------------------------------------------------------------- #
# Shared behaviour
# --------------------------------------------------------------------------- #


def active_feature_slugs() -> list[str]:
    """Active type slugs, cached: three rows that change roughly never."""
    ttl = getattr(settings, "API_CONFIG_CACHE_SECONDS", 120)
    key = "catalog:feature_slugs"
    slugs = cache.get(key) if ttl else None
    if slugs is None:
        slugs = list(
            Feature.objects.filter(is_active=True)
            .order_by("priority", "name")
            .values_list("slug", flat=True)
        )
        if ttl:
            cache.set(key, slugs, ttl)
    return slugs


class PublicAPIView:
    """Mixin: API-key permission, per-client throttling, cache headers."""

    permission_classes = [HasAppKey]
    throttle_classes = [AppClientRateThrottle, AnonBurstThrottle]

    def allowed_feature_slugs(self, requested: list[str] | None = None) -> list[str]:
        """
        Intersect what the route asked for with what this key may read.

        Scoping happens here rather than in the filter so a key restricted to
        `wallpaper` cannot reach Shimeji content by any combination of query
        parameters.
        """
        available = requested if requested else active_feature_slugs()
        client = getattr(self.request, "app_client", None)
        if client is None:
            return list(available)
        return [slug for slug in available if client.may_read_feature(slug)]

    def apply_cache_headers(self, response: Response, last_modified=None) -> Response:
        """
        Cache-Control plus a weak validator.

        ETag is derived from the request path, query string and the newest
        `updated_at` in the result set, so any edit to any matching row changes
        the tag and clients revalidate instead of serving stale content.
        """
        max_age = getattr(settings, "API_LIST_CACHE_SECONDS", 300)
        if max_age > 0:
            response["Cache-Control"] = (
                f"public, max-age={max_age}, stale-while-revalidate=86400"
            )
        else:
            response["Cache-Control"] = "no-cache"

        if last_modified is not None:
            response["Last-Modified"] = http_date(last_modified.timestamp())

        request = self.request
        seed = "|".join(
            [
                request.path,
                request.META.get("QUERY_STRING", ""),
                str(last_modified.timestamp() if last_modified else ""),
            ]
        )
        # md5 here is a cache validator, not a security primitive: it only has to
        # change when the inputs change. usedforsecurity=False says so, and keeps
        # this working under a FIPS-restricted Python.
        digest = hashlib.md5(seed.encode(), usedforsecurity=False).hexdigest()
        response["ETag"] = quote_etag(digest)
        return response


# --------------------------------------------------------------------------- #
# Item list / detail
# --------------------------------------------------------------------------- #


class BaseMediaItemListView(PublicAPIView, GenericAPIView):
    """
    The shared list implementation.

    Subclasses set `feature_slug` (one type) or leave it None for the mixed feed.
    """

    serializer_class = MediaItemSerializer
    feature_slug: str | None = None
    success_message = "Items fetched successfully"

    def resolve_features(self) -> list[str]:
        if self.feature_slug:
            requested = [self.feature_slug]
        else:
            # Mixed feed: ?type= may narrow it, but only to known active slugs.
            requested = parse_type_param(
                self.request.query_params.get("type"), set(active_feature_slugs())
            )
        return self.allowed_feature_slugs(requested)

    def get_queryset(self) -> QuerySet:
        features = self.resolve_features()
        if not features:
            # The key is not permitted any of the requested types. An empty
            # queryset is the honest answer; 403 would leak which types exist.
            return MediaItem.objects.none()
        result = MediaItemFilter(self.request.query_params, features).apply(
            MediaItem.objects.for_api()
        )
        return result.queryset

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()

        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        response = self.get_paginated_response(serializer.data)

        # One cheap aggregate over the same filtered set gives a real validator.
        newest = queryset.aggregate(newest=Max("updated_at"))["newest"]
        return self.apply_cache_headers(response, newest)


class WallpaperListView(BaseMediaItemListView):
    feature_slug = Feature.WALLPAPER
    success_message = "Wallpapers fetched successfully"


class ShimejiListView(BaseMediaItemListView):
    feature_slug = Feature.SHIMEJI
    success_message = "Shimeji fetched successfully"


class BatteryListView(BaseMediaItemListView):
    feature_slug = Feature.BATTERY
    success_message = "Battery items fetched successfully"


class MixedItemListView(BaseMediaItemListView):
    """`/items` — the mixed feed across every type the key may read."""

    feature_slug = None
    success_message = "Items fetched successfully"


class MediaItemDetailView(PublicAPIView, GenericAPIView):
    """Single item, scoped to the route's type and the key's allowlist."""

    serializer_class = MediaItemSerializer
    feature_slug: str | None = None
    success_message = "Item fetched successfully"

    def get(self, request, pk, *args, **kwargs):
        item_id = parse_uuid(str(pk), "id")
        features = self.allowed_feature_slugs(
            [self.feature_slug] if self.feature_slug else None
        )

        item = (
            MediaItem.objects.for_api().filter(pk=item_id, feature__slug__in=features).first()
        )
        if item is None:
            raise ResourceNotFound(message="Item not found.")

        response = Response(self.get_serializer(item).data)
        return self.apply_cache_headers(response, item.updated_at)


class WallpaperDetailView(MediaItemDetailView):
    feature_slug = Feature.WALLPAPER
    success_message = "Wallpaper fetched successfully"


class ShimejiDetailView(MediaItemDetailView):
    feature_slug = Feature.SHIMEJI
    success_message = "Shimeji fetched successfully"


class BatteryDetailView(MediaItemDetailView):
    feature_slug = Feature.BATTERY
    success_message = "Battery item fetched successfully"


class MixedItemDetailView(MediaItemDetailView):
    feature_slug = None


class RelatedItemsView(PublicAPIView, GenericAPIView):
    """Other items sharing this one's subcategory, or failing that its category."""

    serializer_class = MediaItemSerializer
    success_message = "Related items fetched successfully"

    def get(self, request, pk, *args, **kwargs):
        item_id = parse_uuid(str(pk), "id")
        features = self.allowed_feature_slugs()

        item = (
            MediaItem.objects.published()
            .filter(pk=item_id, feature__slug__in=features)
            .only("id", "category_id", "subcategory_id")
            .first()
        )
        if item is None:
            raise ResourceNotFound(message="Item not found.")

        queryset = MediaItem.objects.for_api().exclude(pk=item.pk)
        if item.subcategory_id:
            queryset = queryset.filter(subcategory_id=item.subcategory_id)
        else:
            queryset = queryset.filter(category_id=item.category_id)

        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        response = self.get_paginated_response(serializer.data)
        newest = queryset.aggregate(newest=Max("updated_at"))["newest"]
        return self.apply_cache_headers(response, newest)


# --------------------------------------------------------------------------- #
# Categories / subcategories
# --------------------------------------------------------------------------- #


class CategoryListView(PublicAPIView, GenericAPIView):
    """`/categories?type=wallpaper`"""

    serializer_class = CategorySerializer
    success_message = "Categories fetched successfully"

    def get_queryset(self) -> QuerySet:
        requested = parse_type_param(
            self.request.query_params.get("type"), set(active_feature_slugs())
        )
        features = self.allowed_feature_slugs(requested)
        if not features:
            return Category.objects.none()
        return (
            Category.objects.filter(is_active=True, feature__slug__in=features)
            .select_related("feature")
            .order_by("priority", "name")
        )

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        response = self.get_paginated_response(serializer.data)
        newest = queryset.aggregate(newest=Max("updated_at"))["newest"]
        return self.apply_cache_headers(response, newest)


class SubcategoryListView(PublicAPIView, GenericAPIView):
    """`/subcategories?category_id=…` (or `?type=` for a whole type)."""

    serializer_class = SubcategorySerializer
    success_message = "Subcategories fetched successfully"

    def get_queryset(self) -> QuerySet:
        requested = parse_type_param(
            self.request.query_params.get("type"), set(active_feature_slugs())
        )
        features = self.allowed_feature_slugs(requested)
        if not features:
            return Subcategory.objects.none()

        queryset = (
            Subcategory.objects.filter(
                is_active=True,
                category__is_active=True,
                category__feature__slug__in=features,
            )
            .select_related("category__feature")
            .order_by("priority", "name")
        )

        raw_category = self.request.query_params.get("category_id")
        if raw_category:
            category_id = parse_uuid(raw_category, "category_id")
            queryset = queryset.filter(category_id=category_id)
        return queryset

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        response = self.get_paginated_response(serializer.data)
        newest = queryset.aggregate(newest=Max("updated_at"))["newest"]
        return self.apply_cache_headers(response, newest)


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


class ManifestView(PublicAPIView, APIView):
    """
    One bootstrap request: every type, its categories, and their subcategories.

    Without this an app launch costs one request per type plus one per category.
    Counts come from the denormalized `item_count` columns, never a live COUNT,
    and the whole payload is cached because it changes only when an admin edits
    the taxonomy.
    """

    success_message = "Manifest fetched successfully"

    def get(self, request, *args, **kwargs):
        features = self.allowed_feature_slugs()
        cache_key = "catalog:manifest:" + ",".join(sorted(features))
        ttl = getattr(settings, "API_CONFIG_CACHE_SECONDS", 120)

        payload = cache.get(cache_key) if ttl else None
        if payload is None:
            payload = self._build(features)
            if ttl:
                cache.set(cache_key, payload, ttl)

        response = Response(payload, status=status.HTTP_200_OK)
        return self.apply_cache_headers(response)

    @staticmethod
    def _build(feature_slugs: list[str]) -> dict:
        categories = (
            Category.objects.filter(is_active=True, feature__slug__in=feature_slugs)
            .select_related("feature")
            .prefetch_related("subcategories")
            .order_by("priority", "name")
        )

        by_feature: dict[str, list[dict]] = {slug: [] for slug in feature_slugs}
        newest_stamp = None

        for category in categories:
            subcategories = [
                {
                    "id": str(sub.id),
                    "name": sub.name,
                    "priority": sub.priority,
                    "item_count": sub.item_count,
                }
                for sub in sorted(
                    (s for s in category.subcategories.all() if s.is_active),
                    key=lambda s: (s.priority, s.name),
                )
            ]
            by_feature.setdefault(category.feature.slug, []).append(
                {
                    "id": str(category.id),
                    "name": category.name,
                    "priority": category.priority,
                    "has_subcategories": category.has_subcategories,
                    "item_count": category.item_count,
                    "subcategories": subcategories,
                }
            )
            if newest_stamp is None or category.updated_at > newest_stamp:
                newest_stamp = category.updated_at

        features = (
            Feature.objects.filter(is_active=True, slug__in=feature_slugs)
            .order_by("priority", "name")
            .values("id", "slug", "name", "priority")
        )

        types = [
            {
                "id": str(f["id"]),
                "type": f["slug"],
                "name": f["name"],
                "priority": f["priority"],
                "categories": by_feature.get(f["slug"], []),
            }
            for f in features
        ]

        # A stamp the client can compare to decide whether to refetch the taxonomy.
        version_seed = (
            "|".join(f"{t['type']}:{len(t['categories'])}" for t in types) + f"|{newest_stamp}"
        )
        return {
            "config_version": hashlib.md5(
                version_seed.encode(), usedforsecurity=False
            ).hexdigest()[:16],
            "types": types,
        }
