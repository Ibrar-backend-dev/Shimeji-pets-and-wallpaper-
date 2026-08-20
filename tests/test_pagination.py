"""
Pagination.

Offset paging is the client contract, so the tests cover both that it behaves
and that its two known costs are actually bounded: `total` is not recounted per
page, and deep `skip` is refused rather than allowed to degrade into a scan.
"""

from __future__ import annotations

import base64
import json

import pytest

from .factories import make_item

pytestmark = pytest.mark.django_db


def seed(category, count: int) -> None:
    for n in range(count):
        make_item(category=category, name=f"item-{n:03d}", priority=(n % 3) + 1)


# --------------------------------------------------------------------------- #
# Basics
# --------------------------------------------------------------------------- #


def test_defaults(client, auth, settings, category):
    seed(category, 30)
    data = client.get("/api/v1/wallpapers", **auth).json()["data"]

    assert data["skip"] == 0
    assert data["limit"] == settings.API_PAGE_SIZE_DEFAULT
    assert data["total"] == 30
    assert len(data["items"]) == settings.API_PAGE_SIZE_DEFAULT


def test_total_reflects_the_filtered_set_not_the_table(client, auth, category):
    seed(category, 10)
    make_item(category=category, name="live-one", is_live=True)

    data = client.get("/api/v1/wallpapers?is_live=true", **auth).json()["data"]
    assert data["total"] == 1


def test_paging_covers_everything_exactly_once(client, auth, category):
    seed(category, 47)

    seen: list[str] = []
    skip = 0
    while True:
        data = client.get(f"/api/v1/wallpapers?skip={skip}&limit=10", **auth).json()["data"]
        seen.extend(i["id"] for i in data["items"])
        if len(data["items"]) < 10:
            break
        skip += 10

    assert len(seen) == 47
    assert len(set(seen)) == 47, "a row was returned on two different pages"


def test_skip_past_the_end_returns_an_empty_page(client, auth, category):
    seed(category, 5)
    data = client.get("/api/v1/wallpapers?skip=100&limit=10", **auth).json()["data"]

    assert data["items"] == []
    assert data["total"] == 5  # total still describes the whole filtered set


# --------------------------------------------------------------------------- #
# Rejections — invalid input is a 400, never a silent clamp
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "limit=999999",
        "limit=abc",
        "limit=-1",
        "limit=1.5",
        "skip=-1",
        "skip=abc",
        "skip=10001",
    ],
)
def test_invalid_paging_params_are_rejected(client, auth, category, query):
    make_item(category=category)
    response = client.get(f"/api/v1/wallpapers?{query}", **auth)

    assert response.status_code == 400, f"{query} should be a 400"
    assert response.json()["data"] is None


def test_limit_over_the_cap_names_the_cap(client, auth, settings, category):
    make_item(category=category)
    body = client.get("/api/v1/wallpapers?limit=500", **auth).json()

    assert str(settings.API_PAGE_SIZE_MAX) in body["message"]
    assert "limit" in body["errors"]


def test_limit_is_not_silently_clamped(client, auth, category):
    """
    A client asking for 500 must learn it cannot have 500.

    Returning 100 rows would let it assume it had received everything and stop
    paging, silently losing data.
    """
    seed(category, 30)
    assert client.get("/api/v1/wallpapers?limit=500", **auth).status_code == 400


def test_deep_skip_points_at_the_cursor_alternative(client, auth, settings, category):
    make_item(category=category)
    body = client.get(f"/api/v1/wallpapers?skip={settings.API_MAX_SKIP + 1}", **auth).json()

    assert "cursor" in body["message"].lower()
    assert "skip" in body["errors"]


def test_skip_at_exactly_the_cap_is_allowed(client, auth, settings, category):
    make_item(category=category)
    response = client.get(f"/api/v1/wallpapers?skip={settings.API_MAX_SKIP}", **auth)
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# Count caching
# --------------------------------------------------------------------------- #


def test_total_is_not_recounted_on_every_page(client, auth, settings, category):
    """
    Twenty pages over one filter must not mean twenty COUNT(*) queries.

    This is the difference between a cheap scroll and a free-tier database
    spending its budget counting the same rows over and over.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    settings.API_COUNT_CACHE_SECONDS = 60
    seed(category, 40)

    client.get("/api/v1/wallpapers?limit=10", **auth)  # warm caches

    def count_queries(url: str) -> int:
        with CaptureQueriesContext(connection) as ctx:
            client.get(url, **auth)
        return sum(1 for q in ctx.captured_queries if "COUNT(" in q["sql"].upper())

    first = count_queries("/api/v1/wallpapers?limit=10&skip=10")
    second = count_queries("/api/v1/wallpapers?limit=10&skip=20")

    assert first == 0, "the warm-up should already have cached this filter's count"
    assert second == 0


def test_different_filters_get_different_cached_counts(client, auth, settings, category):
    settings.API_COUNT_CACHE_SECONDS = 60
    seed(category, 10)
    make_item(category=category, name="live", is_live=True)

    assert client.get("/api/v1/wallpapers", **auth).json()["data"]["total"] == 11
    assert client.get("/api/v1/wallpapers?is_live=true", **auth).json()["data"]["total"] == 1
    # And back again, from cache this time.
    assert client.get("/api/v1/wallpapers", **auth).json()["data"]["total"] == 11


# --------------------------------------------------------------------------- #
# Cursor mode
# --------------------------------------------------------------------------- #


def encode_cursor(priority: int, created_at: str, item_id: str) -> str:
    raw = json.dumps({"priority": priority, "created_at": created_at, "id": item_id})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def test_cursor_mode_walks_the_whole_set(client, auth, category):
    seed(category, 25)

    cursor = encode_cursor(
        -1, "1970-01-01 00:00:00+00:00", "00000000-0000-0000-0000-000000000000"
    )
    seen: list[str] = []
    for _ in range(10):
        data = client.get(f"/api/v1/wallpapers?limit=10&cursor={cursor}", **auth).json()["data"]
        seen.extend(i["id"] for i in data["items"])
        if not data["next_cursor"]:
            break
        cursor = data["next_cursor"]

    assert len(seen) == 25
    assert len(set(seen)) == 25


def test_cursor_mode_reports_total_and_next_cursor(client, auth, category):
    seed(category, 25)
    cursor = encode_cursor(
        -1, "1970-01-01 00:00:00+00:00", "00000000-0000-0000-0000-000000000000"
    )

    data = client.get(f"/api/v1/wallpapers?limit=10&cursor={cursor}", **auth).json()["data"]

    assert set(data) == {"items", "total", "limit", "cursor", "next_cursor"}
    assert data["total"] == 25
    assert data["next_cursor"]


def test_malformed_cursor_is_rejected(client, auth, category):
    make_item(category=category)
    response = client.get("/api/v1/wallpapers?cursor=!!!not-base64!!!", **auth)

    assert response.status_code == 400
    assert "cursor" in response.json()["errors"]


def test_cursor_missing_ordering_keys_is_rejected(client, auth, category):
    make_item(category=category)
    bad = base64.urlsafe_b64encode(json.dumps({"priority": 1}).encode()).decode().rstrip("=")

    response = client.get(f"/api/v1/wallpapers?cursor={bad}", **auth)
    assert response.status_code == 400


def test_cursor_with_a_custom_ordering_is_rejected(client, auth, category):
    """
    Keyset paging is only valid over the ordering the cursor encodes.

    Silently returning the wrong page would be worse than an error, so the
    combination is refused outright.
    """
    make_item(category=category)
    cursor = encode_cursor(
        1, "1970-01-01 00:00:00+00:00", "00000000-0000-0000-0000-000000000000"
    )

    response = client.get(f"/api/v1/wallpapers?ordering=newest&cursor={cursor}", **auth)
    assert response.status_code == 400
    assert "cursor" in response.json()["errors"]


# --------------------------------------------------------------------------- #
# Stability under concurrent writes
# --------------------------------------------------------------------------- #


def test_priority_and_id_tiebreak_keeps_ordering_deterministic(client, auth, category):
    """
    Ten rows created in the same instant must still page deterministically.

    Without the `-id` tiebreaker, rows sharing a priority and timestamp could
    appear on two pages or none — and UUIDv7 ids sort by creation time, so the
    tiebreak also happens to be chronological.
    """
    for n in range(10):
        make_item(category=category, name=f"same-{n}", priority=1)

    first = client.get("/api/v1/wallpapers?limit=5&skip=0", **auth).json()["data"]["items"]
    second = client.get("/api/v1/wallpapers?limit=5&skip=5", **auth).json()["data"]["items"]

    ids = [i["id"] for i in first] + [i["id"] for i in second]
    assert len(set(ids)) == 10

    # And the same request twice gives the same answer.
    repeat = client.get("/api/v1/wallpapers?limit=5&skip=0", **auth).json()["data"]["items"]
    assert [i["id"] for i in repeat] == [i["id"] for i in first]
