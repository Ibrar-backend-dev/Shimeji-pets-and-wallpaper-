"""
Pre-flight check.

Runs Django's own `check --deploy` plus the assertions specific to this project,
then exits non-zero if anything is wrong. Wired into the release step so a
misconfigured deploy fails at deploy time with a readable list, rather than at
2am with a 500 and an empty log line.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Verify this deployment is correctly configured. Exits non-zero on failure."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--skip-django-checks",
            action="store_true",
            help="Only run the project-specific assertions.",
        )

    def handle(self, *args, **options) -> None:
        problems: list[str] = []
        warnings: list[str] = []

        skip_django = options["skip_django_checks"] or settings.DEBUG
        if settings.DEBUG and not options["skip_django_checks"]:
            self.stdout.write(
                "Skipping Django's --deploy checks: DEBUG is on, so they would "
                "report only the expected local-development warnings."
            )

        if not skip_django:
            self.stdout.write("Running Django deployment checks...")
            try:
                call_command("check", "--deploy", "--fail-level", "ERROR")
            except SystemExit:
                problems.append("Django's `check --deploy` reported errors.")
            except Exception as exc:
                problems.append(f"Django's `check --deploy` failed: {exc}")

        # --- secrets and hosts -------------------------------------------- #
        if not settings.DEBUG:
            if settings.SECRET_KEY in {"", "dev-only-insecure-key-change-me"}:
                problems.append("DJANGO_SECRET_KEY is unset or still the dev default.")
            if "*" in settings.ALLOWED_HOSTS:
                problems.append("ALLOWED_HOSTS contains '*'.")
            if not settings.ALLOWED_HOSTS:
                problems.append("ALLOWED_HOSTS is empty.")
            if len(getattr(settings, "CRON_SECRET", "")) < 16:
                problems.append("CRON_SECRET is missing or shorter than 16 characters.")
            if settings.ADMIN_URL == "admin/":
                warnings.append(
                    "ADMIN_URL is the default 'admin/'. Moving it reduces automated "
                    "login attempts."
                )

        # --- database ------------------------------------------------------ #
        engine = settings.DATABASES["default"]["ENGINE"]
        if not settings.DEBUG and "sqlite" in engine:
            problems.append("DATABASE_URL is unset: production is falling back to SQLite.")
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            self.stdout.write(self.style.SUCCESS("  database: reachable"))
        except Exception as exc:
            problems.append(f"Database is unreachable: {exc}")

        # Neon's pooled endpoint is pgbouncer in transaction mode. Persistent
        # connections and server-side cursors both break against it, producing
        # intermittent InterfaceError under load rather than a clean failure.
        if "postgresql" in engine:
            db = settings.DATABASES["default"]
            host = str(db.get("HOST", ""))
            pooled = "-pooler" in host or "pgbouncer" in host
            if pooled:
                if db.get("CONN_MAX_AGE"):
                    problems.append(
                        "CONN_MAX_AGE must be 0 with a pooled (pgbouncer) database host."
                    )
                if not db.get("DISABLE_SERVER_SIDE_CURSORS"):
                    problems.append(
                        "DISABLE_SERVER_SIDE_CURSORS must be True with a pooled host."
                    )
            elif "neon.tech" in host:
                warnings.append(
                    "Neon host is not the '-pooler' endpoint. The pooled DSN handles "
                    "many more concurrent workers on a free tier."
                )

        # --- storage ------------------------------------------------------- #
        from apps.ingest import storage

        if not storage.is_configured():
            message = (
                "B2 is not configured (B2_KEY_ID, B2_APPLICATION_KEY, "
                "B2_BUCKET_NAME, B2_ENDPOINT_URL). Uploads will fail."
            )
            (warnings if settings.DEBUG else problems).append(message)
        else:
            health = storage.storage_health()
            if health == "ok":
                self.stdout.write(self.style.SUCCESS("  storage: reachable"))
            else:
                problems.append(f"B2 bucket is not reachable (status: {health}).")

        if not settings.MEDIA_CDN_BASE_URL:
            (warnings if settings.DEBUG else problems).append(
                "MEDIA_CDN_BASE_URL is unset, so every image_url would be empty."
            )
        elif "example.com" in settings.MEDIA_CDN_BASE_URL and not settings.DEBUG:
            problems.append("MEDIA_CDN_BASE_URL is still the placeholder host.")

        # --- seeded data --------------------------------------------------- #
        from apps.catalog.models import Feature

        try:
            slugs = set(Feature.objects.values_list("slug", flat=True))
            missing = {Feature.SHIMEJI, Feature.WALLPAPER, Feature.BATTERY} - slugs
            if missing:
                problems.append(
                    f"Seed migration has not run: missing types {', '.join(sorted(missing))}."
                )
            else:
                self.stdout.write(self.style.SUCCESS("  types: all three seeded"))
        except Exception as exc:
            problems.append(f"Could not read the Feature table (migrations pending?): {exc}")

        # --- report -------------------------------------------------------- #
        self.stdout.write("")
        for warning in warnings:
            self.stdout.write(self.style.WARNING(f"WARNING  {warning}"))
        for problem in problems:
            self.stdout.write(self.style.ERROR(f"ERROR    {problem}"))

        if problems:
            self.stdout.write("")
            raise SystemExit(
                f"check_deploy failed: {len(problems)} error(s), {len(warnings)} warning(s)."
            )

        self.stdout.write(
            self.style.SUCCESS(f"check_deploy passed with {len(warnings)} warning(s).")
        )
