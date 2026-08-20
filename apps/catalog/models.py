"""
The content catalog.

Three levels, matching the API contract:

    Feature   -> serialized as "type"  (wallpaper | shimeji | battery)
      Category                          admin-created; "Trending", "Anime", ...
        Subcategory                     admin-created, optional
          MediaItem                     the asset row

"Trending" and "Latest" are ordinary categories, not computed feeds. Nothing in
this module ranks or scores anything: ordering is entirely admin-controlled via
`priority`, and there are no like/download counters anywhere by design.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils.text import slugify

from apps.core.models import TimeStampedModel

# --------------------------------------------------------------------------- #
# Choices
# --------------------------------------------------------------------------- #


class MediaType(models.TextChoices):
    """The technical kind of the stored asset. Drives validation and rendering."""

    IMAGE = "IMAGE", "Static image"
    GIF = "GIF", "Animated GIF"
    VIDEO = "VIDEO", "Video"
    ZIP = "ZIP", "Zip archive (Shimeji pack)"


class Orientation(models.TextChoices):
    PORTRAIT = "PORTRAIT", "Portrait"
    LANDSCAPE = "LANDSCAPE", "Landscape"
    SQUARE = "SQUARE", "Square"


class Resolution(models.TextChoices):
    SD = "SD", "SD"
    HD = "HD", "HD (720p)"
    FHD = "FHD", "Full HD (1080p)"
    QHD = "QHD", "Quad HD (1440p)"
    UHD_4K = "UHD_4K", "4K UHD"


class ItemStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    READY = "READY", "Ready"
    FAILED = "FAILED", "Failed"
    ARCHIVED = "ARCHIVED", "Archived"


_SLUG_VALIDATOR = RegexValidator(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    "Use lowercase letters, digits and single hyphens only.",
)


def derive_orientation(width: int | None, height: int | None) -> str:
    if not width or not height:
        return ""
    if width > height:
        return Orientation.LANDSCAPE
    if width < height:
        return Orientation.PORTRAIT
    return Orientation.SQUARE


def derive_resolution(width: int | None, height: int | None) -> str:
    """
    Bucket by the longer edge, so a 2160x3840 portrait wallpaper is 4K just like
    its 3840x2160 landscape counterpart.
    """
    if not width or not height:
        return ""
    longest = max(width, height)
    if longest >= 3840:
        return Resolution.UHD_4K
    if longest >= 2560:
        return Resolution.QHD
    if longest >= 1920:
        return Resolution.FHD
    if longest >= 1280:
        return Resolution.HD
    return Resolution.SD


# --------------------------------------------------------------------------- #
# Feature — the top level, exposed as "type"
# --------------------------------------------------------------------------- #


class Feature(TimeStampedModel):
    """
    One of the three apps' content types.

    A table rather than an enum for two reasons: a fourth app later is a row
    instead of a deployment, and the per-type upload policy below stays editable
    data. "Upload should be bulk or single in one feature and selected category
    (check type)" is enforced from these columns.
    """

    SHIMEJI = "shimeji"
    WALLPAPER = "wallpaper"
    BATTERY = "battery"

    slug = models.SlugField(max_length=32, unique=True, validators=[_SLUG_VALIDATOR])
    name = models.CharField(max_length=64)
    description = models.TextField(blank=True)

    # --- upload policy ---
    allowed_mimes = models.JSONField(
        default=list,
        help_text="Whitelist of accepted Content-Type values for this type.",
    )
    max_file_bytes = models.BigIntegerField(
        default=25 * 1024 * 1024, validators=[MinValueValidator(1)]
    )
    max_pixels = models.BigIntegerField(
        default=50_000_000,
        validators=[MinValueValidator(1)],
        help_text="Decompression-bomb ceiling: width * height must not exceed this.",
    )
    strict_zip_structure = models.BooleanField(
        default=False,
        help_text="Reject zip uploads that lack a conf/ entry (Shimeji packs).",
    )

    priority = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("priority", "name")
        indexes = [models.Index(fields=["is_active", "priority"], name="feature_active_prio_idx")]

    def __str__(self) -> str:
        return self.name

    def allows_mime(self, mime: str) -> bool:
        return mime.lower() in {m.lower() for m in (self.allowed_mimes or [])}


# --------------------------------------------------------------------------- #
# Category / Subcategory
# --------------------------------------------------------------------------- #


class Category(TimeStampedModel):
    """Admin-created grouping inside a type. PROTECT so a delete cannot cascade."""

    feature = models.ForeignKey(
        Feature, on_delete=models.PROTECT, related_name="categories"
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, validators=[_SLUG_VALIDATOR])
    description = models.TextField(blank=True)
    thumbnail_key = models.CharField(max_length=512, blank=True)
    priority = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    # Denormalized. The apps use this to decide whether to render a subcategory
    # row at all, so it must not become an EXISTS subquery per category.
    has_subcategories = models.BooleanField(default=False, editable=False)
    item_count = models.PositiveIntegerField(default=0, editable=False)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ("priority", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["feature", "slug"], name="category_unique_feature_slug"
            ),
        ]
        indexes = [
            models.Index(
                fields=["feature", "is_active", "priority"], name="category_feat_act_prio_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.feature.slug}/{self.slug}"

    def save(self, *args, **kwargs):
        if not self.slug and self.name:
            self.slug = slugify(self.name)[:120]
        return super().save(*args, **kwargs)


class Subcategory(TimeStampedModel):
    """Optional third level. An item may sit directly in a category instead."""

    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="subcategories"
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, validators=[_SLUG_VALIDATOR])
    description = models.TextField(blank=True)
    thumbnail_key = models.CharField(max_length=512, blank=True)
    priority = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    item_count = models.PositiveIntegerField(default=0, editable=False)

    class Meta:
        verbose_name_plural = "subcategories"
        ordering = ("priority", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["category", "slug"], name="subcategory_unique_cat_slug"
            ),
            # Target for the composite foreign key added in migration 0003, which
            # is what makes "subcategory belongs to this item's category" a
            # database guarantee rather than an application convention.
            models.UniqueConstraint(
                fields=["id", "category"], name="subcategory_id_category_uniq"
            ),
        ]
        indexes = [
            models.Index(
                fields=["category", "is_active", "priority"], name="subcat_cat_act_prio_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.category.slug}/{self.slug}"

    def save(self, *args, **kwargs):
        if not self.slug and self.name:
            self.slug = slugify(self.name)[:120]
        return super().save(*args, **kwargs)


class Tag(TimeStampedModel):
    """
    Editorial label that cuts across categories.

    An item lives in exactly one category, so labels like "anime" or "dark" that
    should be filterable independently of that placement belong here.
    """

    name = models.CharField(max_length=64)
    slug = models.SlugField(max_length=64, unique=True, validators=[_SLUG_VALIDATOR])
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug and self.name:
            self.slug = slugify(self.name)[:64]
        return super().save(*args, **kwargs)


# --------------------------------------------------------------------------- #
# MediaItem
# --------------------------------------------------------------------------- #


class MediaItemQuerySet(models.QuerySet):
    def published(self) -> MediaItemQuerySet:
        """Rows the public API may return. Matches every partial index below."""
        return self.filter(status=ItemStatus.READY, is_active=True)

    def for_api(self) -> MediaItemQuerySet:
        """
        Published rows with the joins the serializer needs already resolved.

        Without this, serializing 20 rows costs 60+ extra queries. The test suite
        pins the query count so a regression fails loudly.
        """
        return (
            self.published()
            .select_related("feature", "category", "subcategory")
            .prefetch_related("tags")
        )


class MediaItem(TimeStampedModel):
    """
    One asset. The API returns its URLs; the app renders them.

    No like or download counters exist here on purpose — the backend's job is to
    hand over URLs, and engagement is the app's concern.
    """

    # `feature` duplicates category.feature so the per-type feed filters without
    # a join. It is kept in sync by clean()/save() rather than trusted from input.
    feature = models.ForeignKey(Feature, on_delete=models.PROTECT, related_name="items")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="items")
    subcategory = models.ForeignKey(
        Subcategory,
        on_delete=models.PROTECT,
        related_name="items",
        null=True,
        blank=True,
    )

    name = models.CharField(max_length=200, blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="items")

    premium = models.BooleanField(default=False)
    priority = models.IntegerField(default=0, help_text="Lower sorts first.")

    media_type = models.CharField(max_length=8, choices=MediaType.choices, db_index=True)
    is_live = models.BooleanField(
        default=False, help_text="Live wallpaper / animated battery asset."
    )

    # --- stored asset ---
    file_key = models.CharField(max_length=512, unique=True)
    file_bytes = models.BigIntegerField(default=0)
    file_mime = models.CharField(max_length=100, blank=True)
    file_etag = models.CharField(max_length=128, blank=True, db_index=True)

    # --- preview (always an image) ---
    preview_key = models.CharField(max_length=512, blank=True)
    preview_bytes = models.BigIntegerField(default=0)
    preview_mime = models.CharField(max_length=100, blank=True)

    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    # Uploader-declared: there is no ffmpeg on the target hosts to probe it.
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    orientation = models.CharField(
        max_length=10, choices=Orientation.choices, blank=True, db_index=True
    )
    resolution = models.CharField(
        max_length=8, choices=Resolution.choices, blank=True, db_index=True
    )
    dominant_color = models.CharField(
        max_length=7, blank=True, help_text="Hex like #1a2b3c, for image placeholders."
    )

    # --- Shimeji zip introspection ---
    zip_entries = models.PositiveIntegerField(null=True, blank=True)
    zip_has_conf = models.BooleanField(null=True, blank=True)

    status = models.CharField(
        max_length=10, choices=ItemStatus.choices, default=ItemStatus.PENDING
    )
    is_active = models.BooleanField(default=True)
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = MediaItemQuerySet.as_manager()

    class Meta:
        # Lower priority first, then newest first. `id` is a UUIDv7 so it is a
        # valid creation-time tiebreaker and keeps paging deterministic.
        ordering = ("priority", "-created_at", "-id")
        indexes = [
            # Partial indexes: every public read filters to published rows, so
            # indexing only those keeps them a fraction of the full-table size.
            models.Index(
                fields=["priority", "-created_at", "-id"],
                condition=Q(status="READY", is_active=True),
                name="item_pub_feed_idx",
            ),
            models.Index(
                fields=["feature", "priority", "-created_at", "-id"],
                condition=Q(status="READY", is_active=True),
                name="item_pub_feature_idx",
            ),
            models.Index(
                fields=["category", "priority", "-created_at", "-id"],
                condition=Q(status="READY", is_active=True),
                name="item_pub_category_idx",
            ),
            models.Index(
                fields=["subcategory", "priority", "-created_at", "-id"],
                condition=Q(status="READY", is_active=True),
                name="item_pub_subcat_idx",
            ),
            models.Index(
                fields=["feature", "media_type", "priority", "-created_at", "-id"],
                condition=Q(status="READY", is_active=True),
                name="item_pub_feat_mtype_idx",
            ),
            models.Index(
                fields=["feature", "is_live", "priority", "-created_at", "-id"],
                condition=Q(status="READY", is_active=True),
                name="item_pub_feat_live_idx",
            ),
            # Incremental client sync (?updated_since=).
            models.Index(fields=["-updated_at"], name="item_updated_idx"),
            # Dedup lookup on commit.
            models.Index(fields=["file_etag", "file_bytes"], name="item_etag_bytes_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(file_bytes__gte=0) & Q(preview_bytes__gte=0),
                name="item_sizes_non_negative",
            ),
        ]

    def __str__(self) -> str:
        return self.name or str(self.id)

    # -- validation --------------------------------------------------------- #

    def clean(self) -> None:
        """
        Keep the denormalized `feature` honest and reject a cross-category
        subcategory. Migration 0003 also enforces the latter in the database, so
        neither a buggy service nor a raw SQL insert can slip past it.
        """
        super().clean()

        if self.subcategory_id and self.category_id:
            if self.subcategory.category_id != self.category_id:
                raise ValidationError(
                    {
                        "subcategory": (
                            "Subcategory belongs to a different category. An item's "
                            "subcategory must sit inside its own category."
                        )
                    }
                )

        if self.category_id:
            expected = self.category.feature_id
            if self.feature_id and self.feature_id != expected:
                raise ValidationError(
                    {"feature": "Type must match the category's type."}
                )

    def save(self, *args, **kwargs):
        # Derive rather than trust: `feature` mirrors the category, and the
        # computed columns follow the dimensions.
        if self.category_id and not self.feature_id:
            self.feature_id = self.category.feature_id
        self.orientation = derive_orientation(self.width, self.height)
        self.resolution = derive_resolution(self.width, self.height)
        return super().save(*args, **kwargs)
