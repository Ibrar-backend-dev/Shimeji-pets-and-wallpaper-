"""
The presign-to-commit handshake.

A ticket is minted when the server presigns an upload and consumed when the
upload is committed. Keeping it separate from MediaItem means an abandoned
browser upload leaves a *ticket*, not a half-built catalog row that the API
might serve. The reaper deletes expired tickets together with the orphaned B2
objects they point at.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.catalog.models import Category, Feature, Subcategory
from apps.core.models import TimeStampedModel


class TicketKind(models.TextChoices):
    ASSET = "ASSET", "Main asset"
    PREVIEW = "PREVIEW", "Preview image"


class TicketStatus(models.TextChoices):
    ISSUED = "ISSUED", "Issued"
    COMMITTED = "COMMITTED", "Committed"
    ABORTED = "ABORTED", "Aborted"
    EXPIRED = "EXPIRED", "Expired"


class BatchPublishMode(models.TextChoices):
    DRAFT = "DRAFT", "Draft: publish explicitly"
    IMMEDIATE = "IMMEDIATE", "Publish when finalized"


class BatchStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    COMPLETED = "COMPLETED", "Completed"
    ABORTED = "ABORTED", "Aborted"
    EXPIRED = "EXPIRED", "Expired"


class MultipartSessionStatus(models.TextChoices):
    UPLOADING = "UPLOADING", "Uploading"
    UPLOADED = "UPLOADED", "Uploaded to storage"
    FINALIZED = "FINALIZED", "Media item created"
    ABORTED = "ABORTED", "Aborted"
    EXPIRED = "EXPIRED", "Expired"
    FAILED = "FAILED", "Validation failed"


class UploadBatch(TimeStampedModel):
    """Durable, staff-owned grouping for resumable v2 uploads."""

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="upload_batches"
    )
    feature = models.ForeignKey(
        Feature, on_delete=models.PROTECT, related_name="upload_batches"
    )
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="upload_batches"
    )
    subcategory = models.ForeignKey(
        Subcategory,
        on_delete=models.PROTECT,
        related_name="upload_batches",
        null=True,
        blank=True,
    )
    publish_mode = models.CharField(
        max_length=10, choices=BatchPublishMode.choices, default=BatchPublishMode.DRAFT
    )
    status = models.CharField(
        max_length=10, choices=BatchStatus.choices, default=BatchStatus.OPEN
    )
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=["created_by", "status", "-created_at"], name="batch_owner_status_idx"
            )
        ]

    def is_redeemable_by(self, user) -> bool:
        return (
            self.status == BatchStatus.OPEN
            and timezone.now() <= self.expires_at
            and self.created_by_id == getattr(user, "pk", None)
        )


class MultipartUploadSession(TimeStampedModel):
    """One B2 multipart object, resumable while its batch remains open."""

    batch = models.ForeignKey(UploadBatch, on_delete=models.CASCADE, related_name="sessions")
    kind = models.CharField(max_length=8, choices=TicketKind.choices, default=TicketKind.ASSET)
    object_key = models.CharField(max_length=512, unique=True)
    storage_upload_id = models.CharField(max_length=512, unique=True)
    declared_name = models.CharField(max_length=255, blank=True)
    declared_mime = models.CharField(max_length=100)
    declared_bytes = models.BigIntegerField()
    part_size = models.PositiveIntegerField()
    metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=12,
        choices=MultipartSessionStatus.choices,
        default=MultipartSessionStatus.UPLOADING,
    )
    failure_reason = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [models.Index(fields=["status", "updated_at"], name="multipart_status_idx")]

    @property
    def part_count(self) -> int:
        return max(1, (self.declared_bytes + self.part_size - 1) // self.part_size)

    def is_redeemable_by(self, user) -> bool:
        return self.status == MultipartSessionStatus.UPLOADING and self.batch.is_redeemable_by(
            user
        )


class UploadTicket(TimeStampedModel):
    """One presigned upload slot for exactly one object."""

    object_key = models.CharField(max_length=512, unique=True)
    kind = models.CharField(max_length=8, choices=TicketKind.choices, default=TicketKind.ASSET)

    feature = models.ForeignKey(Feature, on_delete=models.PROTECT, related_name="tickets")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="tickets")
    subcategory = models.ForeignKey(
        Subcategory, on_delete=models.PROTECT, related_name="tickets", null=True, blank=True
    )

    # What the client claimed at presign time. Verified against B2 on commit;
    # never trusted on its own.
    declared_name = models.CharField(max_length=255, blank=True)
    declared_mime = models.CharField(max_length=100)
    declared_bytes = models.BigIntegerField()

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="upload_tickets"
    )
    status = models.CharField(
        max_length=10, choices=TicketStatus.choices, default=TicketStatus.ISSUED
    )
    expires_at = models.DateTimeField(db_index=True)
    failure_reason = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["status", "expires_at"], name="ticket_status_exp_idx"),
            models.Index(fields=["created_by", "-created_at"], name="ticket_creator_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.object_key} ({self.status})"

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    def is_redeemable_by(self, user) -> bool:
        """
        A ticket may be committed once, before it expires, by whoever created it.

        The creator check stops one staff account from committing another's
        in-flight upload, which matters because the committer chooses the title,
        category and premium flag.
        """
        return (
            self.status == TicketStatus.ISSUED
            and not self.is_expired
            and self.created_by_id == getattr(user, "pk", None)
        )
