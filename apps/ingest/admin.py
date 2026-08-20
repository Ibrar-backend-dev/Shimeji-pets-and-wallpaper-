"""
Admin for upload tickets.

Read-only: a ticket is a record of a presign, and editing one by hand would let
a stale or foreign ticket be redeemed. Useful for answering "did that bulk upload
actually finish?" — a pile of ISSUED tickets past their expiry is the signature
of a browser tab closed mid-upload, and the reaper cleans them up.
"""

from __future__ import annotations

from django.contrib import admin

from .models import UploadTicket


@admin.register(UploadTicket)
class UploadTicketAdmin(admin.ModelAdmin):
    list_display = ("object_key", "kind", "status", "feature", "category", "declared_mime",
                    "declared_bytes", "created_by", "expires_at", "created_at")
    list_filter = ("status", "kind", "feature")
    list_select_related = ("feature", "category", "created_by")
    search_fields = ("object_key", "declared_name", "id")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request) -> bool:  # noqa: ANN001
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ANN001
        return False
