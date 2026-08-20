"""Model factories. Kept small and explicit rather than clever."""

from __future__ import annotations

import uuid

from apps.catalog.models import ItemStatus, MediaItem, MediaType


def make_item(
    *,
    category,
    subcategory=None,
    feature=None,
    name: str = "item",
    priority: int = 1,
    media_type: str = MediaType.IMAGE,
    is_live: bool = False,
    premium: bool = False,
    status: str = ItemStatus.READY,
    is_active: bool = True,
    width: int | None = 1080,
    height: int | None = 1920,
    file_bytes: int = 400_000,
    file_etag: str = "",
    tags: list | None = None,
    **extra,
) -> MediaItem:
    """
    Create a published MediaItem with plausible storage metadata.

    Keys are randomised because `file_key` is unique and a suite that reuses one
    fails for a reason that has nothing to do with what it is testing.
    """
    unique = uuid.uuid4().hex
    item = MediaItem.objects.create(
        feature=feature or category.feature,
        category=category,
        subcategory=subcategory,
        name=name,
        priority=priority,
        media_type=media_type,
        is_live=is_live,
        premium=premium,
        status=status,
        is_active=is_active,
        width=width,
        height=height,
        file_key=f"{category.feature.slug}/assets/{unique}.bin",
        file_bytes=file_bytes,
        file_mime="image/png",
        file_etag=file_etag or unique,
        preview_key=f"{category.feature.slug}/previews/{unique}.jpg",
        preview_bytes=40_000,
        preview_mime="image/jpeg",
        **extra,
    )
    if tags:
        item.tags.set(tags)
    return item
