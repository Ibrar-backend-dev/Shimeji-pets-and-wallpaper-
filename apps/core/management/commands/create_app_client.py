"""
Mint an API key for one of the consuming apps.

The raw key is printed once and never stored — only its prefix and SHA-256 hash
go to the database, so a leaked dump does not hand over working credentials.
There is no way to recover a lost key; rotate instead.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify

from apps.catalog.models import Feature
from apps.clients.authentication import invalidate_client_cache
from apps.clients.models import AppClient, generate_key


class Command(BaseCommand):
    help = "Create an API client and print its key (shown once)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("name", help='Display name, e.g. "Wallpaper Android"')
        parser.add_argument(
            "--features",
            default="",
            help="Comma-separated type slugs this key may read. Empty means all.",
        )
        parser.add_argument("--rate-limit", type=int, default=120, help="Requests per minute.")
        parser.add_argument(
            "--rotate",
            action="store_true",
            help="Replace the key on an existing client with this slug.",
        )

    def handle(self, *args, **options) -> None:
        name = options["name"]
        slug = slugify(name)[:64]
        if not slug:
            raise CommandError("Name must contain at least one alphanumeric character.")

        requested = [s.strip() for s in options["features"].split(",") if s.strip()]
        features = list(Feature.objects.filter(slug__in=requested)) if requested else []
        if requested and len(features) != len(set(requested)):
            found = {f.slug for f in features}
            raise CommandError(f"Unknown type slug(s): {', '.join(set(requested) - found)}")

        existing = AppClient.objects.filter(slug=slug).first()
        if existing and not options["rotate"]:
            raise CommandError(
                f"A client with slug '{slug}' already exists. Pass --rotate to replace its key."
            )

        raw_key, _, _ = generate_key(slug)
        client = existing or AppClient(name=name, slug=slug)
        old_prefix = client.key_prefix if existing else ""
        client.name = name
        client.rate_limit_per_min = options["rate_limit"]
        client.is_active = True
        client.set_key(raw_key)
        client.save()
        client.allowed_features.set(features)

        # Drop the cached snapshot so the old key stops working immediately.
        if old_prefix:
            invalidate_client_cache(old_prefix)
        invalidate_client_cache(client.key_prefix)

        scope = ", ".join(f.slug for f in features) if features else "all types"
        action = "Rotated" if existing else "Created"
        self.stdout.write(self.style.SUCCESS(f"{action} client '{client.name}'"))
        self.stdout.write(f"  slug       : {client.slug}")
        self.stdout.write(f"  scope      : {scope}")
        self.stdout.write(f"  rate limit : {client.rate_limit_per_min}/min")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("  API key (shown once, store it now):"))
        self.stdout.write(f"  {raw_key}")
        self.stdout.write("")
        self.stdout.write(f'  Usage: curl -H "X-API-Key: {raw_key}" .../api/v1/wallpapers')
