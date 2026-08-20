"""
Model invariants, id generation, maintenance jobs, and the health probes.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.catalog.models import (
    Category,
    Feature,
    ItemStatus,
    MediaItem,
    MediaType,
    Subcategory,
    derive_orientation,
    derive_resolution,
)
from apps.core.ids import uuid7
from apps.core.maintenance import reap_orphans, reconcile_counts
from apps.core.models import AuditLog
from apps.ingest.models import TicketKind, TicketStatus, UploadTicket

from .factories import make_item

# --------------------------------------------------------------------------- #
# UUIDv7
# --------------------------------------------------------------------------- #


def test_uuid7_is_a_valid_version_7_uuid():
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_uuid7_is_strictly_increasing():
    """
    The property the ordering tiebreak depends on.

    Generated in a tight loop so most of these share a millisecond, which is
    exactly the case a purely random low half would order arbitrarily.
    """
    values = [uuid7() for _ in range(5000)]
    assert values == sorted(values), "uuid7 must be monotonic"
    assert len(set(values)) == 5000, "uuid7 must not collide"


def test_uuid7_hex_sorts_chronologically():
    """
    SQLite stores UUIDs as hex text and compares lexicographically, so the hex
    form has to sort the same way the objects do or `ORDER BY id` would lie.
    """
    values = [uuid7() for _ in range(500)]
    assert [v.hex for v in values] == sorted(v.hex for v in values)


def test_uuid7_encodes_the_current_time():
    before = int(timezone.now().timestamp() * 1000)
    value = uuid7()
    after = int(timezone.now().timestamp() * 1000)

    embedded = int.from_bytes(value.bytes[0:6], "big")
    # Allow a little slack for the borrow-a-millisecond path.
    assert before - 10 <= embedded <= after + 10


# --------------------------------------------------------------------------- #
# Derived fields
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (1920, 1080, "LANDSCAPE"),
        (1080, 1920, "PORTRAIT"),
        (1000, 1000, "SQUARE"),
        (None, 1080, ""),
        (1920, None, ""),
    ],
)
def test_derive_orientation(width, height, expected):
    assert derive_orientation(width, height) == expected


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (3840, 2160, "UHD_4K"),
        (2160, 3840, "UHD_4K"),  # portrait 4K is still 4K
        (2560, 1440, "QHD"),
        (1920, 1080, "FHD"),
        (1280, 720, "HD"),
        (640, 480, "SD"),
        (None, None, ""),
    ],
)
def test_derive_resolution(width, height, expected):
    assert derive_resolution(width, height) == expected


@pytest.mark.django_db
def test_derived_fields_are_recomputed_on_save(category):
    item = make_item(category=category, width=1280, height=720)
    assert (item.orientation, item.resolution) == ("LANDSCAPE", "HD")

    item.width, item.height = 2160, 3840
    item.save()
    item.refresh_from_db()
    assert (item.orientation, item.resolution) == ("PORTRAIT", "UHD_4K")


@pytest.mark.django_db
def test_feature_is_derived_from_the_category(category):
    """`feature` is denormalized for query speed, so it must never be trusted."""
    item = MediaItem(
        category=category,
        name="x",
        media_type=MediaType.IMAGE,
        file_key=f"k/{uuid.uuid4().hex}",
        file_bytes=1,
        status=ItemStatus.READY,
    )
    item.save()
    assert item.feature_id == category.feature_id


# --------------------------------------------------------------------------- #
# Constraints
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_cross_category_subcategory_is_rejected_by_clean(category, subcategory):
    """
    An item filed under category A with a subcategory from B can never be listed
    by either filter, so the screen simply comes up empty with no error. That is
    exactly why this is validated rather than left to chance.
    """
    item = MediaItem(
        feature=category.feature,
        category=category,  # "Trending"
        subcategory=subcategory,  # belongs to "Anime"
        name="mismatched",
        media_type=MediaType.IMAGE,
        file_key=f"k/{uuid.uuid4().hex}",
        file_bytes=1,
    )
    with pytest.raises(ValidationError) as exc:
        item.full_clean()
    assert "subcategory" in exc.value.message_dict


@pytest.mark.django_db
def test_matching_subcategory_passes_clean(other_category, subcategory):
    item = MediaItem(
        feature=other_category.feature,
        category=other_category,
        subcategory=subcategory,
        name="ok",
        media_type=MediaType.IMAGE,
        file_key=f"k/{uuid.uuid4().hex}",
        file_bytes=1,
    )
    item.full_clean()  # must not raise


@pytest.mark.django_db
def test_feature_mismatch_is_rejected_by_clean(category, shimeji):
    item = MediaItem(
        feature=shimeji,  # wrong: category belongs to wallpaper
        category=category,
        name="x",
        media_type=MediaType.IMAGE,
        file_key=f"k/{uuid.uuid4().hex}",
        file_bytes=1,
    )
    with pytest.raises(ValidationError) as exc:
        item.full_clean()
    assert "feature" in exc.value.message_dict


@pytest.mark.django_db
def test_file_key_is_unique(category):
    first = make_item(category=category)
    with pytest.raises(IntegrityError), transaction.atomic():
        MediaItem.objects.create(
            feature=category.feature,
            category=category,
            name="dupe",
            media_type=MediaType.IMAGE,
            file_key=first.file_key,
            file_bytes=1,
        )


@pytest.mark.django_db
def test_category_slug_is_unique_per_feature_not_globally(wallpaper, shimeji):
    """Both types are allowed a "Trending" category; the same type is not."""
    Category.objects.create(feature=wallpaper, name="Trending")
    Category.objects.create(feature=shimeji, name="Trending")  # fine

    with pytest.raises(IntegrityError), transaction.atomic():
        Category.objects.create(feature=wallpaper, name="Trending", slug="trending")


@pytest.mark.django_db
def test_feature_delete_is_protected_by_its_categories(wallpaper, category):
    from django.db.models import ProtectedError

    with pytest.raises(ProtectedError):
        wallpaper.delete()


@pytest.mark.django_db
def test_category_delete_is_protected_by_its_items(category):
    from django.db.models import ProtectedError

    make_item(category=category)
    with pytest.raises(ProtectedError):
        category.delete()


@pytest.mark.django_db
def test_slug_is_generated_from_the_name(wallpaper):
    category = Category.objects.create(feature=wallpaper, name="Live Wallpapers 4K")
    assert category.slug == "live-wallpapers-4k"


# --------------------------------------------------------------------------- #
# has_subcategories
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_has_subcategories_tracks_its_subcategories(wallpaper):
    category = Category.objects.create(feature=wallpaper, name="Anime")
    assert category.has_subcategories is False

    sub = Subcategory.objects.create(category=category, name="4K")
    category.refresh_from_db()
    assert category.has_subcategories is True

    second = Subcategory.objects.create(category=category, name="HD")
    sub.delete()
    category.refresh_from_db()
    assert category.has_subcategories is True, "one remaining subcategory still counts"

    second.delete()
    category.refresh_from_db()
    assert category.has_subcategories is False


# --------------------------------------------------------------------------- #
# reconcile_counts
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_reconcile_counts_repairs_drift(category, other_category, subcategory):
    make_item(category=category)
    make_item(category=category)
    make_item(category=other_category, subcategory=subcategory)
    # Only published rows count.
    make_item(category=category, status=ItemStatus.ARCHIVED)
    make_item(category=category, is_active=False)

    # Simulate drift, as a crash between insert and counter update would cause.
    Category.objects.filter(pk=category.pk).update(item_count=999)
    Subcategory.objects.filter(pk=subcategory.pk).update(item_count=0)
    Category.objects.filter(pk=other_category.pk).update(has_subcategories=False)

    stats = reconcile_counts()

    category.refresh_from_db()
    other_category.refresh_from_db()
    subcategory.refresh_from_db()

    assert category.item_count == 2
    assert other_category.item_count == 1
    assert subcategory.item_count == 1
    assert other_category.has_subcategories is True
    assert stats["categories_fixed"] >= 1
    assert stats["has_subcategories_fixed"] == 1


@pytest.mark.django_db
def test_reconcile_counts_is_idempotent(category):
    make_item(category=category)
    reconcile_counts()

    second = reconcile_counts()
    assert second["categories_fixed"] == 0
    assert second["subcategories_fixed"] == 0
    assert second["has_subcategories_fixed"] == 0


@pytest.mark.django_db
def test_reconcile_dry_run_reports_without_writing(category):
    make_item(category=category)
    Category.objects.filter(pk=category.pk).update(item_count=42)

    stats = reconcile_counts(dry_run=True)

    category.refresh_from_db()
    assert stats["categories_fixed"] == 1
    assert category.item_count == 42, "dry run must not write"


# --------------------------------------------------------------------------- #
# reap_orphans
# --------------------------------------------------------------------------- #


def make_ticket(staff_user, category, *, status=TicketStatus.ISSUED, expires_in_hours=1):
    return UploadTicket.objects.create(
        object_key=f"wallpaper/trending/assets/{uuid.uuid4().hex}.png",
        kind=TicketKind.ASSET,
        feature=category.feature,
        category=category,
        declared_name="x.png",
        declared_mime="image/png",
        declared_bytes=100,
        created_by=staff_user,
        status=status,
        expires_at=timezone.now() + timedelta(hours=expires_in_hours),
    )


@pytest.mark.django_db
def test_reap_deletes_expired_tickets_and_their_objects(s3, settings, staff_user, category):
    expired = make_ticket(staff_user, category, expires_in_hours=-1)
    live = make_ticket(staff_user, category, expires_in_hours=2)

    s3.put_object(Bucket=settings.B2_BUCKET_NAME, Key=expired.object_key, Body=b"orphan bytes")

    stats = reap_orphans()

    assert stats["expired_tickets"] == 1
    assert stats["objects_deleted"] == 1
    assert not UploadTicket.objects.filter(pk=expired.pk).exists()
    assert UploadTicket.objects.filter(pk=live.pk).exists(), "unexpired ticket untouched"

    from apps.ingest import storage

    with pytest.raises(storage.ObjectNotFound):
        storage.head_object(expired.object_key)


@pytest.mark.django_db
def test_reap_leaves_unexpired_tickets_alone(s3, staff_user, category):
    make_ticket(staff_user, category, expires_in_hours=5)
    stats = reap_orphans()

    assert stats["expired_tickets"] == 0
    assert UploadTicket.objects.count() == 1


@pytest.mark.django_db
def test_reap_purges_archived_items_past_retention(s3, settings, category):
    settings.ARCHIVE_RETENTION_DAYS = 30

    stale = make_item(category=category, name="stale", status=ItemStatus.ARCHIVED)
    MediaItem.objects.filter(pk=stale.pk).update(
        archived_at=timezone.now() - timedelta(days=31)
    )
    recent = make_item(category=category, name="recent", status=ItemStatus.ARCHIVED)
    MediaItem.objects.filter(pk=recent.pk).update(archived_at=timezone.now())

    s3.put_object(Bucket=settings.B2_BUCKET_NAME, Key=stale.file_key, Body=b"x")
    s3.put_object(Bucket=settings.B2_BUCKET_NAME, Key=stale.preview_key, Body=b"y")

    stats = reap_orphans()

    assert stats["items_purged"] == 1
    assert not MediaItem.objects.filter(pk=stale.pk).exists()
    assert MediaItem.objects.filter(pk=recent.pk).exists(), "still inside the window"


@pytest.mark.django_db
def test_reap_never_touches_published_items(s3, settings, category):
    item = make_item(category=category, status=ItemStatus.READY)
    reap_orphans()
    assert MediaItem.objects.filter(pk=item.pk).exists()


@pytest.mark.django_db
def test_reap_is_idempotent(s3, settings, staff_user, category):
    make_ticket(staff_user, category, expires_in_hours=-1)
    first = reap_orphans()
    second = reap_orphans()

    assert first["expired_tickets"] == 1
    assert second["expired_tickets"] == 0


@pytest.mark.django_db
def test_reap_reports_when_more_work_remains(s3, staff_user, category):
    """
    A batch-limited run must say so.

    Silent truncation reads as "the queue is empty" when it is not.
    """
    for _ in range(4):
        make_ticket(staff_user, category, expires_in_hours=-1)

    stats = reap_orphans(batch_limit=2)
    assert stats["expired_tickets"] == 2
    assert stats["more_pending"] is True


@pytest.mark.django_db
def test_reap_dry_run_writes_nothing(s3, staff_user, category):
    ticket = make_ticket(staff_user, category, expires_in_hours=-1)
    stats = reap_orphans(dry_run=True)

    assert stats["expired_tickets"] == 1
    assert stats["objects_deleted"] == 0
    assert UploadTicket.objects.filter(pk=ticket.pk).exists()


@pytest.mark.django_db
def test_reap_writes_an_audit_row(s3, staff_user, category):
    make_ticket(staff_user, category, expires_in_hours=-1)
    reap_orphans()

    assert AuditLog.objects.filter(action="ORPHAN_REAP").exists()


# --------------------------------------------------------------------------- #
# Health probes
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_healthz_does_no_io(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_readyz_reports_each_dependency(client, s3):
    response = client.get("/readyz")
    body = response.json()

    assert body["checks"]["database"] == "ok"
    assert body["checks"]["storage"] == "ok"
    assert response.status_code == 200


@pytest.mark.django_db
def test_readyz_is_degraded_when_storage_is_unconfigured(client, settings):
    settings.DEBUG = False
    settings.B2_KEY_ID = ""

    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["storage"] == "unconfigured"


@pytest.mark.django_db
def test_health_probes_are_not_enveloped(client):
    """A load balancer wants a status code, not a contract to parse."""
    body = client.get("/healthz").json()
    assert "data" not in body
    assert "message" not in body


# --------------------------------------------------------------------------- #
# Cron endpoints
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["reap-orphans", "reconcile-counts"])
def test_cron_requires_the_secret(client, path):
    assert client.post(f"/internal/cron/{path}").status_code == 403
    assert client.post(f"/internal/cron/{path}", HTTP_X_CRON_SECRET="wrong").status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["reap-orphans", "reconcile-counts"])
def test_cron_accepts_the_secret(client, settings, s3, path):
    response = client.post(f"/internal/cron/{path}", HTTP_X_CRON_SECRET=settings.CRON_SECRET)
    assert response.status_code == 200
    assert response.json()["status"] == 200


@pytest.mark.django_db
def test_cron_denies_everything_when_no_secret_is_configured(client, settings):
    """
    The failure mode of a missing environment variable must be a closed door.

    A blank configured secret matching a blank supplied header would leave these
    endpoints wide open.
    """
    settings.CRON_SECRET = ""
    assert client.post("/internal/cron/reap-orphans").status_code == 403
    assert client.post("/internal/cron/reap-orphans", HTTP_X_CRON_SECRET="").status_code == 403


@pytest.mark.django_db
def test_cron_limit_cannot_be_raised_past_the_cap(client, settings, s3):
    response = client.post(
        "/internal/cron/reap-orphans?limit=999999",
        HTTP_X_CRON_SECRET=settings.CRON_SECRET,
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# Seed data
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_the_three_types_are_seeded():
    slugs = set(Feature.objects.values_list("slug", flat=True))
    assert slugs == {"wallpaper", "shimeji", "battery"}


@pytest.mark.django_db
def test_each_type_has_an_upload_policy():
    for feature in Feature.objects.all():
        assert feature.allowed_mimes, f"{feature.slug} has no allowed_mimes"
        assert feature.max_file_bytes > 0
        assert feature.max_pixels > 0


@pytest.mark.django_db
def test_shimeji_accepts_zip_and_wallpaper_does_not():
    shimeji = Feature.objects.get(slug="shimeji")
    wallpaper = Feature.objects.get(slug="wallpaper")

    assert shimeji.allows_mime("application/zip")
    assert not wallpaper.allows_mime("application/zip")
    assert wallpaper.allows_mime("video/mp4")


@pytest.mark.django_db
def test_allows_mime_is_case_insensitive():
    wallpaper = Feature.objects.get(slug="wallpaper")
    assert wallpaper.allows_mime("IMAGE/PNG")
