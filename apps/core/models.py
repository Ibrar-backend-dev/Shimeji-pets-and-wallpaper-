"""Abstract bases and the audit trail."""

from __future__ import annotations

from django.conf import settings
from django.db import models

from .ids import uuid7


class UUIDModel(models.Model):
    """
    Primary key base: a time-ordered UUID.

    See apps/core/ids.py for why this is UUIDv7 and not uuid4.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(UUIDModel):
    """UUID pk plus creation/update stamps."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditLog(models.Model):
    """
    Append-only record of privileged actions.

    Django's admin LogEntry covers changes made through the admin UI. This covers
    everything driven through the API — bulk ingest commits, archives, and cron
    purges — so "who removed four hundred wallpapers, and when" has an answer.

    Deliberately not a TimeStampedModel: rows are never updated.
    """

    class Action(models.TextChoices):
        UPLOAD_PRESIGN = "UPLOAD_PRESIGN", "Upload presigned"
        UPLOAD_COMMIT = "UPLOAD_COMMIT", "Upload committed"
        UPLOAD_ABORT = "UPLOAD_ABORT", "Upload aborted"
        ITEM_UPDATE = "ITEM_UPDATE", "Item updated"
        ITEM_ARCHIVE = "ITEM_ARCHIVE", "Item archived"
        ITEM_PURGE = "ITEM_PURGE", "Item purged"
        ORPHAN_REAP = "ORPHAN_REAP", "Orphan reaped"
        COUNTS_RECONCILE = "COUNTS_RECONCILE", "Counts reconciled"

    id = models.BigAutoField(primary_key=True)
    # Cron and management commands have no actor, hence nullable.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    actor_label = models.CharField(
        max_length=150,
        blank=True,
        help_text="Username captured at write time, so the trail survives user deletion.",
    )
    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "audit log entry"
        verbose_name_plural = "audit log"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["action", "-created_at"], name="audit_action_created_idx"),
            models.Index(
                fields=["object_type", "object_id"], name="audit_object_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.object_type}:{self.object_id} by {self.actor_label or 'system'}"
