"""
Seed the three content types.

They are data rather than an enum so a fourth app is a row and the per-type
upload policy stays editable, but the original three still have to exist for the
API to work at all — hence a migration rather than a fixture somebody has to
remember to load.

Written to be re-runnable: existing rows are left alone so an operator's tuned
limits survive a re-deploy.
"""

from django.db import migrations

# Deliberately conservative ceilings, sized for a free-tier host and Cloudflare
# in front of B2. Every one is editable in the admin afterwards.
FEATURES = [
    {
        "slug": "wallpaper",
        "name": "Wallpapers",
        "description": "Static and live wallpapers.",
        "priority": 1,
        "allowed_mimes": [
            "image/jpeg",
            "image/png",
            "image/webp",
            "image/gif",
            "video/mp4",
        ],
        "max_file_bytes": 30 * 1024 * 1024,
        "max_pixels": 60_000_000,          # ~8K, well above 4K wallpapers
        "strict_zip_structure": False,
    },
    {
        "slug": "shimeji",
        "name": "Shimeji Animations",
        "description": "Shimeji mascot packs and animations.",
        "priority": 2,
        "allowed_mimes": [
            "application/zip",
            "application/x-zip-compressed",
            "image/gif",
            "image/png",
            "image/webp",
        ],
        "max_file_bytes": 40 * 1024 * 1024,
        "max_pixels": 30_000_000,
        # Left lenient: packers vary, and rejecting a valid pack over a missing
        # conf/ directory is worse than recording that it lacks one. Flip this in
        # the admin once the real packs are known.
        "strict_zip_structure": False,
    },
    {
        "slug": "battery",
        "name": "Battery Emoji & Animations",
        "description": "Battery charging emoji and animations.",
        "priority": 3,
        "allowed_mimes": [
            "image/gif",
            "image/png",
            "image/webp",
            "video/mp4",
        ],
        "max_file_bytes": 20 * 1024 * 1024,
        "max_pixels": 30_000_000,
        "strict_zip_structure": False,
    },
]


def seed_features(apps, schema_editor):
    Feature = apps.get_model("catalog", "Feature")
    for spec in FEATURES:
        # get_or_create, not update_or_create: an operator who has tuned
        # max_file_bytes in the admin should not have it reset by a deploy.
        Feature.objects.get_or_create(slug=spec["slug"], defaults=spec)


def unseed_features(apps, schema_editor):
    """
    Remove the seeded rows on reverse — but only if nothing references them.

    Feature is PROTECTed by Category and MediaItem, so a reverse migration on a
    populated database would raise. Skipping referenced rows makes the reverse
    safe to run without silently destroying content.
    """
    Feature = apps.get_model("catalog", "Feature")
    for spec in FEATURES:
        feature = Feature.objects.filter(slug=spec["slug"]).first()
        if feature is None:
            continue
        if feature.categories.exists() or feature.items.exists():
            continue
        feature.delete()


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]

    operations = [migrations.RunPython(seed_features, unseed_features)]
