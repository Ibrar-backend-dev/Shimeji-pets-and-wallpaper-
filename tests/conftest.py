"""
Shared fixtures.

Every test that touches storage runs inside moto's in-process S3 mock, so the
suite never reaches real B2 and needs no credentials. `storage.reset_client()`
matters: the boto3 client is cached process-wide for performance, and a client
built before the mock was active would talk to the real endpoint.
"""

from __future__ import annotations

import io
import zipfile

import boto3
import pytest
from django.core.cache import cache
from moto import mock_aws
from PIL import Image

from apps.catalog.models import Category, Feature, Subcategory
from apps.clients.models import AppClient, generate_key
from apps.ingest import storage


@pytest.fixture(autouse=True)
def _clear_caches():
    """
    Wipe the cache between tests.

    Non-negotiable here: API clients, feature slugs and filtered counts are all
    memoised, so a leftover entry from a previous test would make results depend
    on execution order.
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _reset_storage_client():
    storage.reset_client()
    yield
    storage.reset_client()


@pytest.fixture
def s3(settings):
    """An active S3 mock with the configured bucket already created."""
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name=settings.B2_REGION,
            aws_access_key_id=settings.B2_KEY_ID,
            aws_secret_access_key=settings.B2_APPLICATION_KEY,
        )
        client.create_bucket(Bucket=settings.B2_BUCKET_NAME)
        storage.reset_client()
        yield client


# --------------------------------------------------------------------------- #
# Taxonomy
# --------------------------------------------------------------------------- #


@pytest.fixture
def features(db):
    """The three seeded types, as a slug -> Feature mapping."""
    return {f.slug: f for f in Feature.objects.all()}


@pytest.fixture
def wallpaper(features):
    return features["wallpaper"]


@pytest.fixture
def shimeji(features):
    return features["shimeji"]


@pytest.fixture
def battery(features):
    return features["battery"]


@pytest.fixture
def category(wallpaper):
    return Category.objects.create(feature=wallpaper, name="Trending", priority=1)


@pytest.fixture
def other_category(wallpaper):
    return Category.objects.create(feature=wallpaper, name="Anime", priority=2)


@pytest.fixture
def subcategory(other_category):
    return Subcategory.objects.create(category=other_category, name="4K Anime", priority=1)


# --------------------------------------------------------------------------- #
# API clients
# --------------------------------------------------------------------------- #


@pytest.fixture
def api_key(db):
    """An unrestricted key. Rate limit 0 means unlimited, so tests don't throttle."""
    raw, _, _ = generate_key("test")
    client = AppClient(name="Test Client", slug="test-client", rate_limit_per_min=0)
    client.set_key(raw)
    client.save()
    return raw


@pytest.fixture
def wallpaper_only_key(db, wallpaper):
    """A key scoped to the wallpaper type, for testing feature isolation."""
    raw, _, _ = generate_key("wponly")
    client = AppClient(name="Wallpaper Only", slug="wallpaper-only", rate_limit_per_min=0)
    client.set_key(raw)
    client.save()
    client.allowed_features.set([wallpaper])
    return raw


@pytest.fixture
def auth(api_key):
    """Header kwargs for an authenticated public request."""
    return {"HTTP_X_API_KEY": api_key}


@pytest.fixture
def staff_user(django_user_model):
    return django_user_model.objects.create_user(
        username="uploader", password="not-a-real-password", is_staff=True
    )


@pytest.fixture
def staff_client(client, staff_user):
    client.force_login(staff_user)
    return client


# --------------------------------------------------------------------------- #
# Test media
# --------------------------------------------------------------------------- #


def make_png(width: int = 1080, height: int = 1920, color=(40, 90, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def make_jpeg(width: int = 1080, height: int = 1920) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 60, 60)).save(buffer, format="JPEG")
    return buffer.getvalue()


def make_gif(width: int = 64, height: int = 64, frames: int = 3) -> bytes:
    buffer = io.BytesIO()
    images = [Image.new("P", (width, height), i * 40) for i in range(frames)]
    images[0].save(buffer, format="GIF", save_all=True, append_images=images[1:], loop=0)
    return buffer.getvalue()


def make_zip(entries: dict[str, bytes] | None = None) -> bytes:
    """A plausible Shimeji pack: conf/ XML plus a couple of frames."""
    entries = entries or {
        "conf/actions.xml": b"<mascot></mascot>",
        "conf/behaviors.xml": b"<mascot></mascot>",
        "img/shime1.png": make_png(64, 64),
        "img/shime2.png": make_png(64, 64, (10, 10, 10)),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def make_mp4() -> bytes:
    """
    Minimal ISO-BMFF header.

    Only the 'ftyp' box at offset 4 matters: that is what the magic-byte sniffer
    keys on, and nothing in this project decodes video.
    """
    return (
        b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
        + b"\x00\x00\x00\x08free"
        + b"\x00" * 512
    )


@pytest.fixture
def png_bytes():
    return make_png()


@pytest.fixture
def zip_bytes():
    return make_zip()
