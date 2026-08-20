"""
The public read contract.

These tests pin the response shape against the reference API that three Android
clients are already written for. If a field is renamed, retyped, or the envelope
changes, this file fails — which is the point.
"""

from __future__ import annotations

import re
import uuid

import pytest

from apps.catalog.models import Category, ItemStatus, MediaType

from .factories import make_item

pytestmark = pytest.mark.django_db

# Exactly the keys the reference response carries. Additions are fine; removals
# and renames break shipped clients.
REFERENCE_ITEM_KEYS = {
    "id",
    "name",
    "category_id",
    "category_name",
    "subcategory_id",
    "subcategory_name",
    "premium",
    "preview_url",
    "image_url",
    "priority",
    "created_at",
}
REFERENCE_CATEGORY_KEYS = {
    "id",
    "name",
    "type",
    "thumbnail",
    "priority",
    "has_subcategories",
}


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #


def test_envelope_shape_on_success(client, auth, category):
    make_item(category=category)
    response = client.get("/api/v1/wallpapers", **auth)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "data", "message"}
    assert body["status"] == 200
    assert body["message"] == "Wallpapers fetched successfully"


def test_envelope_shape_on_error(client, auth):
    response = client.get("/api/v1/wallpapers?limit=999", **auth)

    assert response.status_code == 400
    body = response.json()
    assert body["status"] == 400
    assert body["data"] is None
    assert body["message"]
    assert "limit" in body["errors"]
    # The request id is echoed so a client bug report maps onto a log line.
    assert body["request_id"]
    assert response.headers["X-Request-ID"] == body["request_id"]


def test_pagination_block_has_exactly_the_contract_keys(client, auth, category):
    make_item(category=category)
    data = client.get("/api/v1/wallpapers", **auth).json()["data"]
    assert set(data) == {"items", "total", "skip", "limit"}


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/api/v1/wallpapers", "Wallpapers fetched successfully"),
        ("/api/v1/shimeji", "Shimeji fetched successfully"),
        ("/api/v1/battery", "Battery items fetched successfully"),
        ("/api/v1/categories", "Categories fetched successfully"),
        ("/api/v1/subcategories", "Subcategories fetched successfully"),
    ],
)
def test_per_route_messages(client, auth, path, message):
    assert client.get(path, **auth).json()["message"] == message


# --------------------------------------------------------------------------- #
# Item payload
# --------------------------------------------------------------------------- #


def test_item_carries_every_reference_key(client, auth, category):
    make_item(category=category, name="wallpaper")
    item = client.get("/api/v1/wallpapers", **auth).json()["data"]["items"][0]

    missing = REFERENCE_ITEM_KEYS - set(item)
    assert not missing, f"contract regression: missing {sorted(missing)}"


def test_item_field_types_match_the_reference(client, auth, category):
    make_item(category=category, name="wallpaper", priority=1, premium=False)
    item = client.get("/api/v1/wallpapers", **auth).json()["data"]["items"][0]

    uuid.UUID(item["id"])  # a UUID string, not an integer
    uuid.UUID(item["category_id"])
    assert isinstance(item["name"], str)
    assert isinstance(item["category_name"], str)
    assert isinstance(item["premium"], bool)
    assert isinstance(item["priority"], int)
    assert isinstance(item["preview_url"], str)
    assert isinstance(item["image_url"], str)


def test_subcategory_fields_are_null_when_absent(client, auth, category):
    make_item(category=category, subcategory=None)
    item = client.get("/api/v1/wallpapers", **auth).json()["data"]["items"][0]

    assert item["subcategory_id"] is None
    assert item["subcategory_name"] is None


def test_subcategory_fields_populate_when_present(client, auth, other_category, subcategory):
    make_item(category=other_category, subcategory=subcategory)
    item = client.get("/api/v1/wallpapers", **auth).json()["data"]["items"][0]

    assert item["subcategory_id"] == str(subcategory.id)
    assert item["subcategory_name"] == "4K Anime"


def test_created_at_matches_the_reference_format(client, auth, category):
    """Naive UTC with microseconds, e.g. 2026-08-20T04:51:49.002715."""
    make_item(category=category)
    item = client.get("/api/v1/wallpapers", **auth).json()["data"]["items"][0]

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}", item["created_at"]), (
        item["created_at"]
    )


def test_urls_are_absolute_cdn_urls(client, auth, settings, category):
    make_item(category=category)
    item = client.get("/api/v1/wallpapers", **auth).json()["data"]["items"][0]

    assert item["image_url"].startswith(settings.MEDIA_CDN_BASE_URL + "/")
    assert item["preview_url"].startswith(settings.MEDIA_CDN_BASE_URL + "/")


def test_urls_follow_the_cdn_setting_without_a_migration(client, auth, settings, category):
    """URLs are composed at serialization time, never stored."""
    make_item(category=category)
    settings.MEDIA_CDN_BASE_URL = "https://newcdn.example.net"

    item = client.get("/api/v1/wallpapers", **auth).json()["data"]["items"][0]
    assert item["image_url"].startswith("https://newcdn.example.net/")


def test_image_url_carries_whatever_the_asset_is(client, auth, shimeji):
    """
    `image_url` is the asset URL regardless of type, with `media_type` telling
    the app how to render it. That keeps a reference-era client working.
    """
    cat = Category.objects.create(feature=shimeji, name="Packs", priority=1)
    item = make_item(
        category=cat, media_type=MediaType.ZIP, is_live=True, width=None, height=None
    )
    item.file_key = "shimeji/assets/pack.zip"
    item.save()

    payload = client.get("/api/v1/shimeji", **auth).json()["data"]["items"][0]
    assert payload["image_url"].endswith(".zip")
    assert payload["media_type"] == "ZIP"
    assert payload["is_live"] is True


def test_computed_dimension_fields_are_derived_on_save(client, auth, category):
    make_item(category=category, width=3840, height=2160)
    item = client.get("/api/v1/wallpapers", **auth).json()["data"]["items"][0]

    assert item["orientation"] == "LANDSCAPE"
    assert item["resolution"] == "UHD_4K"


def test_portrait_4k_is_also_4k(client, auth, category):
    """Bucketing uses the longer edge, so orientation does not change the label."""
    make_item(category=category, width=2160, height=3840)
    item = client.get("/api/v1/wallpapers", **auth).json()["data"]["items"][0]

    assert item["orientation"] == "PORTRAIT"
    assert item["resolution"] == "UHD_4K"


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #


def test_default_ordering_is_priority_then_newest(client, auth, category):
    """
    Reproduces the reference ordering: every priority-1 row first, newest first
    inside each band.
    """
    for priority in (3, 1, 2):
        for n in range(3):
            make_item(category=category, name=f"p{priority}-{n}", priority=priority)

    items = client.get("/api/v1/wallpapers?limit=100", **auth).json()["data"]["items"]

    priorities = [i["priority"] for i in items]
    assert priorities == sorted(priorities)

    for band in set(priorities):
        stamps = [i["created_at"] for i in items if i["priority"] == band]
        assert stamps == sorted(stamps, reverse=True)


def test_ordering_is_whitelisted(client, auth, category):
    make_item(category=category)

    assert client.get("/api/v1/wallpapers?ordering=newest", **auth).status_code == 200
    assert client.get("/api/v1/wallpapers?ordering=oldest", **auth).status_code == 200

    # Raw user input must never reach order_by().
    bad = client.get("/api/v1/wallpapers?ordering=file_key", **auth)
    assert bad.status_code == 400
    assert "ordering" in bad.json()["errors"]

    injection = client.get("/api/v1/wallpapers?ordering=id;DROP TABLE", **auth)
    assert injection.status_code == 400


# --------------------------------------------------------------------------- #
# Categories, subcategories, manifest
# --------------------------------------------------------------------------- #


def test_category_payload_matches_the_reference(client, auth, category):
    payload = client.get("/api/v1/categories?type=wallpaper", **auth).json()["data"]
    entry = payload["items"][0]

    missing = REFERENCE_CATEGORY_KEYS - set(entry)
    assert not missing, f"missing {sorted(missing)}"
    assert entry["type"] == "wallpaper"
    assert entry["name"] == "Trending"


def test_has_subcategories_reflects_reality(
    client, auth, category, other_category, subcategory
):
    response = client.get("/api/v1/categories?type=wallpaper", **auth)
    items = response.json()["data"]["items"]
    by_name = {c["name"]: c for c in items}

    assert by_name["Anime"]["has_subcategories"] is True
    assert by_name["Trending"]["has_subcategories"] is False


def test_has_subcategories_flips_back_when_the_last_one_goes(
    client, auth, other_category, subcategory
):
    subcategory.delete()
    response = client.get("/api/v1/categories?type=wallpaper", **auth)
    items = response.json()["data"]["items"]
    assert items[0]["has_subcategories"] is False


def test_empty_list_still_returns_the_full_envelope(client, auth):
    response = client.get("/api/v1/wallpapers", **auth)
    assert response.status_code == 200
    assert response.json()["data"] == {"items": [], "total": 0, "skip": 0, "limit": 20}


def test_manifest_nests_the_whole_taxonomy(client, auth, category, other_category, subcategory):
    make_item(category=other_category, subcategory=subcategory)

    data = client.get("/api/v1/manifest", **auth).json()["data"]

    assert data["config_version"]
    assert {t["type"] for t in data["types"]} == {"wallpaper", "shimeji", "battery"}

    wallpaper_entry = next(t for t in data["types"] if t["type"] == "wallpaper")
    names = {c["name"] for c in wallpaper_entry["categories"]}
    assert names == {"Trending", "Anime"}

    anime = next(c for c in wallpaper_entry["categories"] if c["name"] == "Anime")
    assert [s["name"] for s in anime["subcategories"]] == ["4K Anime"]


# --------------------------------------------------------------------------- #
# Visibility
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [ItemStatus.PENDING, ItemStatus.FAILED, ItemStatus.ARCHIVED])
def test_unpublished_items_are_never_listed(client, auth, category, status):
    make_item(category=category, status=status, name="hidden")
    make_item(category=category, status=ItemStatus.READY, name="visible")

    items = client.get("/api/v1/wallpapers", **auth).json()["data"]["items"]
    assert [i["name"] for i in items] == ["visible"]


def test_inactive_items_are_never_listed(client, auth, category):
    make_item(category=category, is_active=False, name="hidden")
    assert client.get("/api/v1/wallpapers", **auth).json()["data"]["total"] == 0


def test_unpublished_item_404s_on_detail(client, auth, category):
    item = make_item(category=category, status=ItemStatus.ARCHIVED)
    response = client.get(f"/api/v1/wallpapers/{item.id}", **auth)

    assert response.status_code == 404
    assert response.json()["data"] is None


def test_item_from_another_type_404s_on_a_typed_route(client, auth, category):
    item = make_item(category=category)
    assert client.get(f"/api/v1/shimeji/{item.id}", **auth).status_code == 404
    assert client.get(f"/api/v1/wallpapers/{item.id}", **auth).status_code == 200


def test_unknown_id_404s(client, auth):
    assert client.get(f"/api/v1/wallpapers/{uuid.uuid4()}", **auth).status_code == 404


# --------------------------------------------------------------------------- #
# Related
# --------------------------------------------------------------------------- #


def test_related_prefers_the_subcategory_and_excludes_self(
    client, auth, other_category, subcategory
):
    target = make_item(category=other_category, subcategory=subcategory, name="target")
    sibling = make_item(category=other_category, subcategory=subcategory, name="sibling")
    make_item(category=other_category, subcategory=None, name="cousin")

    items = client.get(f"/api/v1/items/{target.id}/related", **auth).json()["data"]["items"]
    names = {i["name"] for i in items}

    assert names == {"sibling"}
    assert str(target.id) not in {i["id"] for i in items}
    assert sibling.name in names


def test_related_falls_back_to_the_category(client, auth, category):
    target = make_item(category=category, name="target", subcategory=None)
    make_item(category=category, name="sibling", subcategory=None)

    items = client.get(f"/api/v1/items/{target.id}/related", **auth).json()["data"]["items"]
    assert {i["name"] for i in items} == {"sibling"}


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #


def test_list_response_sets_cache_headers(client, auth, settings, category):
    settings.API_LIST_CACHE_SECONDS = 300
    make_item(category=category)

    response = client.get("/api/v1/wallpapers", **auth)
    assert "max-age=300" in response["Cache-Control"]
    assert "stale-while-revalidate" in response["Cache-Control"]
    assert response["ETag"]
    assert response["Last-Modified"]


def test_if_none_match_returns_304(client, auth, settings, category):
    settings.API_LIST_CACHE_SECONDS = 300
    make_item(category=category)

    first = client.get("/api/v1/wallpapers", **auth)
    second = client.get("/api/v1/wallpapers", HTTP_IF_NONE_MATCH=first["ETag"], **auth)

    assert second.status_code == 304


def test_etag_changes_when_content_changes(client, auth, settings, category):
    settings.API_LIST_CACHE_SECONDS = 300
    make_item(category=category, name="first")
    original = client.get("/api/v1/wallpapers", **auth)["ETag"]

    make_item(category=category, name="second")
    updated = client.get("/api/v1/wallpapers", **auth)["ETag"]

    assert original != updated


# --------------------------------------------------------------------------- #
# Query efficiency
# --------------------------------------------------------------------------- #


def test_list_query_count_does_not_grow_with_page_size(
    client, auth, django_assert_num_queries, category, subcategory, other_category
):
    """
    The N+1 guard.

    A page of 20 must cost the same number of queries as a page of 3. Without
    select_related and prefetch_related this grows by four queries per row, which
    is the difference between a usable free tier and a dead one.
    """
    from apps.catalog.models import Tag

    tag = Tag.objects.create(name="Anime", slug="anime")
    for n in range(3):
        make_item(category=other_category, subcategory=subcategory, name=f"a{n}", tags=[tag])

    client.get("/api/v1/wallpapers", **auth)  # warm the feature-slug cache
    with django_assert_num_queries(4) as ctx:
        client.get("/api/v1/wallpapers?limit=3", **auth)
    baseline = len(ctx.captured_queries)

    for n in range(17):
        make_item(category=other_category, subcategory=subcategory, name=f"b{n}", tags=[tag])

    with django_assert_num_queries(baseline):
        client.get("/api/v1/wallpapers?limit=20", **auth)
