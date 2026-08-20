"""
Django admin for the catalog.

The admin is the taxonomy editor: types, categories, subcategories and tags are
managed here, and items are reviewed and curated here. Item *creation* is not
offered — an item without a validated B2 object behind it would be a broken row,
so items only ever come from the ingest pipeline.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.utils.html import format_html

from apps.ingest.storage import public_url

from .models import Category, Feature, MediaItem, Subcategory, Tag


def _thumb(url: str, size: int = 60) -> str:
    if not url:
        return "—"
    return format_html(
        '<img src="{}" style="height:{}px;width:auto;border-radius:4px;'
        'object-fit:cover;background:#eee" loading="lazy" />',
        url,
        size,
    )


@admin.register(Feature)
class FeatureAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "priority", "category_count", "max_file_mb")
    list_editable = ("is_active", "priority")
    search_fields = ("name", "slug")
    readonly_fields = ("id", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("id", "slug", "name", "description", "priority", "is_active")}),
        (
            "Upload policy",
            {
                "fields": (
                    "allowed_mimes",
                    "max_file_bytes",
                    "max_pixels",
                    "strict_zip_structure",
                ),
                "description": (
                    "Enforced at presign and again at commit. allowed_mimes is a JSON "
                    "list, e.g. [\"image/jpeg\", \"image/png\"]."
                ),
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Categories")
    def category_count(self, obj: Feature) -> int:
        return obj.categories.count()

    @admin.display(description="Max file")
    def max_file_mb(self, obj: Feature) -> str:
        return f"{obj.max_file_bytes / (1024 * 1024):.0f} MB"


class SubcategoryInline(admin.TabularInline):
    model = Subcategory
    extra = 0
    fields = ("name", "slug", "priority", "is_active", "item_count")
    readonly_fields = ("item_count",)
    prepopulated_fields = {"slug": ("name",)}
    show_change_link = True


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "feature",
        "priority",
        "is_active",
        "has_subcategories",
        "item_count",
        "thumbnail_preview",
    )
    list_filter = ("feature", "is_active", "has_subcategories")
    list_editable = ("priority", "is_active")
    list_select_related = ("feature",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = (
        "id",
        "has_subcategories",
        "item_count",
        "created_at",
        "updated_at",
        "thumbnail_preview",
    )
    inlines = [SubcategoryInline]
    ordering = ("feature__priority", "priority", "name")

    @admin.display(description="Thumb")
    def thumbnail_preview(self, obj: Category) -> str:
        return _thumb(public_url(obj.thumbnail_key))


@admin.register(Subcategory)
class SubcategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "feature_name", "priority", "is_active", "item_count")
    list_filter = ("category__feature", "is_active")
    list_editable = ("priority", "is_active")
    list_select_related = ("category__feature",)
    search_fields = ("name", "slug", "category__name")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("id", "item_count", "created_at", "updated_at")
    autocomplete_fields = ("category",)

    @admin.display(description="Type", ordering="category__feature__name")
    def feature_name(self, obj: Subcategory) -> str:
        return obj.category.feature.name


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "usage")
    list_editable = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("id", "created_at", "updated_at")

    @admin.display(description="Items")
    def usage(self, obj: Tag) -> int:
        return obj.items.count()


@admin.register(MediaItem)
class MediaItemAdmin(admin.ModelAdmin):
    list_display = (
        "preview_thumb",
        "name",
        "feature",
        "category",
        "subcategory",
        "media_type",
        "is_live",
        "premium",
        "dimensions",
        "size_mb",
        "priority",
        "status",
        "is_active",
    )
    list_display_links = ("preview_thumb", "name")
    list_filter = (
        "feature",
        "status",
        "media_type",
        "is_live",
        "premium",
        "is_active",
        "orientation",
        "resolution",
    )
    list_editable = ("priority", "premium", "is_active")
    # Without this, the changelist runs three extra queries per row.
    list_select_related = ("feature", "category", "subcategory")
    search_fields = ("name", "file_key", "id")
    filter_horizontal = ("tags",)
    date_hierarchy = "created_at"
    ordering = ("priority", "-created_at")
    list_per_page = 50

    readonly_fields = (
        "id",
        "feature",
        "media_type",
        "file_key",
        "file_bytes",
        "file_mime",
        "file_etag",
        "preview_key",
        "preview_bytes",
        "preview_mime",
        "width",
        "height",
        "orientation",
        "resolution",
        "dominant_color",
        "zip_entries",
        "zip_has_conf",
        "archived_at",
        "created_at",
        "updated_at",
        "asset_links",
        "large_preview",
    )
    fieldsets = (
        (
            "Editorial",
            {
                "fields": (
                    "name",
                    "category",
                    "subcategory",
                    "tags",
                    "premium",
                    "priority",
                    "is_live",
                    "is_active",
                    "status",
                )
            },
        ),
        ("Preview", {"fields": ("large_preview", "asset_links")}),
        (
            "Stored asset (derived at commit — read only)",
            {
                "fields": (
                    "id",
                    "feature",
                    "media_type",
                    "file_key",
                    "file_bytes",
                    "file_mime",
                    "file_etag",
                    "preview_key",
                    "preview_bytes",
                    "preview_mime",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Derived metadata",
            {
                "fields": (
                    "width",
                    "height",
                    "duration_ms",
                    "orientation",
                    "resolution",
                    "dominant_color",
                    "zip_entries",
                    "zip_has_conf",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Timestamps",
            {"fields": ("archived_at", "created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )
    actions = ("action_archive", "action_mark_premium", "action_clear_premium")

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        # Items must come from the ingest pipeline: a row without a validated B2
        # object behind it would serve a broken URL.
        return False

    @admin.display(description="")
    def preview_thumb(self, obj: MediaItem) -> str:
        return _thumb(public_url(obj.preview_key or obj.file_key), 48)

    @admin.display(description="Preview")
    def large_preview(self, obj: MediaItem) -> str:
        return _thumb(public_url(obj.preview_key or obj.file_key), 320)

    @admin.display(description="Asset")
    def asset_links(self, obj: MediaItem) -> str:
        asset = public_url(obj.file_key)
        preview = public_url(obj.preview_key)
        if not asset:
            return "MEDIA_CDN_BASE_URL is not configured."
        if preview:
            return format_html(
                '<a href="{}" target="_blank" rel="noopener">asset</a> · '
                '<a href="{}" target="_blank" rel="noopener">preview</a>',
                asset,
                preview,
            )
        return format_html('<a href="{}" target="_blank" rel="noopener">asset</a>', asset)

    @admin.display(description="Size", ordering="file_bytes")
    def size_mb(self, obj: MediaItem) -> str:
        return f"{obj.file_bytes / (1024 * 1024):.2f} MB"

    @admin.display(description="Dimensions")
    def dimensions(self, obj: MediaItem) -> str:
        if obj.width and obj.height:
            return f"{obj.width}×{obj.height}"
        return "—"

    @admin.action(description="Archive selected items (bytes purged after retention)")
    def action_archive(self, request, queryset) -> None:  # noqa: ANN001
        from apps.ingest.services import archive_item

        archived = 0
        for item in queryset.exclude(status="ARCHIVED"):
            archive_item(item=item, request=request)
            archived += 1
        self.message_user(
            request,
            f"Archived {archived} item(s). Bytes are purged by the reaper after "
            f"the retention window.",
            messages.SUCCESS,
        )

    @admin.action(description="Mark selected as premium")
    def action_mark_premium(self, request, queryset) -> None:  # noqa: ANN001
        updated = queryset.update(premium=True)
        self.message_user(request, f"Marked {updated} item(s) premium.", messages.SUCCESS)

    @admin.action(description="Clear premium on selected")
    def action_clear_premium(self, request, queryset) -> None:  # noqa: ANN001
        updated = queryset.update(premium=False)
        self.message_user(request, f"Cleared premium on {updated} item(s).", messages.SUCCESS)
