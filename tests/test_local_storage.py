"""
The filesystem storage backend.

These run against a tmp_path rather than the real media root, and they go
through `storage.*` rather than `local_storage.*` on purpose: the thing worth
asserting is that the dispatch in storage.py actually routes, not just that the
local functions work in isolation.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from apps.ingest import local_storage, storage


@pytest.fixture
def local(settings, tmp_path):
    """Switch the process to local storage, rooted somewhere disposable."""
    settings.MEDIA_LOCAL_STORAGE = True
    settings.LOCAL_MEDIA_ROOT = str(tmp_path)
    settings.LOCAL_MEDIA_ORIGIN = "http://testhost:8000"
    return tmp_path


def key_for(
    feature="wallpaper", category="nature", kind="assets", uid="abc123", mime="image/png"
):
    return storage.build_object_key(
        feature_slug=feature, category_slug=category, kind=kind, unique_id=uid, mime=mime
    )


def token_from(url: str) -> str:
    """
    Pull the token out of a presigned URL, undoing the percent-encoding.

    The unquote matters: a signed token contains ':', which `presign_put`
    encodes as %3A. Reading the raw query value gives a string that fails
    signature checks for the wrong reason — so a test asserting "tampering is
    rejected" would pass without ever testing tampering. Django's request
    parsing does this decode for real callers.
    """
    query = parse_qs(urlparse(url).query)
    return query["token"][0]


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def test_storage_routes_to_the_local_backend_when_enabled(local):
    """Without this, every other test here would be asserting the wrong module."""
    meta = storage.put_bytes(key=key_for(), data=b"x", content_type="image/png")
    assert meta.size == 1
    assert storage.storage_health() == "ok"


def test_storage_still_uses_b2_when_disabled(settings):
    settings.MEDIA_LOCAL_STORAGE = False
    # test.py supplies fake B2 credentials, so is_configured is the B2 answer.
    assert storage.is_configured() is True


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("slug", "folder"),
    [
        ("shimeji", "shimeiji animation"),
        ("wallpaper", "wallpaper"),
        ("battery", "battery emoji"),
    ],
)
def test_each_feature_lands_in_its_own_named_folder(local, slug, folder):
    key = key_for(feature=slug)
    storage.put_bytes(key=key, data=b"data", content_type="image/png")

    stored = list(local.rglob("*.png"))
    assert len(stored) == 1
    assert stored[0].relative_to(local).parts[0] == folder


def test_assets_and_previews_are_siblings_under_one_feature(local):
    """The 'alternate media' for an item sits beside it, not in a separate tree."""
    storage.put_bytes(key=key_for(kind="assets", uid="a"), data=b"1", content_type="image/png")
    storage.put_bytes(
        key=key_for(kind="previews", uid="b", mime="image/jpeg"),
        data=b"2",
        content_type="image/jpeg",
    )

    kinds = {p.name for p in (local / "wallpaper" / "nature").iterdir()}
    assert kinds == {"assets", "previews"}


def test_object_keys_stay_slug_based(local):
    """
    The friendly folder name is a filesystem detail only.

    Keys are persisted on MediaItem rows and handed to clients, so renaming them
    would be a data migration — the mapping must not leak into the key.
    """
    key = key_for(feature="battery")
    assert key.startswith("battery/")
    storage.put_bytes(key=key, data=b"x", content_type="image/png")
    assert storage.head_object(key).key == key
    assert "battery emoji" not in storage.public_url(key)


def test_an_unmapped_feature_slug_uses_itself(local, settings):
    settings.LOCAL_MEDIA_FEATURE_DIRS = {}
    storage.put_bytes(key=key_for(feature="wallpaper"), data=b"x", content_type="image/png")
    assert (local / "wallpaper").is_dir()


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #


def test_round_trip(local):
    key = key_for()
    payload = b"\x89PNG\r\n\x1a\n" + b"body" * 100

    storage.put_bytes(key=key, data=payload, content_type="image/png")

    meta = storage.head_object(key)
    assert meta.size == len(payload)
    assert meta.content_type == "image/png"
    assert meta.etag
    assert storage.get_range(key, 0, 8) == b"\x89PNG\r\n\x1a\n"
    assert storage.get_object_bytes(key, 10_000) == payload


def test_head_object_raises_for_a_missing_key(local):
    with pytest.raises(storage.ObjectNotFound):
        storage.head_object(key_for(uid="nope"))


def test_get_object_bytes_refuses_oversized_objects(local):
    key = key_for()
    storage.put_bytes(key=key, data=b"x" * 5000, content_type="image/png")
    with pytest.raises(storage.StorageError):
        storage.get_object_bytes(key, 100)


def test_delete_is_idempotent_and_never_raises(local):
    key = key_for()
    storage.put_bytes(key=key, data=b"x", content_type="image/png")
    assert storage.delete_object(key) is True
    assert storage.delete_object(key) is False
    assert storage.delete_object("") is False


def test_public_url_uses_the_client_origin_not_the_cdn(local, settings):
    settings.MEDIA_CDN_BASE_URL = "https://cdn.should-not-be-used.example.com"
    url = storage.public_url("wallpaper/nature/assets/2026/08/x.png")
    assert url == "http://testhost:8000/media/wallpaper/nature/assets/2026/08/x.png"


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "key",
    [
        "../outside.png",
        "wallpaper/../../outside.png",
        "/etc/passwd",
        "wallpaper\\..\\outside.png",
        "",
    ],
)
def test_unsafe_keys_are_refused(local, key):
    """
    Keys are server-minted, so this is defence in depth — but the upload views
    accept a key from a signed token, and signed means unmodified, not harmless.
    """
    with pytest.raises(storage.StorageError):
        storage.put_bytes(key=key, data=b"x", content_type="image/png")


# --------------------------------------------------------------------------- #
# Signed upload tokens
# --------------------------------------------------------------------------- #


def test_presign_binds_type_and_length_into_the_token(local):
    key = key_for()
    url, headers = storage.presign_put(key=key, content_type="image/png", content_length=1234)

    assert url.startswith("http://testhost:8000/media/upload/?token=")
    assert headers == {"Content-Type": "image/png", "Content-Length": "1234"}

    token = token_from(url)
    payload = local_storage.verify_upload_token(
        token, salt=local_storage.UPLOAD_SALT, max_age=900
    )
    assert payload == {"key": key, "content_type": "image/png", "content_length": 1234}


def test_a_tampered_token_is_rejected(local):
    url, _ = storage.presign_put(key=key_for(), content_type="image/png", content_length=1)
    token = token_from(url)

    with pytest.raises(storage.StorageError):
        local_storage.verify_upload_token(
            token[:-4] + "AAAA", salt=local_storage.UPLOAD_SALT, max_age=900
        )


def test_an_upload_token_cannot_be_replayed_against_the_part_endpoint(local):
    """The two endpoints use different salts precisely to stop this."""
    url, _ = storage.presign_put(key=key_for(), content_type="image/png", content_length=1)
    token = token_from(url)

    with pytest.raises(storage.StorageError):
        local_storage.verify_upload_token(token, salt=local_storage.PART_SALT, max_age=900)


def test_an_expired_token_is_rejected(local):
    url, _ = storage.presign_put(key=key_for(), content_type="image/png", content_length=1)
    token = token_from(url)

    with pytest.raises(storage.StorageError):
        local_storage.verify_upload_token(token, salt=local_storage.UPLOAD_SALT, max_age=-1)


# --------------------------------------------------------------------------- #
# Multipart
# --------------------------------------------------------------------------- #


def test_multipart_assembles_parts_in_declared_order(local):
    key = key_for(mime="application/zip")
    upload_id = storage.create_multipart_upload(key=key, content_type="application/zip")

    chunks = {1: b"AAA", 2: b"BBB", 3: b"CCC"}
    for number, data in chunks.items():
        local_storage.write_part(upload_id=upload_id, part_number=number, data=data)

    # Deliberately out of order: completion must sort by PartNumber, not trust
    # the sequence the client happened to send.
    parts = [{"PartNumber": n} for n in (3, 1, 2)]
    meta = storage.complete_multipart_upload(key=key, upload_id=upload_id, parts=parts)

    assert meta.size == 9
    assert storage.get_object_bytes(key, 1000) == b"AAABBBCCC"


def test_list_multipart_parts_reports_what_was_uploaded(local):
    key = key_for(mime="application/zip")
    upload_id = storage.create_multipart_upload(key=key, content_type="application/zip")
    local_storage.write_part(upload_id=upload_id, part_number=1, data=b"x" * 10)
    local_storage.write_part(upload_id=upload_id, part_number=2, data=b"y" * 20)

    parts = storage.list_multipart_parts(key=key, upload_id=upload_id)
    assert [p["PartNumber"] for p in parts] == [1, 2]
    assert [p["Size"] for p in parts] == [10, 20]
    assert all(p["ETag"] for p in parts)


def test_completing_with_a_missing_part_fails_without_leaving_a_partial_object(local):
    key = key_for(mime="application/zip")
    upload_id = storage.create_multipart_upload(key=key, content_type="application/zip")
    local_storage.write_part(upload_id=upload_id, part_number=1, data=b"AAA")

    with pytest.raises(storage.StorageError):
        storage.complete_multipart_upload(
            key=key, upload_id=upload_id, parts=[{"PartNumber": 1}, {"PartNumber": 2}]
        )

    # A truncated object would pass validation for the wrong reasons later.
    with pytest.raises(storage.ObjectNotFound):
        storage.head_object(key)


def test_abort_removes_the_session(local):
    key = key_for(mime="application/zip")
    upload_id = storage.create_multipart_upload(key=key, content_type="application/zip")
    local_storage.write_part(upload_id=upload_id, part_number=1, data=b"AAA")

    assert storage.abort_multipart_upload(key=key, upload_id=upload_id) is True
    assert storage.list_multipart_parts(key=key, upload_id=upload_id) == []
    assert storage.abort_multipart_upload(key=key, upload_id=upload_id) is False


def test_parts_never_appear_in_the_browsable_tree(local):
    """In-flight parts live under a dotted directory, not beside real media."""
    key = key_for(mime="application/zip")
    upload_id = storage.create_multipart_upload(key=key, content_type="application/zip")
    local_storage.write_part(upload_id=upload_id, part_number=1, data=b"AAA")

    top_level = {p.name for p in local.iterdir()}
    assert top_level == {".multipart"}
