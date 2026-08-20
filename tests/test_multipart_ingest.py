"""Focused contract tests for the resumable v2 ingest path."""

import pytest

from apps.catalog.models import ItemStatus, MediaItem
from apps.ingest import services
from apps.ingest.models import BatchPublishMode, MultipartSessionStatus

pytestmark = pytest.mark.django_db


def _batch(staff_user, category):
    return services.create_upload_batch(
        user=staff_user,
        feature_slug="wallpaper",
        category_id=category.id,
        subcategory_id=None,
        publish_mode=BatchPublishMode.DRAFT,
    )


def test_multipart_draft_is_resumable_then_publishable(
    s3, settings, staff_user, category, png_bytes
):
    batch = _batch(staff_user, category)
    session = services.create_multipart_session(
        batch=batch,
        payload={
            "kind": "ASSET",
            "filename": "wall.png",
            "content_type": "image/png",
            "size": len(png_bytes),
            "name": "Draft wall",
            "tags": ["draft"],
        },
    )
    urls = services.multipart_part_urls(session=session, part_numbers=[1])
    assert urls["parts"][0]["part_number"] == 1
    s3.upload_part(
        Bucket=settings.B2_BUCKET_NAME,
        Key=session.object_key,
        UploadId=session.storage_upload_id,
        PartNumber=1,
        Body=png_bytes,
    )
    assert services.multipart_parts(session)[0]["part_number"] == 1
    services.complete_multipart_session(session=session)
    session.refresh_from_db()
    assert session.status == MultipartSessionStatus.UPLOADED
    item = services.finalize_multipart_session(session=session)
    assert item.status == ItemStatus.PENDING
    assert not MediaItem.objects.published().filter(pk=item.pk).exists()
    outcome = services.publish_batch(batch=batch, item_ids=[item.id])
    assert outcome["published"] == [str(item.id)]
    item.refresh_from_db()
    assert item.status == ItemStatus.READY


def test_multipart_part_urls_omit_already_uploaded_part(
    s3, settings, staff_user, category, png_bytes
):
    batch = _batch(staff_user, category)
    session = services.create_multipart_session(
        batch=batch,
        payload={
            "kind": "ASSET",
            "filename": "wall.png",
            "content_type": "image/png",
            "size": len(png_bytes),
        },
    )
    s3.upload_part(
        Bucket=settings.B2_BUCKET_NAME,
        Key=session.object_key,
        UploadId=session.storage_upload_id,
        PartNumber=1,
        Body=png_bytes,
    )
    result = services.multipart_part_urls(session=session, part_numbers=[1])
    assert result["already_uploaded"] == [1]
    assert result["parts"] == []


def test_v2_batch_endpoint_is_staff_owned(staff_client, category):
    response = staff_client.post(
        "/api/v2/admin/upload-batches",
        data={
            "type": "wallpaper",
            "category_id": str(category.id),
            "publish_mode": "DRAFT",
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    assert response.json()["data"]["publish_mode"] == "DRAFT"
