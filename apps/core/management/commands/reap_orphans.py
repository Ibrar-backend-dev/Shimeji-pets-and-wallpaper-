"""Same job as POST /internal/cron/reap-orphans, for a shell or a host scheduler."""

from django.core.management.base import BaseCommand

from apps.core.maintenance import DEFAULT_BATCH_LIMIT, reap_orphans


class Command(BaseCommand):
    help = "Delete abandoned upload tickets and purge archived items past retention."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--limit", type=int, default=DEFAULT_BATCH_LIMIT)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be removed without deleting anything.",
        )

    def handle(self, *args, **options) -> None:
        stats = reap_orphans(batch_limit=options["limit"], dry_run=options["dry_run"])
        for key, value in stats.items():
            self.stdout.write(f"{key}: {value}")
        if stats.get("more_pending"):
            self.stdout.write(
                self.style.WARNING("Batch limit reached — run again to continue.")
            )
        self.stdout.write(self.style.SUCCESS("reap_orphans complete"))
