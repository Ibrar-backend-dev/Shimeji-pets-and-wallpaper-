"""
Admin for API clients.

The raw key is shown exactly once, when the client is created, via a message
banner. It is never stored, so it cannot be shown again — rotate to issue a new
one. Saving here also drops the cached snapshot, so a deactivated key stops
working on the next request rather than up to five minutes later.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.utils.html import format_html

from .authentication import invalidate_client_cache
from .models import AppClient, generate_key


@admin.register(AppClient)
class AppClientAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "key_prefix", "is_active", "scope", "rate_limit_per_min",
                    "last_used_at")
    list_filter = ("is_active",)
    list_editable = ("is_active", "rate_limit_per_min")
    search_fields = ("name", "slug", "key_prefix")
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ("allowed_features",)
    readonly_fields = ("id", "key_prefix", "key_hash", "last_used_at", "created_at",
                       "updated_at", "usage_hint")
    fieldsets = (
        (None, {"fields": ("name", "slug", "is_active")}),
        (
            "Scope and limits",
            {
                "fields": ("allowed_features", "rate_limit_per_min"),
                "description": (
                    "Leave allowed_features empty to permit every type. A rate limit "
                    "of 0 means unlimited."
                ),
            },
        ),
        (
            "Credential",
            {
                "fields": ("id", "key_prefix", "key_hash", "usage_hint", "last_used_at"),
                "description": (
                    "Keys are stored hashed and cannot be recovered. To replace one, "
                    "use: manage.py create_app_client \"<name>\" --rotate"
                ),
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Scope")
    def scope(self, obj: AppClient) -> str:
        slugs = [f.slug for f in obj.allowed_features.all()]
        return ", ".join(slugs) if slugs else "all types"

    @admin.display(description="Usage")
    def usage_hint(self, obj: AppClient) -> str:
        if not obj.key_prefix:
            return "—"
        return format_html(
            '<code>curl -H "X-API-Key: {}…" .../api/v1/wallpapers</code>', obj.key_prefix
        )

    def save_model(self, request, obj, form, change) -> None:  # noqa: ANN001
        old_prefix = ""
        if change:
            old_prefix = AppClient.objects.filter(pk=obj.pk).values_list(
                "key_prefix", flat=True
            ).first() or ""

        raw_key = None
        if not obj.key_hash:
            # New client: mint a credential and surface it once.
            raw_key, _, _ = generate_key(obj.slug or obj.name)
            obj.set_key(raw_key)

        super().save_model(request, obj, form, change)

        if old_prefix:
            invalidate_client_cache(old_prefix)
        if obj.key_prefix:
            invalidate_client_cache(obj.key_prefix)

        if raw_key:
            self.message_user(
                request,
                format_html(
                    "API key for <b>{}</b> — copy it now, it will not be shown "
                    "again:<br><code style='font-size:1.1em'>{}</code>",
                    obj.name,
                    raw_key,
                ),
                messages.WARNING,
            )
