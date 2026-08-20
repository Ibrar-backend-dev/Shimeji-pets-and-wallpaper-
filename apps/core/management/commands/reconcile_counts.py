"""Same job as POST /internal/cron/reconcile-counts."""

from django.core.management.base import BaseCommand

from apps.core.maintenance import reconcile_counts


class Command(BaseCommand):
    help = "Recompute denormalized item counts and has_subcategories flags."

    def add_arguments(self, parser) -> None:  # noqa: ANN001
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report drift without writing corrections.",
        )

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        stats = reconcile_counts(dry_run=options["dry_run"])
        for key, value in stats.items():
            self.stdout.write(f"{key}: {value}")
        self.stdout.write(self.style.SUCCESS("reconcile_counts complete"))
