"""
Request serializers for the staff ingest endpoints.

Write payloads are declared field by field — never `fields = "__all__"` — so a
client cannot reach a column the endpoint did not intend to expose. `status`,
`file_key`, `file_etag` and the computed dimensions are all derived server-side
and are absent here by design.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework import serializers

from apps.catalog.models import Category, Feature, MediaItem, Subcategory

from .models import TicketKind


class PresignFileSerializer(serializers.Serializer):
    """One file in a presign batch."""

    filename = serializers.CharField(
        max_length=255, allow_blank=True, required=False, default=""
    )
    content_type = serializers.CharField(max_length=100)
    size = serializers.IntegerField(min_value=1)
    kind = serializers.ChoiceField(
        choices=TicketKind.choices, required=False, default=TicketKind.ASSET
    )


class PresignRequestSerializer(serializers.Serializer):
    """
    `POST /admin/uploads/presign`

    One array-shaped request covers a single file and a batch of fifty
    identically, which is what "bulk or single, in one feature and selected
    category" needs — no second code path.
    """

    type = serializers.SlugField()
    category_id = serializers.UUIDField()
    subcategory_id = serializers.UUIDField(required=False, allow_null=True)
    files = serializers.ListField(child=PresignFileSerializer(), min_length=1)

    def validate_files(self, value: list) -> list:
        limit = settings.UPLOAD_BULK_MAX_FILES
        if len(value) > limit:
            raise serializers.ValidationError(
                f"At most {limit} files per request; received {len(value)}."
            )
        return value

    def validate(self, attrs: dict) -> dict:
        feature = Feature.objects.filter(slug=attrs["type"], is_active=True).first()
        if feature is None:
            raise serializers.ValidationError(
                {"type": f"Unknown or inactive content type '{attrs['type']}'."}
            )

        category = (
            Category.objects.filter(pk=attrs["category_id"], is_active=True)
            .select_related("feature")
            .first()
        )
        if category is None:
            raise serializers.ValidationError({"category_id": "Unknown or inactive category."})
        if category.feature_id != feature.id:
            raise serializers.ValidationError(
                {
                    "category_id": (
                        f"Category belongs to '{category.feature.slug}', not '{feature.slug}'."
                    )
                }
            )

        subcategory = None
        if attrs.get("subcategory_id"):
            subcategory = Subcategory.objects.filter(
                pk=attrs["subcategory_id"], is_active=True
            ).first()
            if subcategory is None:
                raise serializers.ValidationError(
                    {"subcategory_id": "Unknown or inactive subcategory."}
                )
            # The check that makes an empty screen impossible: a subcategory from
            # a different category would produce items nothing can ever list.
            if subcategory.category_id != category.id:
                raise serializers.ValidationError(
                    {
                        "subcategory_id": (
                            "Subcategory belongs to a different category than the one supplied."
                        )
                    }
                )

        attrs["feature_obj"] = feature
        attrs["category_obj"] = category
        attrs["subcategory_obj"] = subcategory
        return attrs


class CommitItemSerializer(serializers.Serializer):
    """One item in a commit batch."""

    asset_ticket_id = serializers.UUIDField()
    preview_ticket_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    premium = serializers.BooleanField(required=False, default=False)
    priority = serializers.IntegerField(required=False, default=0)
    # None means "derive from the media type" — see services._resolve_is_live.
    is_live = serializers.BooleanField(required=False, allow_null=True, default=None)
    duration_ms = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    tags = serializers.ListField(
        child=serializers.SlugField(max_length=64), required=False, default=list
    )

    def validate_tags(self, value: list) -> list:
        if len(value) > 20:
            raise serializers.ValidationError("At most 20 tags per item.")
        return value


class CommitRequestSerializer(serializers.Serializer):
    """`POST /admin/uploads/commit`"""

    items = serializers.ListField(child=CommitItemSerializer(), min_length=1)

    def validate_items(self, value: list) -> list:
        limit = settings.UPLOAD_BULK_MAX_FILES
        if len(value) > limit:
            raise serializers.ValidationError(
                f"At most {limit} items per request; received {len(value)}."
            )
        seen: set[str] = set()
        for entry in value:
            ticket = str(entry["asset_ticket_id"])
            if ticket in seen:
                raise serializers.ValidationError(
                    f"Ticket {ticket} appears more than once in this batch."
                )
            seen.add(ticket)
        return value


class AbortRequestSerializer(serializers.Serializer):
    """`POST /admin/uploads/abort`"""

    ticket_ids = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, max_length=200
    )


class MediaItemUpdateSerializer(serializers.ModelSerializer):
    """
    `PATCH /admin/items/{id}` — editorial fields only.

    Storage keys, sizes, checksums, dimensions and status are all derived at
    commit time. Allowing them to be patched would let the database disagree with
    the bytes in the bucket, so they are simply not in `fields`.
    """

    tags = serializers.ListField(child=serializers.SlugField(max_length=64), required=False)
    subcategory_id = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = MediaItem
        fields = (
            "name",
            "premium",
            "priority",
            "is_live",
            "is_active",
            "tags",
            "subcategory_id",
        )

    def validate_subcategory_id(self, value):
        if value is None:
            return None
        subcategory = Subcategory.objects.filter(pk=value, is_active=True).first()
        if subcategory is None:
            raise serializers.ValidationError("Unknown or inactive subcategory.")
        if subcategory.category_id != self.instance.category_id:
            raise serializers.ValidationError(
                "Subcategory belongs to a different category than this item."
            )
        return value

    def update(self, instance: MediaItem, validated_data: dict) -> MediaItem:
        from .services import _resolve_tags

        tag_slugs = validated_data.pop("tags", None)
        subcategory_id = validated_data.pop("subcategory_id", ...)

        for field_name, value in validated_data.items():
            setattr(instance, field_name, value)
        if subcategory_id is not ...:
            instance.subcategory_id = subcategory_id

        instance.full_clean(
            exclude=["file_key", "file_etag", "file_mime", "preview_mime", "media_type"]
        )
        instance.save()

        if tag_slugs is not None:
            instance.tags.set(_resolve_tags(tag_slugs))
        return instance
