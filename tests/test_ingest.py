"""
The ingest pipeline: presign, real upload, commit, and every rejection path.

These are the tests that matter most. The public API only reads rows; this is
the code that decides what becomes a row in the first place, and it is the only
place where untrusted bytes are handled.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from django.urls import reverse

from apps.catalog.models import Category, ItemStatus, MediaItem, MediaType
from apps.ingest import storage
from apps.ingest.models import TicketStatus, UploadTicket

from .conftest import make_gif, make_jpeg, make_mp4, make_png, make_zip

pytestmark = pytest.mark.django_db


PRESIGN_URL = "/api/v1/admin/uploads/presign"
COMMIT_URL = "/api/v1/admin/uploads/commit"
ABORT_URL = "/api/v1/admin/uploads/abort"


def post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def presign(client, *, feature_slug, category, files, subcategory=None):
    body = {"type": feature_slug, "category_id": str(category.id), "files": files}
    if subcategory is not None:
        body["subcategory_id"] = str(subcategory.id)
    return post(client, PRESIGN_URL, body)


def upload(s3, settings, key: str, data: bytes, content_type: str) -> None:
    """Stand in for the browser's direct PUT to B2."""
    s3.put_object(Bucket=settings.B2_BUCKET_NAME, Key=key, Body=data, ContentType=content_type)


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


def test_image_round_trip_generates_preview_and_metadata(
    s3, settings, staff_client, wallpaper, category
):
    """presign -> PUT -> commit produces a READY item with a generated preview."""
    data = make_png(1080, 1920)

    response = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "wall.png", "content_type": "image/png", "size": len(data)}],
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["status"] == 201
    slot = body["data"]["slots"][0]

    # The key is server-minted and namespaced; the client's filename is nowhere in it.
    assert slot["object_key"].startswith("wallpaper/trending/assets/")
    assert slot["object_key"].endswith(".png")
    assert "wall.png" not in slot["object_key"]
    assert slot["required_headers"]["Content-Type"] == "image/png"

    upload(s3, settings, slot["object_key"], data, "image/png")

    response = post(
        staff_client,
        COMMIT_URL,
        {
            "items": [
                {
                    "asset_ticket_id": slot["ticket_id"],
                    "name": "Neon Wall",
                    "priority": 2,
                    "tags": ["anime", "neon"],
                }
            ]
        },
    )
    assert response.status_code == 201, response.content
    result = response.json()["data"]["results"][0]
    assert result["ok"] is True

    item = MediaItem.objects.get(pk=result["id"])
    assert item.status == ItemStatus.READY
    assert item.media_type == MediaType.IMAGE
    assert item.is_live is False  # static image defaults to not live
    assert (item.width, item.height) == (1080, 1920)
    assert item.orientation == "PORTRAIT"  # computed from the dimensions
    assert item.resolution == "FHD"
    assert item.dominant_color.startswith("#")
    assert item.file_bytes == len(data)
    assert item.file_etag  # captured from B2 for dedup
    assert sorted(t.slug for t in item.tags.all()) == ["anime", "neon"]

    # A preview was generated server-side and really exists in the bucket.
    assert item.preview_key
    head = s3.head_object(Bucket=settings.B2_BUCKET_NAME, Key=item.preview_key)
    assert head["ContentType"] == "image/jpeg"
    assert head["ContentLength"] > 0

    # The ticket is spent, so it cannot be replayed.
    assert UploadTicket.objects.get(pk=slot["ticket_id"]).status == TicketStatus.COMMITTED


def test_zip_round_trip_records_shimeji_structure(s3, settings, staff_client, shimeji):
    category = Category.objects.create(feature=shimeji, name="Trending", priority=1)
    data = make_zip()

    response = presign(
        staff_client,
        feature_slug="shimeji",
        category=category,
        files=[{"filename": "pack.zip", "content_type": "application/zip", "size": len(data)}],
    )
    slot = response.json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], data, "application/zip")

    response = post(
        staff_client,
        COMMIT_URL,
        {"items": [{"asset_ticket_id": slot["ticket_id"], "name": "Hatsune"}]},
    )
    assert response.status_code == 201, response.content

    item = MediaItem.objects.get(pk=response.json()["data"]["results"][0]["id"])
    assert item.media_type == MediaType.ZIP
    assert item.is_live is True  # archives default to live
    assert item.zip_entries == 4
    assert item.zip_has_conf is True  # conf/ was detected
    assert item.width is None  # nothing to measure in an archive


def test_gif_round_trip(s3, settings, staff_client, battery):
    category = Category.objects.create(feature=battery, name="Latest", priority=1)
    data = make_gif()

    response = presign(
        staff_client,
        feature_slug="battery",
        category=category,
        files=[{"filename": "pulse.gif", "content_type": "image/gif", "size": len(data)}],
    )
    slot = response.json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], data, "image/gif")

    response = post(
        staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]}
    )
    assert response.status_code == 201, response.content
    item = MediaItem.objects.get(pk=response.json()["data"]["results"][0]["id"])
    assert item.media_type == MediaType.GIF
    assert item.is_live is True
    assert item.preview_key  # Pillow can render a GIF's first frame


def test_video_requires_supplied_preview_then_succeeds(
    s3, settings, staff_client, wallpaper, category
):
    """
    There is no ffmpeg on the target hosts, so a poster frame cannot be derived
    and must be uploaded alongside the video.
    """
    video = make_mp4()
    poster = make_jpeg(1080, 1920)

    response = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[
            {"filename": "live.mp4", "content_type": "video/mp4", "size": len(video)},
            {
                "filename": "poster.jpg",
                "content_type": "image/jpeg",
                "size": len(poster),
                "kind": "PREVIEW",
            },
        ],
    )
    assert response.status_code == 201, response.content
    slots = {s["kind"]: s for s in response.json()["data"]["slots"]}
    assert slots["PREVIEW"]["object_key"].startswith("wallpaper/trending/previews/")

    upload(s3, settings, slots["ASSET"]["object_key"], video, "video/mp4")
    upload(s3, settings, slots["PREVIEW"]["object_key"], poster, "image/jpeg")

    # Without the preview ticket, commit is refused.
    refused = post(
        staff_client,
        COMMIT_URL,
        {"items": [{"asset_ticket_id": slots["ASSET"]["ticket_id"]}]},
    )
    assert refused.status_code == 400
    assert refused.json()["data"]["results"][0]["code"] == "preview_required"

    # With it, the poster's dimensions stand in for the video's.
    response = post(
        staff_client,
        COMMIT_URL,
        {
            "items": [
                {
                    "asset_ticket_id": slots["ASSET"]["ticket_id"],
                    "preview_ticket_id": slots["PREVIEW"]["ticket_id"],
                    "name": "Live City",
                    "duration_ms": 6000,
                }
            ]
        },
    )
    assert response.status_code == 201, response.content
    item = MediaItem.objects.get(pk=response.json()["data"]["results"][0]["id"])
    assert item.media_type == MediaType.VIDEO
    assert item.is_live is True
    assert item.duration_ms == 6000
    assert (item.width, item.height) == (1080, 1920)
    assert item.preview_key == slots["PREVIEW"]["object_key"]


def test_bulk_upload_of_many_files(s3, settings, staff_client, wallpaper, category):
    """A 12-file batch: one presign call, one commit call, twelve rows."""
    payloads = [make_png(400 + i, 600) for i in range(12)]
    files = [
        {"filename": f"w{i}.png", "content_type": "image/png", "size": len(p)}
        for i, p in enumerate(payloads)
    ]

    response = presign(staff_client, feature_slug="wallpaper", category=category, files=files)
    assert response.status_code == 201
    slots = response.json()["data"]["slots"]
    assert len(slots) == 12

    for slot, payload in zip(slots, payloads, strict=True):
        upload(s3, settings, slot["object_key"], payload, "image/png")

    response = post(
        staff_client,
        COMMIT_URL,
        {
            "items": [
                {"asset_ticket_id": s["ticket_id"], "name": f"Bulk {i}"}
                for i, s in enumerate(slots)
            ]
        },
    )
    assert response.status_code == 201, response.content
    assert response.json()["data"]["committed_count"] == 12
    assert MediaItem.objects.filter(status=ItemStatus.READY).count() == 12


def test_partial_batch_failure_keeps_the_good_items(
    s3, settings, staff_client, wallpaper, category
):
    """
    One corrupt file in a batch must not discard the rest.

    This is why commit returns 207 with per-item results instead of a flat 400.
    """
    good = make_png(300, 300)
    files = [
        {"filename": "good.png", "content_type": "image/png", "size": len(good)},
        {"filename": "bad.png", "content_type": "image/png", "size": len(good)},
    ]
    slots = presign(
        staff_client, feature_slug="wallpaper", category=category, files=files
    ).json()["data"]["slots"]

    upload(s3, settings, slots[0]["object_key"], good, "image/png")
    # Same declared size, but the bytes are not a PNG at all.
    upload(s3, settings, slots[1]["object_key"], b"X" * len(good), "image/png")

    response = post(
        staff_client,
        COMMIT_URL,
        {"items": [{"asset_ticket_id": s["ticket_id"]} for s in slots]},
    )
    assert response.status_code == 207, response.content
    body = response.json()["data"]
    assert body["committed_count"] == 1
    assert body["failed_count"] == 1
    assert body["results"][0]["ok"] is True
    assert body["results"][1]["ok"] is False
    assert MediaItem.objects.count() == 1


def test_abort_deletes_the_uploaded_object(s3, settings, staff_client, category):
    data = make_png(100, 100)
    slot = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "x.png", "content_type": "image/png", "size": len(data)}],
    ).json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], data, "image/png")

    response = post(staff_client, ABORT_URL, {"ticket_ids": [slot["ticket_id"]]})
    assert response.status_code == 200
    assert response.json()["data"]["aborted"] == [slot["ticket_id"]]
    assert UploadTicket.objects.get(pk=slot["ticket_id"]).status == TicketStatus.ABORTED

    with pytest.raises(storage.ObjectNotFound):
        storage.head_object(slot["object_key"])


# --------------------------------------------------------------------------- #
# Presign-time rejections (cheap, before any bytes move)
# --------------------------------------------------------------------------- #


def test_presign_rejects_disallowed_mime(staff_client, category):
    response = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "doc.pdf", "content_type": "application/pdf", "size": 1000}],
    )
    assert response.status_code == 400
    rejected = response.json()["data"]["rejected"][0]
    assert rejected["code"] == "mime_not_allowed"
    assert "image/jpeg" in rejected["error"]  # tells the uploader what is allowed
    assert not UploadTicket.objects.exists()


def test_presign_rejects_oversized_file(staff_client, wallpaper, category):
    response = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[
            {
                "filename": "huge.png",
                "content_type": "image/png",
                "size": wallpaper.max_file_bytes + 1,
            }
        ],
    )
    assert response.status_code == 400
    assert response.json()["data"]["rejected"][0]["code"] == "file_too_large"


def test_presign_rejects_extension_mime_mismatch(staff_client, category):
    response = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "sneaky.exe", "content_type": "image/png", "size": 1000}],
    )
    assert response.status_code == 400
    assert response.json()["data"]["rejected"][0]["code"] == "extension_mismatch"


def test_presign_rejects_zero_size(staff_client, category):
    response = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "empty.png", "content_type": "image/png", "size": 0}],
    )
    # min_value=1 on the serializer catches this before the service does.
    assert response.status_code == 400


def test_presign_rejects_cross_type_category(staff_client, shimeji, category):
    """A wallpaper category cannot be used for a shimeji upload."""
    response = presign(
        staff_client,
        feature_slug="shimeji",
        category=category,
        files=[{"filename": "p.zip", "content_type": "application/zip", "size": 100}],
    )
    assert response.status_code == 400
    assert "category_id" in response.json()["errors"]


def test_presign_rejects_subcategory_from_another_category(staff_client, category, subcategory):
    """
    The check that prevents a permanently empty screen: an item filed under
    category A with a subcategory belonging to B can never be listed by either.
    """
    response = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        subcategory=subcategory,  # belongs to "Anime", not "Trending"
        files=[{"filename": "x.png", "content_type": "image/png", "size": 100}],
    )
    assert response.status_code == 400
    assert "subcategory_id" in response.json()["errors"]


def test_presign_enforces_bulk_cap(staff_client, category, settings):
    settings.UPLOAD_BULK_MAX_FILES = 3
    response = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[
            {"filename": f"w{i}.png", "content_type": "image/png", "size": 100}
            for i in range(4)
        ],
    )
    assert response.status_code == 400
    assert "files" in response.json()["errors"]


def test_presign_reports_mixed_batch_as_207(s3, staff_client, category):
    response = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[
            {"filename": "ok.png", "content_type": "image/png", "size": 500},
            {"filename": "bad.pdf", "content_type": "application/pdf", "size": 500},
        ],
    )
    assert response.status_code == 207
    body = response.json()["data"]
    assert body["accepted_count"] == 1
    assert body["rejected_count"] == 1


# --------------------------------------------------------------------------- #
# Commit-time rejections — the real gate
# --------------------------------------------------------------------------- #


def test_commit_rejects_exe_disguised_as_png(s3, settings, staff_client, category):
    """
    The headline check: a Windows executable declared as image/png.

    Content-Type is a client claim. Only the magic bytes are evidence.
    """
    exe = b"MZ\x90\x00\x03" + b"\x00" * 2000  # DOS/PE header
    slot = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "payload.png", "content_type": "image/png", "size": len(exe)}],
    ).json()["data"]["slots"][0]

    upload(s3, settings, slot["object_key"], exe, "image/png")

    response = post(
        staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]}
    )
    assert response.status_code == 400
    result = response.json()["data"]["results"][0]
    assert result["code"] == "unrecognised_content"
    assert not MediaItem.objects.exists()


def test_commit_rejects_wrong_content_type(s3, settings, staff_client, category):
    """A real GIF uploaded against a presign that declared PNG."""
    gif = make_gif()
    slot = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "x.png", "content_type": "image/png", "size": len(gif)}],
    ).json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], gif, "image/png")

    response = post(
        staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]}
    )
    assert response.status_code == 400
    assert response.json()["data"]["results"][0]["code"] == "content_mismatch"


def test_commit_rejects_size_mismatch(s3, settings, staff_client, category):
    """More bytes arrived than the presign was issued for."""
    data = make_png(200, 200)
    slot = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "x.png", "content_type": "image/png", "size": len(data)}],
    ).json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], data + b"extra padding", "image/png")

    response = post(
        staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]}
    )
    assert response.status_code == 400
    assert response.json()["data"]["results"][0]["code"] == "size_mismatch"


def test_commit_rejects_missing_object(s3, staff_client, category):
    """Ticket issued, PUT never performed."""
    slot = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "x.png", "content_type": "image/png", "size": 500}],
    ).json()["data"]["slots"][0]

    response = post(
        staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]}
    )
    assert response.status_code == 400
    assert response.json()["data"]["results"][0]["code"] == "object_missing"


def test_commit_rejects_zip_with_path_traversal(s3, settings, staff_client, shimeji):
    category = Category.objects.create(feature=shimeji, name="Packs", priority=1)
    evil = make_zip(
        {
            "conf/actions.xml": b"<x/>",
            "../../../etc/passwd": b"root:x:0:0",
        }
    )
    slot = presign(
        staff_client,
        feature_slug="shimeji",
        category=category,
        files=[{"filename": "evil.zip", "content_type": "application/zip", "size": len(evil)}],
    ).json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], evil, "application/zip")

    response = post(
        staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]}
    )
    assert response.status_code == 400
    assert response.json()["data"]["results"][0]["code"] == "zip_traversal"
    assert not MediaItem.objects.exists()


def test_commit_rejects_zip_bomb(s3, settings, staff_client, shimeji):
    """
    Highly compressible content: a small archive claiming a huge expansion.

    Caught from the central directory's declared sizes, so nothing is ever
    decompressed — the check cannot itself exhaust memory.
    """
    category = Category.objects.create(feature=shimeji, name="Packs", priority=1)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("conf/actions.xml", b"<x/>")
        archive.writestr("bomb.bin", b"\x00" * (40 * 1024 * 1024))
    bomb = buffer.getvalue()

    slot = presign(
        staff_client,
        feature_slug="shimeji",
        category=category,
        files=[{"filename": "bomb.zip", "content_type": "application/zip", "size": len(bomb)}],
    ).json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], bomb, "application/zip")

    response = post(
        staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]}
    )
    assert response.status_code == 400
    assert response.json()["data"]["results"][0]["code"] in {"zip_bomb", "zip_too_large"}


def test_commit_rejects_zip_with_too_many_entries(s3, settings, staff_client, shimeji):
    settings.ZIP_MAX_ENTRIES = 5
    category = Category.objects.create(feature=shimeji, name="Packs", priority=1)
    data = make_zip({f"img/f{i}.png": b"x" for i in range(10)})

    slot = presign(
        staff_client,
        feature_slug="shimeji",
        category=category,
        files=[{"filename": "many.zip", "content_type": "application/zip", "size": len(data)}],
    ).json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], data, "application/zip")

    response = post(
        staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]}
    )
    assert response.status_code == 400
    assert response.json()["data"]["results"][0]["code"] == "zip_too_many_entries"


def test_strict_zip_structure_requires_conf(s3, settings, staff_client, shimeji):
    """The per-feature strictness flag, when an operator turns it on."""
    shimeji.strict_zip_structure = True
    shimeji.save()
    category = Category.objects.create(feature=shimeji, name="Packs", priority=1)
    data = make_zip({"img/shime1.png": make_png(32, 32)})  # no conf/

    slot = presign(
        staff_client,
        feature_slug="shimeji",
        category=category,
        files=[
            {"filename": "noconf.zip", "content_type": "application/zip", "size": len(data)}
        ],
    ).json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], data, "application/zip")

    response = post(
        staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]}
    )
    assert response.status_code == 400
    assert response.json()["data"]["results"][0]["code"] == "zip_missing_conf"


def test_commit_rejects_image_over_pixel_budget(
    s3, settings, staff_client, wallpaper, category
):
    """Decompression-bomb defence: pixel count, not file size."""
    wallpaper.max_pixels = 10_000  # 100x100
    wallpaper.save()
    data = make_png(500, 500)  # 250,000 pixels

    slot = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "big.png", "content_type": "image/png", "size": len(data)}],
    ).json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], data, "image/png")

    response = post(
        staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]}
    )
    assert response.status_code == 400
    assert response.json()["data"]["results"][0]["code"] in {
        "too_many_pixels",
        "decompression_bomb",
    }


def test_commit_rejects_duplicate_bytes(s3, settings, staff_client, category):
    """Guards against the double-clicked bulk upload."""
    data = make_png(250, 250)
    keys = []
    for name in ("a.png", "b.png"):
        slot = presign(
            staff_client,
            feature_slug="wallpaper",
            category=category,
            files=[{"filename": name, "content_type": "image/png", "size": len(data)}],
        ).json()["data"]["slots"][0]
        upload(s3, settings, slot["object_key"], data, "image/png")
        keys.append(slot["ticket_id"])

    first = post(staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": keys[0]}]})
    assert first.status_code == 201

    second = post(staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": keys[1]}]})
    assert second.status_code == 400
    assert second.json()["data"]["results"][0]["code"] == "duplicate_file"
    assert MediaItem.objects.count() == 1


def test_ticket_cannot_be_replayed(s3, settings, staff_client, category):
    data = make_png(120, 120)
    slot = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "x.png", "content_type": "image/png", "size": len(data)}],
    ).json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], data, "image/png")

    assert (
        post(
            staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]}
        ).status_code
        == 201
    )

    replay = post(staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]})
    assert replay.status_code == 400
    assert replay.json()["data"]["results"][0]["code"] == "ticket_unusable"
    assert MediaItem.objects.count() == 1


def test_expired_ticket_is_refused(s3, settings, staff_client, category):
    settings.PRESIGN_EXPIRY_SECONDS = -1  # already expired at issue time
    data = make_png(120, 120)
    slot = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "x.png", "content_type": "image/png", "size": len(data)}],
    ).json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], data, "image/png")

    response = post(
        staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]}
    )
    assert response.status_code == 400
    assert response.json()["data"]["results"][0]["code"] == "ticket_unusable"


def test_another_staff_user_cannot_commit_someone_elses_ticket(
    s3, settings, client, staff_client, django_user_model, category
):
    """
    The committer chooses the title, category and premium flag, so a ticket must
    only be redeemable by whoever created it.
    """
    data = make_png(120, 120)
    slot = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "x.png", "content_type": "image/png", "size": len(data)}],
    ).json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], data, "image/png")

    intruder = django_user_model.objects.create_user(
        username="intruder", password="not-a-real-password", is_staff=True
    )
    client.force_login(intruder)
    response = post(client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]})
    assert response.status_code == 400
    assert response.json()["data"]["results"][0]["code"] == "ticket_unusable"


def test_commit_rejects_duplicate_ticket_within_one_batch(staff_client, category):
    slot = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "x.png", "content_type": "image/png", "size": 500}],
    ).json()["data"]["slots"][0]

    response = post(
        staff_client,
        COMMIT_URL,
        {
            "items": [
                {"asset_ticket_id": slot["ticket_id"]},
                {"asset_ticket_id": slot["ticket_id"]},
            ]
        },
    )
    assert response.status_code == 400
    assert "items" in response.json()["errors"]


def test_preview_ticket_cannot_be_used_as_an_asset(s3, settings, staff_client, category):
    poster = make_jpeg(300, 300)
    slot = presign(
        staff_client,
        feature_slug="wallpaper",
        category=category,
        files=[
            {
                "filename": "p.jpg",
                "content_type": "image/jpeg",
                "size": len(poster),
                "kind": "PREVIEW",
            }
        ],
    ).json()["data"]["slots"][0]
    upload(s3, settings, slot["object_key"], poster, "image/jpeg")

    response = post(
        staff_client, COMMIT_URL, {"items": [{"asset_ticket_id": slot["ticket_id"]}]}
    )
    assert response.status_code == 400
    assert response.json()["data"]["results"][0]["code"] == "ticket_wrong_kind"


# --------------------------------------------------------------------------- #
# Authorisation
# --------------------------------------------------------------------------- #


def test_ingest_requires_staff(client, api_key, category):
    """An API key is for reading. It grants nothing on the ingest surface."""
    response = client.post(
        PRESIGN_URL,
        data=json.dumps(
            {
                "type": "wallpaper",
                "category_id": str(category.id),
                "files": [{"content_type": "image/png", "size": 10}],
            }
        ),
        content_type="application/json",
        HTTP_X_API_KEY=api_key,
    )
    assert response.status_code in (401, 403)


def test_non_staff_user_is_refused(client, django_user_model, category):
    user = django_user_model.objects.create_user(
        username="regular", password="not-a-real-password", is_staff=False
    )
    client.force_login(user)
    response = presign(
        client,
        feature_slug="wallpaper",
        category=category,
        files=[{"filename": "x.png", "content_type": "image/png", "size": 100}],
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Archive
# --------------------------------------------------------------------------- #


def test_archive_hides_item_and_decrements_counts(
    s3, settings, staff_client, api_key, category
):
    from .factories import make_item

    item = make_item(category=category, name="doomed")
    Category.objects.filter(pk=category.pk).update(item_count=1)
    # The factory only writes a row, so put real bytes behind its keys.
    upload(s3, settings, item.file_key, make_png(80, 80), "image/png")
    upload(s3, settings, item.preview_key, make_jpeg(80, 80), "image/jpeg")

    listed = staff_client.get("/api/v1/wallpapers", HTTP_X_API_KEY=api_key)
    assert listed.json()["data"]["total"] == 1

    response = staff_client.delete(reverse("ingest:item-admin", args=[item.id]))
    assert response.status_code == 200
    assert response.json()["data"]["status"] == ItemStatus.ARCHIVED

    item.refresh_from_db()
    assert item.status == ItemStatus.ARCHIVED
    assert item.archived_at is not None
    category.refresh_from_db()
    assert category.item_count == 0

    # Bytes are deliberately still present: the reaper purges them later, which
    # leaves a window to undo an accidental archive.
    assert storage.head_object(item.file_key) is not None

    listed = staff_client.get("/api/v1/wallpapers", HTTP_X_API_KEY=api_key)
    assert listed.json()["data"]["total"] == 0


def test_patch_updates_editorial_fields_only(staff_client, category, subcategory):
    from .factories import make_item

    item = make_item(category=category, name="before")
    original_key = item.file_key

    response = staff_client.patch(
        reverse("ingest:item-admin", args=[item.id]),
        data=json.dumps(
            {
                "name": "after",
                "premium": True,
                "priority": 7,
                "tags": ["dark"],
                # Not in the serializer's `fields`, so these must be ignored.
                "file_key": "attacker/controlled/key.png",
                "status": "ARCHIVED",
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content

    item.refresh_from_db()
    assert item.name == "after"
    assert item.premium is True
    assert item.priority == 7
    assert [t.slug for t in item.tags.all()] == ["dark"]
    assert item.file_key == original_key  # unchanged
    assert item.status == ItemStatus.READY  # unchanged


def test_patch_rejects_subcategory_from_another_category(staff_client, category, subcategory):
    from .factories import make_item

    item = make_item(category=category, name="x")
    response = staff_client.patch(
        reverse("ingest:item-admin", args=[item.id]),
        data=json.dumps({"subcategory_id": str(subcategory.id)}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "subcategory_id" in response.json()["errors"]
