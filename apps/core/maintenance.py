"""
Housekeeping jobs.

Written as plain functions so the HTTP cron endpoints and the management commands
run identical code. That matters because the target hosts differ: Render's free
tier has no cron and no background worker (so an external scheduler calls the
HTTP endpoint), while Railway and a local shell run the command directly.

Both jobs are idempotent. A scheduler that fires twice, or retries after a
timeout, must not do damage — so nothing here depends on running exactly once.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from apps.catalog.models import Category, ItemStatus, MediaItem, Subcategory
from apps.ingest.models import (
    MultipartSessionStatus,
    MultipartUploadSession,
    TicketStatus,
    UploadBatch,
    UploadTicket,
)

from . import audit

logger = logging.getLogger("core.maintenance")

# Bounded per run so one invocation cannot exceed a request timeout or hold a
# long transaction. Whatever is left over is picked up on the next tick.
DEFAULT_BATCH_LIMIT = 200


def reap_orphans(
    *, batch_limit: int = DEFAULT_BATCH_LIMIT, request: Any = None, dry_run: bool = False
) -> dict[str, Any]:
    """
    Delete abandoned uploads and purge archived content.

    Three kinds of debris accumulate:

      1. Tickets that were issued and never committed — the browser tab closed
         mid-upload. Their B2 objects are orphaned bytes nobody can reach.
      2. Tickets already marked expired or aborted whose objects may still exist.
      3. Items archived longer ago than ARCHIVE_RETENTION_DAYS, whose bytes are
         now safe to delete.

    Storage deletions happen before the database rows are updated. If a delete
    fails, the row stays and the next run retries — the opposite order would
    forget the key and leak the bytes permanently.
    """
    from apps.ingest import storage

    now = timezone.now()
    stats = {
        "expired_tickets": 0,
        "objects_deleted": 0,
        "objects_failed": 0,
        "items_purged": 0,
        "multipart_sessions_aborted": 0,
        "dry_run": dry_run,
    }

    # --- 1 & 2: abandoned upload tickets ---------------------------------- #
    stale_tickets = list(
        UploadTicket.objects.filter(
            Q(status=TicketStatus.ISSUED, expires_at__lt=now)
            | Q(status__in=[TicketStatus.EXPIRED, TicketStatus.ABORTED])
        ).order_by("expires_at")[:batch_limit]
    )

    reaped_ids: list[Any] = []
    for ticket in stale_tickets:
        if dry_run:
            stats["expired_tickets"] += 1
            continue
        if storage.delete_object(ticket.object_key):
            stats["objects_deleted"] += 1
        else:
            stats["objects_failed"] += 1
        reaped_ids.append(ticket.pk)
        stats["expired_tickets"] += 1

    if reaped_ids:
        # Deleting the rows is safe now: the keys they held have been dealt with.
        UploadTicket.objects.filter(pk__in=reaped_ids).delete()

    # --- 2b: expired multipart uploads ------------------------------------ #
    stale_sessions = list(
        MultipartUploadSession.objects.select_related("batch")
        .filter(status=MultipartSessionStatus.UPLOADING, batch__expires_at__lt=now)
        .order_by("batch__expires_at")[:batch_limit]
    )
    for session in stale_sessions:
        if dry_run:
            stats["multipart_sessions_aborted"] += 1
            continue
        if storage.abort_multipart_upload(
            key=session.object_key, upload_id=session.storage_upload_id
        ):
            session.status = MultipartSessionStatus.EXPIRED
            session.save(update_fields=["status", "updated_at"])
            stats["multipart_sessions_aborted"] += 1
    UploadBatch.objects.filter(status="OPEN", expires_at__lt=now).update(status="EXPIRED")

    # --- 3: archived items past their retention window --------------------- #
    cutoff = now - timezone.timedelta(days=settings.ARCHIVE_RETENTION_DAYS)
    purgeable = list(
        MediaItem.objects.filter(
            status=ItemStatus.ARCHIVED, archived_at__isnull=False, archived_at__lt=cutoff
        ).order_by("archived_at")[:batch_limit]
    )

    for item in purgeable:
        if dry_run:
            stats["items_purged"] += 1
            continue

        ok = True
        for key in filter(None, (item.file_key, item.preview_key)):
            if storage.delete_object(key):
                stats["objects_deleted"] += 1
            else:
                stats["objects_failed"] += 1
                ok = False

        if not ok:
            # Leave the row alone and retry next run rather than lose the key.
            logger.warning(
                "Deferred purge: storage delete failed",
                extra={"event": "purge_deferred", "item_id": str(item.id)},
            )
            continue

        audit.record(
            "ITEM_PURGE",
            request=request,
            object_type="MediaItem",
            object_id=str(item.id),
            payload={"file_key": item.file_key, "preview_key": item.preview_key},
        )
        item.delete()
        stats["items_purged"] += 1

    # Coverage note: a run that hits its batch limit has more work pending, and
    # saying so beats a caller assuming the queue is empty.
    stats["more_pending"] = (
        len(stale_tickets) >= batch_limit
        or len(stale_sessions) >= batch_limit
        or len(purgeable) >= batch_limit
    )

    if not dry_run and (stats["expired_tickets"] or stats["items_purged"]):
        audit.record(
            "ORPHAN_REAP",
            request=request,
            object_type="maintenance",
            object_id="reap_orphans",
            payload=stats,
        )

    logger.info("Orphan reap complete", extra={"event": "reap_orphans", **stats})
    return stats


def reconcile_counts(*, request: Any = None, dry_run: bool = False) -> dict[str, Any]:
    """
    Rebuild the denormalized counters from the real data.

    `Category.item_count`, `Subcategory.item_count` and
    `Category.has_subcategories` are maintained incrementally on the write paths
    because the alternative is a COUNT per row on every list request. Incremental
    counters drift — a crash between the item insert and the counter update, a
    bulk operation that bypasses the ORM — so this recomputes them from source.

    Cheap enough to run hourly: three aggregate queries and a bulk update.
    """
    stats = {
        "categories_fixed": 0,
        "subcategories_fixed": 0,
        "has_subcategories_fixed": 0,
        "dry_run": dry_run,
    }

    published = Q(items__status=ItemStatus.READY, items__is_active=True)

    # --- Category.item_count ---------------------------------------------- #
    category_rows = Category.objects.annotate(
        actual=Count("items", filter=published, distinct=True)
    ).only("id", "item_count")

    drifted_categories = [c for c in category_rows if c.item_count != c.actual]
    for category in drifted_categories:
        category.item_count = category.actual
    if drifted_categories and not dry_run:
        Category.objects.bulk_update(drifted_categories, ["item_count"], batch_size=500)
    stats["categories_fixed"] = len(drifted_categories)

    # --- Subcategory.item_count ------------------------------------------- #
    subcategory_rows = Subcategory.objects.annotate(
        actual=Count("items", filter=published, distinct=True)
    ).only("id", "item_count")

    drifted_subs = [s for s in subcategory_rows if s.item_count != s.actual]
    for subcategory in drifted_subs:
        subcategory.item_count = subcategory.actual
    if drifted_subs and not dry_run:
        Subcategory.objects.bulk_update(drifted_subs, ["item_count"], batch_size=500)
    stats["subcategories_fixed"] = len(drifted_subs)

    # --- Category.has_subcategories --------------------------------------- #
    has_subs = Subcategory.objects.filter(category=OuterRef("pk"))
    flag_rows = Category.objects.annotate(actual=Exists(has_subs)).only(
        "id", "has_subcategories"
    )
    drifted_flags = [c for c in flag_rows if c.has_subcategories != c.actual]
    for category in drifted_flags:
        category.has_subcategories = category.actual
    if drifted_flags and not dry_run:
        Category.objects.bulk_update(drifted_flags, ["has_subcategories"], batch_size=500)
    stats["has_subcategories_fixed"] = len(drifted_flags)

    if not dry_run and any(
        stats[k] for k in ("categories_fixed", "subcategories_fixed", "has_subcategories_fixed")
    ):
        audit.record(
            "COUNTS_RECONCILE",
            request=request,
            object_type="maintenance",
            object_id="reconcile_counts",
            payload=stats,
        )

    logger.info("Count reconcile complete", extra={"event": "reconcile_counts", **stats})
    return stats
