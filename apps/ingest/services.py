"""
Ingest orchestration: presign, commit, abort.

The flow, and why it is shaped this way:

    1. presign  server validates the *declared* upload, mints an object key and
                an UploadTicket, returns a presigned PUT
    2. PUT      the browser uploads straight to B2 — the app server sees none of
                the bytes, which is what makes a 50-file bulk upload work inside
                a free tier's 512 MB and 30-60 s ceiling
    3. commit   server re-derives everything from the stored object, validates
                it, generates a preview, and creates the MediaItem row

Commit is per-item rather than all-or-nothing: one corrupt file in a batch of
fifty must not discard the other forty-nine successful uploads. Each item gets
its own transaction and its own result entry.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.catalog.models import Category, Feature, ItemStatus, MediaItem, Subcategory, Tag
from apps.core import audit
from apps.core.exceptions import ApiError

from . import storage, validators
from .models import TicketKind, TicketStatus, UploadTicket

logger = logging.getLogger("ingest.upload")


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass
class PresignedSlot:
    ticket_id: str
    object_key: str
    upload_url: str
    required_headers: dict[str, str]
    expires_at: str
    kind: str
    filename: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "object_key": self.object_key,
            "upload_url": self.upload_url,
            "required_headers": self.required_headers,
            "expires_at": self.expires_at,
            "kind": self.kind,
            "filename": self.filename,
        }


@dataclass
class ItemResult:
    """Per-item outcome, so a partial batch failure is legible to the uploader."""

    index: int
    ok: bool
    item_id: str | None = None
    error: str | None = None
    code: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"index": self.index, "ok": self.ok}
        if self.ok:
            payload["id"] = self.item_id
            payload.update(self.detail)
        else:
            payload["error"] = self.error
            payload["code"] = self.code
        return payload


# --------------------------------------------------------------------------- #
# Presign
# --------------------------------------------------------------------------- #


def presign_uploads(
    *,
    user,  # noqa: ANN001
    feature: Feature,
    category: Category,
    subcategory: Subcategory | None,
    files: list[dict[str, Any]],
    request=None,  # noqa: ANN001
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Validate and presign a batch. Returns (slots, rejections).

    Rejections are reported rather than raised so an uploader learns which of
    fifty files are unacceptable in one round trip instead of fifty.
    """
    slots: list[PresignedSlot] = []
    rejections: list[dict[str, Any]] = []
    tickets: list[UploadTicket] = []
    expires_at = timezone.now() + timezone.timedelta(
        seconds=settings.PRESIGN_EXPIRY_SECONDS
    )

    for index, spec in enumerate(files):
        filename = str(spec.get("filename") or "")
        content_type = str(spec.get("content_type") or "").lower()
        kind = str(spec.get("kind") or TicketKind.ASSET).upper()
        try:
            size = int(spec.get("size") or 0)
        except (TypeError, ValueError):
            rejections.append(
                {
                    "index": index,
                    "filename": filename,
                    "error": "'size' must be an integer.",
                    "code": "bad_size",
                }
            )
            continue

        if kind not in TicketKind.values:
            rejections.append(
                {
                    "index": index,
                    "filename": filename,
                    "error": f"'kind' must be one of {', '.join(TicketKind.values)}.",
                    "code": "bad_kind",
                }
            )
            continue

        # A preview is always an image, whatever the asset it accompanies, so it
        # is validated against image rules rather than the feature's whitelist.
        try:
            if kind == TicketKind.PREVIEW:
                _validate_preview_spec(filename, content_type, size)
            else:
                validators.validate_declared_upload(
                    feature=feature,
                    filename=filename,
                    content_type=content_type,
                    size=size,
                )
        except validators.ValidationFailure as exc:
            rejections.append(
                {
                    "index": index,
                    "filename": filename,
                    "error": exc.message,
                    "code": exc.code,
                }
            )
            continue

        object_key = storage.build_object_key(
            feature_slug=feature.slug,
            category_slug=category.slug,
            kind="previews" if kind == TicketKind.PREVIEW else "assets",
            unique_id=uuid.uuid4().hex,
            mime=content_type,
        )

        try:
            upload_url, headers = storage.presign_put(
                key=object_key, content_type=content_type, content_length=size
            )
        except storage.StorageError as exc:
            logger.error(
                "Presign failed",
                extra={"event": "presign_error", "key": object_key, "error": str(exc)},
            )
            rejections.append(
                {
                    "index": index,
                    "filename": filename,
                    "error": "Storage is unavailable. Try again shortly.",
                    "code": "storage_unavailable",
                }
            )
            continue

        ticket = UploadTicket(
            object_key=object_key,
            kind=kind,
            feature=feature,
            category=category,
            subcategory=subcategory,
            declared_name=filename[:255],
            declared_mime=content_type,
            declared_bytes=size,
            created_by=user,
            status=TicketStatus.ISSUED,
            expires_at=expires_at,
        )
        tickets.append(ticket)
        slots.append(
            PresignedSlot(
                ticket_id=str(ticket.id),
                object_key=object_key,
                upload_url=upload_url,
                required_headers=headers,
                expires_at=expires_at.isoformat(),
                kind=kind,
                filename=filename,
            )
        )

    if tickets:
        UploadTicket.objects.bulk_create(tickets)
        audit.record(
            "UPLOAD_PRESIGN",
            request=request,
            object_type="UploadTicket",
            object_id=str(len(tickets)),
            payload={
                "count": len(tickets),
                "feature": feature.slug,
                "category": category.slug,
                "subcategory": subcategory.slug if subcategory else None,
                "rejected": len(rejections),
            },
        )
        logger.info(
            "Presigned %s uploads",
            len(tickets),
            extra={
                "event": "presign_batch",
                "count": len(tickets),
                "rejected": len(rejections),
                "feature": feature.slug,
            },
        )

    return [slot.as_dict() for slot in slots], rejections


def _validate_preview_spec(filename: str, content_type: str, size: int) -> None:
    """Previews must be modest images; nothing else is ever accepted as one."""
    allowed = {"image/jpeg", "image/png", "image/webp"}
    if content_type not in allowed:
        raise validators.ValidationFailure(
            f"A preview must be one of {', '.join(sorted(allowed))}.",
            code="bad_preview_mime",
        )
    if size <= 0:
        raise validators.ValidationFailure(
            "Preview size must be greater than zero.", code="empty_file"
        )
    max_preview = 5 * 1024 * 1024
    if size > max_preview:
        raise validators.ValidationFailure(
            f"Preview is {size} bytes, above the {max_preview} byte limit.",
            code="preview_too_large",
        )
    if filename and "." in filename:
        actual = filename.rsplit(".", 1)[-1].lower()
        expected = storage.extension_for_mime(content_type)
        if actual not in {expected, "jpeg" if expected == "jpg" else expected}:
            raise validators.ValidationFailure(
                f"Preview extension '.{actual}' does not match '{content_type}'.",
                code="extension_mismatch",
            )


# --------------------------------------------------------------------------- #
# Commit
# --------------------------------------------------------------------------- #


def commit_uploads(
    *, user, items: list[dict[str, Any]], request=None  # noqa: ANN001
) -> list[dict[str, Any]]:
    """Commit a batch. Each item succeeds or fails independently."""
    results: list[ItemResult] = []

    for index, spec in enumerate(items):
        try:
            item = _commit_single(user=user, spec=spec, request=request)
        except validators.ValidationFailure as exc:
            results.append(
                ItemResult(index=index, ok=False, error=exc.message, code=exc.code)
            )
        except ApiError as exc:
            results.append(
                ItemResult(index=index, ok=False, error=exc.message, code="rejected")
            )
        except storage.StorageError as exc:
            logger.error(
                "Commit failed on storage",
                extra={"event": "commit_storage_error", "error": str(exc)},
            )
            results.append(
                ItemResult(
                    index=index,
                    ok=False,
                    error="Storage is unavailable. Try again shortly.",
                    code="storage_unavailable",
                )
            )
        except Exception as exc:  # pragma: no cover - unexpected, keep the batch alive
            logger.exception(
                "Unexpected commit failure",
                extra={"event": "commit_unexpected", "error": str(exc)},
            )
            results.append(
                ItemResult(
                    index=index,
                    ok=False,
                    error="Unexpected failure while committing this item.",
                    code="internal_error",
                )
            )
        else:
            results.append(
                ItemResult(
                    index=index,
                    ok=True,
                    item_id=str(item.id),
                    detail={
                        "media_type": item.media_type,
                        "width": item.width,
                        "height": item.height,
                        "image_url": storage.public_url(item.file_key),
                        "preview_url": storage.public_url(item.preview_key),
                    },
                )
            )

    succeeded = sum(1 for r in results if r.ok)
    logger.info(
        "Committed %s of %s uploads",
        succeeded,
        len(items),
        extra={
            "event": "commit_batch",
            "succeeded": succeeded,
            "failed": len(items) - succeeded,
        },
    )
    return [result.as_dict() for result in results]


def _load_ticket(ticket_id: str, user, kind: str) -> UploadTicket:  # noqa: ANN001
    """
    Fetch and authorise a ticket.

    A single indistinguishable message covers unknown, expired, already-used and
    someone-else's tickets, so this cannot be used to probe which ids exist.
    """
    try:
        parsed = uuid.UUID(str(ticket_id))
    except (ValueError, TypeError, AttributeError):
        raise validators.ValidationFailure(
            "'ticket_id' is not a valid identifier.", code="bad_ticket"
        ) from None

    ticket = (
        UploadTicket.objects.select_related("feature", "category", "subcategory")
        .filter(pk=parsed)
        .first()
    )
    if ticket is None or not ticket.is_redeemable_by(user):
        raise validators.ValidationFailure(
            "Ticket is unknown, expired, already used, or not yours.",
            code="ticket_unusable",
        )
    if ticket.kind != kind:
        raise validators.ValidationFailure(
            f"Ticket is a {ticket.kind} ticket but was supplied as {kind}.",
            code="ticket_wrong_kind",
        )
    return ticket


def _commit_single(*, user, spec: dict[str, Any], request=None) -> MediaItem:  # noqa: ANN001
    """Validate one uploaded object and create its MediaItem."""
    asset_ticket = _load_ticket(spec.get("asset_ticket_id"), user, TicketKind.ASSET)
    feature = asset_ticket.feature

    # 1. Confirm what actually landed, and identify it from its bytes.
    meta, sniffed_mime = validators.verify_stored_object(
        key=asset_ticket.object_key,
        expected_mime=asset_ticket.declared_mime,
        expected_size=asset_ticket.declared_bytes,
    )
    media_type = validators.media_type_for_mime(sniffed_mime)

    # 2. Reject a re-upload of bytes already in the catalog.
    duplicate = (
        MediaItem.objects.filter(file_etag=meta.etag, file_bytes=meta.size)
        .exclude(status=ItemStatus.ARCHIVED)
        .values_list("id", flat=True)
        .first()
    )
    if meta.etag and duplicate:
        raise validators.ValidationFailure(
            f"These exact bytes are already in the catalog as item {duplicate}.",
            code="duplicate_file",
        )

    # 3. Type-specific validation and metadata extraction.
    probe = validators.ImageProbe()
    zip_probe = None
    generated_preview: tuple[bytes, str] | None = None

    if media_type in {"IMAGE", "GIF"}:
        if meta.size <= settings.INLINE_PROCESS_MAX_BYTES:
            data = storage.get_object_bytes(asset_ticket.object_key, meta.size)
            probe = validators.probe_image(data, feature)
            generated_preview = validators.make_preview(
                data, settings.PREVIEW_MAX_EDGE, settings.PREVIEW_JPEG_QUALITY
            )
        elif not spec.get("preview_ticket_id"):
            # Too big to pull into this process, and no preview supplied.
            raise validators.ValidationFailure(
                f"Images above {settings.INLINE_PROCESS_MAX_BYTES} bytes must be "
                "committed with a 'preview_ticket_id'.",
                code="preview_required",
            )
    elif media_type == "ZIP":
        zip_probe = validators.probe_zip_from_storage(
            asset_ticket.object_key, meta.size, feature
        )
    elif media_type == "VIDEO":
        # There is no ffmpeg on the target hosts, so a poster cannot be derived.
        if not spec.get("preview_ticket_id"):
            raise validators.ValidationFailure(
                "Video uploads require a 'preview_ticket_id' for the poster frame.",
                code="preview_required",
            )

    # 4. Resolve the preview: an uploaded one wins over a generated one.
    preview_ticket = None
    preview_key = ""
    preview_bytes = 0
    preview_mime = ""

    if spec.get("preview_ticket_id"):
        preview_ticket = _load_ticket(
            spec["preview_ticket_id"], user, TicketKind.PREVIEW
        )
        preview_meta, preview_sniffed = validators.verify_stored_object(
            key=preview_ticket.object_key,
            expected_mime=preview_ticket.declared_mime,
            expected_size=preview_ticket.declared_bytes,
        )
        if not preview_sniffed.startswith("image/"):
            raise validators.ValidationFailure(
                "The preview object is not an image.", code="bad_preview"
            )
        preview_probe = validators.probe_image(
            storage.get_object_bytes(preview_ticket.object_key, preview_meta.size),
            feature,
        )
        preview_key = preview_ticket.object_key
        preview_bytes = preview_meta.size
        preview_mime = preview_meta.content_type or preview_sniffed
        # A video has no intrinsic dimensions we can read, so the poster's are
        # the best available answer for the app's layout.
        if probe.width is None:
            probe = preview_probe
    elif generated_preview is not None:
        data, mime = generated_preview
        preview_key = storage.build_object_key(
            feature_slug=feature.slug,
            category_slug=asset_ticket.category.slug,
            kind="previews",
            unique_id=uuid.uuid4().hex,
            mime=mime,
        )
        stored = storage.put_bytes(key=preview_key, data=data, content_type=mime)
        preview_bytes = stored.size
        preview_mime = mime

    # 5. Create the row. One transaction per item keeps a batch partial-safe.
    with transaction.atomic():
        item = MediaItem(
            feature=feature,
            category=asset_ticket.category,
            subcategory=asset_ticket.subcategory,
            name=str(spec.get("name") or asset_ticket.declared_name or "")[:200],
            premium=bool(spec.get("premium", False)),
            priority=int(spec.get("priority") or 0),
            media_type=media_type,
            is_live=_resolve_is_live(spec.get("is_live"), media_type),
            file_key=asset_ticket.object_key,
            file_bytes=meta.size,
            file_mime=sniffed_mime,
            file_etag=meta.etag,
            preview_key=preview_key,
            preview_bytes=preview_bytes,
            preview_mime=preview_mime,
            width=probe.width,
            height=probe.height,
            duration_ms=_coerce_optional_int(spec.get("duration_ms")),
            dominant_color=probe.dominant_color,
            zip_entries=zip_probe.entries if zip_probe else None,
            zip_has_conf=zip_probe.has_conf if zip_probe else None,
            status=ItemStatus.READY,
            is_active=True,
        )
        # full_clean catches a cross-category subcategory in the same place the
        # admin would; migration 0003 backs it with a database constraint.
        item.full_clean(exclude=["file_etag", "file_mime", "preview_mime"])
        item.save()

        tag_slugs = spec.get("tags") or []
        if tag_slugs:
            item.tags.set(_resolve_tags(tag_slugs))

        UploadTicket.objects.filter(
            pk__in=[t.pk for t in (asset_ticket, preview_ticket) if t]
        ).update(status=TicketStatus.COMMITTED)

        _bump_counts(item, delta=1)

    audit.record(
        "UPLOAD_COMMIT",
        request=request,
        object_type="MediaItem",
        object_id=str(item.id),
        payload={
            "file_key": item.file_key,
            "media_type": item.media_type,
            "bytes": item.file_bytes,
            "feature": feature.slug,
            "category": asset_ticket.category.slug,
        },
    )
    return item


def _resolve_is_live(supplied: Any, media_type: str) -> bool:
    """
    Default `is_live` from the media type, but let the uploader override.

    A GIF, video or Shimeji pack is animated by nature; a static image is not.
    Defaulting saves the uploader a decision on every one of fifty files.
    """
    if supplied is not None:
        return bool(supplied)
    return media_type in {"GIF", "VIDEO", "ZIP"}


def _coerce_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise validators.ValidationFailure(
            "'duration_ms' must be an integer.", code="bad_duration"
        ) from None
    return parsed if parsed >= 0 else None


def _resolve_tags(slugs: list[str]) -> list[Tag]:
    """Map slugs onto Tag rows, creating any that do not exist yet."""
    cleaned = {
        str(s).strip().lower()[:64] for s in slugs if str(s).strip()
    }
    if not cleaned:
        return []
    existing = {t.slug: t for t in Tag.objects.filter(slug__in=cleaned)}
    missing = cleaned - existing.keys()
    if missing:
        Tag.objects.bulk_create(
            [Tag(slug=slug, name=slug.replace("-", " ").title()) for slug in missing],
            ignore_conflicts=True,
        )
        existing.update({t.slug: t for t in Tag.objects.filter(slug__in=cleaned)})
    return list(existing.values())


def _bump_counts(item: MediaItem, delta: int) -> None:
    """
    Adjust the denormalized counters.

    F() expressions so two concurrent commits cannot lose an increment, and
    Greatest() so a counter can never be driven negative by a double decrement.
    The reconcile command repairs any drift regardless.
    """
    from django.db.models import F, Value
    from django.db.models.functions import Greatest

    Category.objects.filter(pk=item.category_id).update(
        item_count=Greatest(F("item_count") + delta, Value(0))
    )
    if item.subcategory_id:
        Subcategory.objects.filter(pk=item.subcategory_id).update(
            item_count=Greatest(F("item_count") + delta, Value(0))
        )


# --------------------------------------------------------------------------- #
# Abort
# --------------------------------------------------------------------------- #


def abort_uploads(*, user, ticket_ids: list[str], request=None) -> dict[str, Any]:  # noqa: ANN001
    """Cancel issued tickets and delete whatever bytes already reached B2."""
    aborted: list[str] = []
    skipped: list[str] = []

    for raw_id in ticket_ids:
        try:
            parsed = uuid.UUID(str(raw_id))
        except (ValueError, TypeError):
            skipped.append(str(raw_id))
            continue

        ticket = UploadTicket.objects.filter(
            pk=parsed, created_by=user, status=TicketStatus.ISSUED
        ).first()
        if ticket is None:
            skipped.append(str(raw_id))
            continue

        storage.delete_object(ticket.object_key)
        ticket.status = TicketStatus.ABORTED
        ticket.save(update_fields=["status", "updated_at"])
        aborted.append(str(ticket.id))

    if aborted:
        audit.record(
            "UPLOAD_ABORT",
            request=request,
            object_type="UploadTicket",
            object_id=",".join(aborted[:3]),
            payload={"aborted": aborted, "skipped": skipped},
        )
    return {"aborted": aborted, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Archive
# --------------------------------------------------------------------------- #


def archive_item(*, item: MediaItem, request=None) -> MediaItem:  # noqa: ANN001
    """
    Soft-delete: hide from the API now, purge bytes later.

    Deleting the B2 objects inline would mean a storage hiccup either rolls back
    a delete the admin believes succeeded, or orphans the row. Flipping status is
    atomic and local; the reaper removes the bytes after the retention window,
    which also leaves a window to undo a mistake.
    """
    item.status = ItemStatus.ARCHIVED
    item.is_active = False
    item.archived_at = timezone.now()
    item.save(update_fields=["status", "is_active", "archived_at", "updated_at"])
    _bump_counts(item, delta=-1)

    audit.record(
        "ITEM_ARCHIVE",
        request=request,
        object_type="MediaItem",
        object_id=str(item.id),
        payload={"file_key": item.file_key, "preview_key": item.preview_key},
    )
    return item
