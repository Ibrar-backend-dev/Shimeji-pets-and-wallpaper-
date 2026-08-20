"""Admin for the audit trail. Append-only, so entirely read-only here."""

from __future__ import annotations

from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor_label", "object_type", "object_id",
                    "ip", "request_id")
    list_filter = ("action", "created_at")
    search_fields = ("object_id", "request_id", "actor_label")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("actor", "actor_label", "action", "object_type", "object_id",
                       "payload", "ip", "request_id", "created_at")

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        # An audit trail that can be edited or trimmed from the same UI it
        # audits is not an audit trail.
        return False
