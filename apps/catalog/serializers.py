"""
Read serializers for the public API.

Field names and nesting follow the agreed contract exactly, so a client written
against the reference response keeps working:

    id, name, category_id, category_name, subcategory_id, subcategory_name,
    premium, preview_url, image_url, priority, created_at

with `type`, `media_type`, `is_live`, `width`, `height`, `file_bytes`,
`duration_ms`, `orientation`, `resolution`, `dominant_color` and `tags` added.
`image_url` carries whatever the asset is — png, mp4 or a Shimeji zip — and
`media_type` tells the app how to render it.

No URL is ever persisted: every one is composed from MEDIA_CDN_BASE_URL at
serialization time, so changing CDN host is an env var, not a migration.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.ingest.storage import public_url

from .models import Category, Feature, MediaItem, Subcategory

# The reference emits naive-UTC microsecond timestamps ("2026-08-20T04:51:49.002715").
# Matching it exactly avoids a parser change on three shipped Android clients.
REFERENCE_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


class _CDNUrlField(serializers.Field):
    """Renders a stored object key as its public CDN URL."""

    def __init__(self, key_attr: str, **kwargs) -> None:
        self.key_attr = key_attr
        kwargs.setdefault("read_only", True)
        kwargs.setdefault("source", "*")
        super().__init__(**kwargs)

    def to_representation(self, instance) -> str:
        return public_url(getattr(instance, self.key_attr, "") or "")


class FeatureSerializer(serializers.ModelSerializer):
    """A content type. `slug` is exposed as `type` to match the contract."""

    type = serializers.CharField(source="slug", read_only=True)

    class Meta:
        model = Feature
        fields = ("id", "type", "name", "description", "priority")
        read_only_fields = fields


class CategorySerializer(serializers.ModelSerializer):
    type = serializers.CharField(source="feature.slug", read_only=True)
    thumbnail = _CDNUrlField("thumbnail_key")

    class Meta:
        model = Category
        fields = (
            "id",
            "name",
            "type",
            "thumbnail",
            "priority",
            "has_subcategories",
            "item_count",
        )
        read_only_fields = fields


class SubcategorySerializer(serializers.ModelSerializer):
    type = serializers.CharField(source="category.feature.slug", read_only=True)
    category_id = serializers.UUIDField(source="category.id", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)
    thumbnail = _CDNUrlField("thumbnail_key")

    class Meta:
        model = Subcategory
        fields = (
            "id",
            "name",
            "type",
            "category_id",
            "category_name",
            "thumbnail",
            "priority",
            "item_count",
        )
        read_only_fields = fields


class MediaItemSerializer(serializers.ModelSerializer):
    """
    One asset row.

    Every relation it reads is resolved by MediaItemQuerySet.for_api(), so
    serializing a page of 20 costs no extra queries. tests/test_api_items.py
    pins that with assertNumQueries.
    """

    type = serializers.CharField(source="feature.slug", read_only=True)

    category_id = serializers.UUIDField(source="category.id", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)
    # Nullable third level: null on both fields when the item sits directly in
    # its category, which is what the reference response shows.
    subcategory_id = serializers.UUIDField(
        source="subcategory.id", read_only=True, allow_null=True
    )
    subcategory_name = serializers.CharField(
        source="subcategory.name", read_only=True, allow_null=True
    )

    preview_url = _CDNUrlField("preview_key")
    image_url = _CDNUrlField("file_key")

    tags = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(format=REFERENCE_DATETIME_FORMAT, read_only=True)

    class Meta:
        model = MediaItem
        fields = (
            "id",
            "name",
            "type",
            "category_id",
            "category_name",
            "subcategory_id",
            "subcategory_name",
            "premium",
            "preview_url",
            "image_url",
            "media_type",
            "is_live",
            "width",
            "height",
            "file_bytes",
            "duration_ms",
            "orientation",
            "resolution",
            "dominant_color",
            "tags",
            "priority",
            "created_at",
        )
        read_only_fields = fields

    def get_tags(self, obj: MediaItem) -> list[str]:
        # Uses the prefetched rows; .all() here would defeat prefetch_related.
        return [tag.slug for tag in obj.tags.all()]


class ManifestSerializer(serializers.Serializer):
    """
    Bootstrap payload.

    Exists so app launch is one request instead of one per type plus one per
    category. It is also the most cacheable response in the system.
    """

    config_version = serializers.CharField()
    types = serializers.ListField(child=serializers.DictField())
