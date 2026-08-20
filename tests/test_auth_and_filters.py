"""
API-key authentication, feature scoping, throttling, and query filters.
"""

from __future__ import annotations

import urllib.parse
import uuid

import pytest

from apps.catalog.models import Category, MediaType, Tag
from apps.clients.models import AppClient, generate_key

from .factories import make_item

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


def test_missing_key_is_refused(client, settings, category):
    settings.DEBUG = False
    make_item(category=category)

    response = client.get("/api/v1/wallpapers")
    assert response.status_code == 403
    assert response.json()["data"] is None


def test_unknown_key_is_refused(client, settings, category):
    settings.DEBUG = False
    response = client.get("/api/v1/wallpapers", HTTP_X_API_KEY="nope_totally-made-up-key")
    assert response.status_code == 403


def test_key_shorter_than_the_prefix_is_refused(client, settings):
    settings.DEBUG = False
    assert client.get("/api/v1/wallpapers", HTTP_X_API_KEY="abc").status_code == 403


def test_inactive_client_is_refused(client, settings, db):
    settings.DEBUG = False
    raw, _, _ = generate_key("dead")
    inactive = AppClient(name="Dead", slug="dead", is_active=False)
    inactive.set_key(raw)
    inactive.save()

    assert client.get("/api/v1/wallpapers", HTTP_X_API_KEY=raw).status_code == 403


def test_tampered_secret_with_a_valid_prefix_is_refused(client, settings, api_key):
    """
    The prefix is public-ish — it identifies which client is calling. Only the
    hashed remainder authenticates, and it is compared in constant time.
    """
    settings.DEBUG = False
    tampered = api_key[:12] + "X" * (len(api_key) - 12)

    assert client.get("/api/v1/wallpapers", HTTP_X_API_KEY=tampered).status_code == 403


def test_valid_key_is_accepted(client, settings, auth, category):
    settings.DEBUG = False
    make_item(category=category)
    assert client.get("/api/v1/wallpapers", **auth).status_code == 200


def test_key_is_never_stored_in_plaintext(db):
    raw, prefix, _ = generate_key("secret")
    client_row = AppClient(name="X", slug="x")
    client_row.set_key(raw)
    client_row.save()

    client_row.refresh_from_db()
    assert client_row.key_hash != raw
    assert raw not in client_row.key_hash
    assert len(client_row.key_hash) == 64  # sha256 hex
    assert client_row.key_prefix == prefix


def test_authentication_costs_no_query_after_the_first(
    client, auth, django_assert_max_num_queries, category
):
    """The resolved client is cached, so auth is not a per-request DB hit."""
    make_item(category=category)
    client.get("/api/v1/wallpapers", **auth)  # populate the caches

    with django_assert_max_num_queries(4):
        client.get("/api/v1/wallpapers", **auth)


# --------------------------------------------------------------------------- #
# Feature scoping
# --------------------------------------------------------------------------- #


def test_scoped_key_reads_its_own_type(client, settings, wallpaper_only_key, category):
    settings.DEBUG = False
    make_item(category=category)

    response = client.get("/api/v1/wallpapers", HTTP_X_API_KEY=wallpaper_only_key)
    assert response.status_code == 200
    assert response.json()["data"]["total"] == 1


def test_scoped_key_sees_nothing_of_another_type(client, settings, wallpaper_only_key, shimeji):
    """
    An empty page, not a 403.

    A 403 would confirm that the shimeji type exists and has content, which a
    key scoped away from it has no business learning.
    """
    settings.DEBUG = False
    shimeji_category = Category.objects.create(feature=shimeji, name="Packs", priority=1)
    make_item(category=shimeji_category, media_type=MediaType.ZIP)

    response = client.get("/api/v1/shimeji", HTTP_X_API_KEY=wallpaper_only_key)
    assert response.status_code == 200
    assert response.json()["data"]["total"] == 0


def test_scoped_key_mixed_feed_contains_only_its_type(
    client, settings, wallpaper_only_key, category, shimeji
):
    settings.DEBUG = False
    make_item(category=category)
    shimeji_category = Category.objects.create(feature=shimeji, name="Packs", priority=1)
    make_item(category=shimeji_category, media_type=MediaType.ZIP)

    data = client.get("/api/v1/items", HTTP_X_API_KEY=wallpaper_only_key).json()["data"]
    assert {i["type"] for i in data["items"]} == {"wallpaper"}
    assert data["total"] == 1


def test_scoped_key_cannot_widen_scope_via_the_type_param(
    client, settings, wallpaper_only_key, shimeji
):
    """Scope is intersected server-side; no query parameter can extend it."""
    settings.DEBUG = False
    shimeji_category = Category.objects.create(feature=shimeji, name="Packs", priority=1)
    make_item(category=shimeji_category, media_type=MediaType.ZIP)

    data = client.get("/api/v1/items?type=shimeji", HTTP_X_API_KEY=wallpaper_only_key).json()[
        "data"
    ]
    assert data["total"] == 0


def test_scoped_key_manifest_and_categories_are_scoped(
    client, settings, wallpaper_only_key, category, shimeji
):
    settings.DEBUG = False
    Category.objects.create(feature=shimeji, name="Packs", priority=1)

    manifest = client.get("/api/v1/manifest", HTTP_X_API_KEY=wallpaper_only_key).json()
    assert [t["type"] for t in manifest["data"]["types"]] == ["wallpaper"]

    categories = client.get(
        "/api/v1/categories?type=shimeji", HTTP_X_API_KEY=wallpaper_only_key
    ).json()
    assert categories["data"]["total"] == 0


def test_scoped_key_cannot_fetch_a_foreign_item_by_id(
    client, settings, wallpaper_only_key, shimeji
):
    settings.DEBUG = False
    shimeji_category = Category.objects.create(feature=shimeji, name="Packs", priority=1)
    item = make_item(category=shimeji_category, media_type=MediaType.ZIP)

    response = client.get(f"/api/v1/items/{item.id}", HTTP_X_API_KEY=wallpaper_only_key)
    assert response.status_code == 404


def test_unrestricted_key_sees_every_type(client, settings, auth, category, shimeji, battery):
    settings.DEBUG = False
    make_item(category=category)
    make_item(
        category=Category.objects.create(feature=shimeji, name="P", priority=1),
        media_type=MediaType.ZIP,
    )
    make_item(
        category=Category.objects.create(feature=battery, name="L", priority=1),
        media_type=MediaType.GIF,
    )

    data = client.get("/api/v1/items", **auth).json()["data"]
    assert data["total"] == 3
    assert {i["type"] for i in data["items"]} == {"wallpaper", "shimeji", "battery"}


# --------------------------------------------------------------------------- #
# Throttling
# --------------------------------------------------------------------------- #


def test_per_client_rate_limit_is_enforced(client, settings, db, category):
    settings.DEBUG = False
    make_item(category=category)

    raw, _, _ = generate_key("slow")
    limited = AppClient(name="Slow", slug="slow", rate_limit_per_min=3)
    limited.set_key(raw)
    limited.save()

    statuses = [
        client.get("/api/v1/wallpapers", HTTP_X_API_KEY=raw).status_code for _ in range(5)
    ]
    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses[3:]


def test_throttled_response_says_when_to_retry(client, settings, db, category):
    settings.DEBUG = False
    make_item(category=category)

    raw, _, _ = generate_key("burst")
    limited = AppClient(name="Burst", slug="burst", rate_limit_per_min=1)
    limited.set_key(raw)
    limited.save()

    client.get("/api/v1/wallpapers", HTTP_X_API_KEY=raw)
    blocked = client.get("/api/v1/wallpapers", HTTP_X_API_KEY=raw)

    assert blocked.status_code == 429
    body = blocked.json()
    assert body["status"] == 429
    assert "retry_after_seconds" in body["errors"]


def test_zero_rate_limit_means_unlimited(client, settings, auth, category):
    settings.DEBUG = False
    make_item(category=category)
    statuses = [client.get("/api/v1/wallpapers", **auth).status_code for _ in range(12)]
    assert set(statuses) == {200}


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


def test_filter_by_category(client, auth, category, other_category):
    make_item(category=category, name="in-trending")
    make_item(category=other_category, name="in-anime")

    data = client.get(f"/api/v1/wallpapers?category_id={category.id}", **auth).json()["data"]
    assert [i["name"] for i in data["items"]] == ["in-trending"]


def test_filter_by_subcategory(client, auth, other_category, subcategory):
    make_item(category=other_category, subcategory=subcategory, name="in-sub")
    make_item(category=other_category, subcategory=None, name="bare")

    data = client.get(f"/api/v1/wallpapers?subcategory_id={subcategory.id}", **auth).json()[
        "data"
    ]
    assert [i["name"] for i in data["items"]] == ["in-sub"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("media_type=VIDEO", {"vid"}),
        ("media_type=GIF", {"gif"}),
        ("media_type=VIDEO,GIF", {"vid", "gif"}),
        ("is_live=true", {"vid", "gif"}),
        ("is_live=false", {"still", "wide"}),
        ("premium=true", {"gif"}),
        ("orientation=LANDSCAPE", {"wide"}),
        ("resolution=UHD_4K", {"wide"}),
        ("q=still", {"still"}),
    ],
)
def test_filters(client, auth, category, query, expected):
    make_item(category=category, name="still", media_type=MediaType.IMAGE, is_live=False)
    make_item(category=category, name="vid", media_type=MediaType.VIDEO, is_live=True)
    make_item(
        category=category, name="gif", media_type=MediaType.GIF, is_live=True, premium=True
    )
    make_item(
        category=category,
        name="wide",
        media_type=MediaType.IMAGE,
        width=3840,
        height=2160,
    )

    data = client.get(f"/api/v1/wallpapers?{query}", **auth).json()["data"]
    assert {i["name"] for i in data["items"]} == expected


def test_filter_by_tags_is_or_and_deduplicates(client, auth, category):
    anime = Tag.objects.create(name="Anime", slug="anime")
    dark = Tag.objects.create(name="Dark", slug="dark")

    make_item(category=category, name="both", tags=[anime, dark])
    make_item(category=category, name="anime-only", tags=[anime])
    make_item(category=category, name="untagged")

    data = client.get("/api/v1/wallpapers?tags=anime,dark", **auth).json()["data"]
    assert {i["name"] for i in data["items"]} == {"both", "anime-only"}
    # The join could duplicate "both"; distinct() must prevent it.
    assert data["total"] == 2


def test_updated_since_supports_incremental_sync(client, auth, category):
    """
    Incremental sync for the app's local cache.

    Timestamps are set explicitly rather than relying on wall-clock separation
    between two inserts: the system clock is only millisecond-granular in
    practice, so two rows created back to back routinely share a created_at and
    a time-based boundary between them would be a coin flip.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.catalog.models import MediaItem

    old_item = make_item(category=category, name="old")
    new_item = make_item(category=category, name="new")

    now = timezone.now()
    # update() bypasses auto_now, which save() would otherwise overwrite.
    MediaItem.objects.filter(pk=old_item.pk).update(updated_at=now - timedelta(hours=2))
    MediaItem.objects.filter(pk=new_item.pk).update(updated_at=now)
    boundary = now - timedelta(hours=1)

    # Properly encoded: '+' escaped as %2B.
    encoded = urllib.parse.quote(boundary.isoformat(), safe="")
    data = client.get(f"/api/v1/wallpapers?updated_since={encoded}", **auth).json()["data"]
    assert {i["name"] for i in data["items"]} == {"new"}

    # The 'Z' spelling works too.
    zulu = boundary.isoformat().replace("+00:00", "Z")
    data = client.get(f"/api/v1/wallpapers?updated_since={zulu}", **auth).json()["data"]
    assert {i["name"] for i in data["items"]} == {"new"}

    # And the common client mistake — a raw '+' that the URL layer decodes to a
    # space — is repaired rather than rejected.
    data = client.get(
        f"/api/v1/wallpapers?updated_since={boundary.isoformat()}", **auth
    ).json()["data"]
    assert {i["name"] for i in data["items"]} == {"new"}


def test_updated_since_rejects_unparseable_values(client, auth, category):
    make_item(category=category)
    response = client.get("/api/v1/wallpapers?updated_since=last-tuesday", **auth)

    assert response.status_code == 400
    assert "updated_since" in response.json()["errors"]


def test_filters_combine(client, auth, category):
    make_item(
        category=category, name="target", media_type=MediaType.VIDEO, is_live=True, premium=True
    )
    make_item(
        category=category,
        name="wrong-premium",
        media_type=MediaType.VIDEO,
        is_live=True,
        premium=False,
    )
    make_item(
        category=category,
        name="wrong-type",
        media_type=MediaType.IMAGE,
        is_live=True,
        premium=True,
    )

    data = client.get(
        f"/api/v1/wallpapers?category_id={category.id}&media_type=VIDEO"
        "&is_live=true&premium=true",
        **auth,
    ).json()["data"]
    assert [i["name"] for i in data["items"]] == ["target"]


# --------------------------------------------------------------------------- #
# Filter rejections
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("query", "field"),
    [
        ("media_type=BOGUS", "media_type"),
        ("orientation=SIDEWAYS", "orientation"),
        ("resolution=8K", "resolution"),
        ("is_live=maybe", "is_live"),
        ("premium=perhaps", "premium"),
        ("category_id=not-a-uuid", "category_id"),
        ("subcategory_id=nope", "subcategory_id"),
        ("ordering=file_key", "ordering"),
        ("type=nonexistent", "type"),
    ],
)
def test_invalid_filter_values_are_rejected(client, auth, category, query, field):
    make_item(category=category)
    response = client.get(f"/api/v1/items?{query}", **auth)

    assert response.status_code == 400, f"{query} should be a 400"
    assert field in response.json()["errors"], response.json()


def test_unknown_category_is_rejected_not_silently_empty(client, auth, category):
    """
    An unknown id is a client bug. Returning an empty page would leave the app
    author staring at a blank screen with no signal.
    """
    response = client.get(f"/api/v1/wallpapers?category_id={uuid.uuid4()}", **auth)
    assert response.status_code == 400
    assert "category_id" in response.json()["errors"]


def test_cross_type_category_is_rejected_and_says_which_type(client, auth, shimeji):
    shimeji_category = Category.objects.create(feature=shimeji, name="Packs", priority=1)
    response = client.get(f"/api/v1/wallpapers?category_id={shimeji_category.id}", **auth)

    assert response.status_code == 400
    assert "shimeji" in str(response.json()["errors"])


def test_contradictory_category_and_subcategory_are_rejected(
    client, auth, category, subcategory
):
    response = client.get(
        f"/api/v1/wallpapers?category_id={category.id}&subcategory_id={subcategory.id}",
        **auth,
    )
    assert response.status_code == 400
    assert "subcategory_id" in response.json()["errors"]


def test_overlong_search_is_rejected(client, auth, category):
    make_item(category=category)
    response = client.get("/api/v1/wallpapers?q=" + "x" * 500, **auth)

    assert response.status_code == 400
    assert "q" in response.json()["errors"]


def test_too_many_csv_values_are_rejected(client, auth, category):
    make_item(category=category)
    response = client.get("/api/v1/wallpapers?media_type=" + ",".join(["IMAGE"] * 30), **auth)
    assert response.status_code == 400


def test_unknown_params_are_ignored(client, auth, category):
    """
    Cache-busters and analytics tags must not break a client.

    Known params are strict; unknown ones are simply not our business.
    """
    make_item(category=category)
    response = client.get("/api/v1/wallpapers?_=1699999999&utm_source=x", **auth)
    assert response.status_code == 200
    assert response.json()["data"]["total"] == 1
