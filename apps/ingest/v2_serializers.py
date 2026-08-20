"""Strict request serializers for the resumable v2 upload API."""

from django.conf import settings
from rest_framework import serializers

from .models import BatchPublishMode, TicketKind


class BatchCreateSerializer(serializers.Serializer):
    type = serializers.SlugField()
    category_id = serializers.UUIDField()
    subcategory_id = serializers.UUIDField(required=False, allow_null=True)
    publish_mode = serializers.ChoiceField(
        choices=BatchPublishMode.choices, default=BatchPublishMode.DRAFT
    )


class SessionCreateSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=TicketKind.choices, default=TicketKind.ASSET)
    filename = serializers.CharField(
        max_length=255, allow_blank=True, required=False, default=""
    )
    content_type = serializers.CharField(max_length=100)
    size = serializers.IntegerField(min_value=1)
    name = serializers.CharField(max_length=200, allow_blank=True, required=False, default="")
    premium = serializers.BooleanField(required=False, default=False)
    priority = serializers.IntegerField(required=False, default=0)
    is_live = serializers.BooleanField(required=False, allow_null=True, default=None)
    duration_ms = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    tags = serializers.ListField(
        child=serializers.SlugField(max_length=64), required=False, default=list
    )

    def validate_tags(self, value):
        if len(value) > 20:
            raise serializers.ValidationError("At most 20 tags per item.")
        return value


class PartURLsSerializer(serializers.Serializer):
    part_numbers = serializers.ListField(
        child=serializers.IntegerField(min_value=1), min_length=1
    )

    def validate_part_numbers(self, value):
        if len(value) > settings.MULTIPART_MAX_PART_URLS:
            raise serializers.ValidationError(
                f"At most {settings.MULTIPART_MAX_PART_URLS} part URLs per request."
            )
        if len(set(value)) != len(value):
            raise serializers.ValidationError("Part numbers must not repeat.")
        return value


class FinalizeSerializer(serializers.Serializer):
    preview_session_id = serializers.UUIDField(required=False, allow_null=True)


class PublishSerializer(serializers.Serializer):
    item_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list
    )
