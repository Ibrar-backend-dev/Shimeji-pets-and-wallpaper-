"""
Management commands.

These are operator entry points — the things someone runs on a fresh deploy at
an awkward hour — so they are covered like any other interface.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.catalog.models import Category, ItemStatus
from apps.clients.authentication import resolve_from_request
from apps.clients.models import AppClient, hash_key
from apps.ingest.models import TicketKind, TicketStatus, UploadTicket

from .factories import make_item

pytestmark = pytest.mark.django_db


def run(command: str, *args, **kwargs) -> str:
    out = StringIO()
    call_command(command, *args, stdout=out, stderr=out, **kwargs)
    return out.getvalue()


# --------------------------------------------------------------------------- #
# create_app_client
# --------------------------------------------------------------------------- #


def test_create_app_client_prints_the_key_once(wallpaper):
    output = run("create_app_client", "Wallpaper Android", "--features", "wallpaper")

    client = AppClient.objects.get(slug="wallpaper-android")
    assert client.is_active
    assert [f.slug for f in client.allowed_features.all()] == ["wallpaper"]

    # The raw key appears in the output and nowhere in the database.
    assert client.key_prefix in output
    assert "shown once" in output
    assert client.key_hash not in output


def test_created_key_actually_authenticates(wallpaper, rf):
    output = run("create_app_client", "Test App", "--features", "wallpaper")
    client = AppClient.objects.get(slug="test-app")

    # Locate the key by the prefix actually stored, rather than assuming how
    # generate_key derives it from the name.
    raw_key = next(
        line.strip()
        for line in output.splitlines()
        if line.strip().startswith(client.key_prefix)
    )
    assert client.key_hash == hash_key(raw_key)

    request = rf.get("/api/v1/wallpapers", HTTP_X_API_KEY=raw_key)
    resolved = resolve_from_request(request)
    assert resolved is not None
    assert resolved.slug == "test-app"
    assert resolved.may_read_feature("wallpaper")
    assert not resolved.may_read_feature("shimeji")


def test_create_app_client_without_features_allows_all(db):
    run("create_app_client", "Everything")

    client = AppClient.objects.get(slug="everything")
    assert client.allowed_features.count() == 0  # empty means unrestricted


def test_create_app_client_rejects_an_unknown_feature(db):
    with pytest.raises(CommandError, match="Unknown type slug"):
        run("create_app_client", "Bad", "--features", "not-a-type")
    assert not AppClient.objects.exists()


def test_create_app_client_refuses_to_clobber_an_existing_slug(db):
    run("create_app_client", "Dup")
    with pytest.raises(CommandError, match="already exists"):
        run("create_app_client", "Dup")


def test_rotate_replaces_the_key_and_invalidates_the_cache(db, rf):
    first_output = run("create_app_client", "Rotating")
    old = AppClient.objects.get(slug="rotating")
    old_hash, old_prefix = old.key_hash, old.key_prefix

    old_key = next(
        line.strip()
        for line in first_output.splitlines()
        if line.strip().startswith(old_prefix)
    )
    # Populate the cache so the invalidation is actually being tested.
    assert resolve_from_request(rf.get("/", HTTP_X_API_KEY=old_key)) is not None

    run("create_app_client", "Rotating", "--rotate")

    old.refresh_from_db()
    assert old.key_hash != old_hash
    assert AppClient.objects.count() == 1, "rotate must not create a second client"

    # The old key must stop working immediately, not when its cache entry expires.
    assert resolve_from_request(rf.get("/", HTTP_X_API_KEY=old_key)) is None
    assert old.key_prefix != old_prefix or old.key_hash != old_hash


def test_create_app_client_honours_the_rate_limit_flag(db):
    run("create_app_client", "Slow App", "--rate-limit", "30")
    assert AppClient.objects.get(slug="slow-app").rate_limit_per_min == 30


def test_create_app_client_rejects_a_nameless_slug(db):
    with pytest.raises(CommandError, match="alphanumeric"):
        run("create_app_client", "!!!")


# --------------------------------------------------------------------------- #
# reap_orphans
# --------------------------------------------------------------------------- #


def make_ticket(staff_user, category, *, expires_in_hours=1):
    return UploadTicket.objects.create(
        object_key=f"wallpaper/trending/assets/{uuid.uuid4().hex}.png",
        kind=TicketKind.ASSET,
        feature=category.feature,
        category=category,
        declared_name="x.png",
        declared_mime="image/png",
        declared_bytes=100,
        created_by=staff_user,
        status=TicketStatus.ISSUED,
        expires_at=timezone.now() + timedelta(hours=expires_in_hours),
    )


def test_reap_orphans_command_reports_its_work(s3, staff_user, category):
    make_ticket(staff_user, category, expires_in_hours=-1)

    output = run("reap_orphans")

    assert "expired_tickets: 1" in output
    assert "reap_orphans complete" in output
    assert not UploadTicket.objects.exists()


def test_reap_orphans_command_dry_run_changes_nothing(s3, staff_user, category):
    ticket = make_ticket(staff_user, category, expires_in_hours=-1)

    output = run("reap_orphans", "--dry-run")

    assert "dry_run: True" in output
    assert UploadTicket.objects.filter(pk=ticket.pk).exists()


def test_reap_orphans_command_warns_when_batch_limited(s3, staff_user, category):
    """
    A capped run must say so.

    Silent truncation reads as "nothing left to do", which is how orphaned bytes
    quietly accumulate a bill.
    """
    for _ in range(3):
        make_ticket(staff_user, category, expires_in_hours=-1)

    output = run("reap_orphans", "--limit", "1")

    assert "Batch limit reached" in output
    assert UploadTicket.objects.count() == 2


# --------------------------------------------------------------------------- #
# reconcile_counts
# --------------------------------------------------------------------------- #


def test_reconcile_counts_command_repairs_drift(category):
    make_item(category=category)
    Category.objects.filter(pk=category.pk).update(item_count=999)

    output = run("reconcile_counts")

    category.refresh_from_db()
    assert category.item_count == 1
    assert "categories_fixed: 1" in output


def test_reconcile_counts_command_dry_run_changes_nothing(category):
    make_item(category=category)
    Category.objects.filter(pk=category.pk).update(item_count=999)

    output = run("reconcile_counts", "--dry-run")

    category.refresh_from_db()
    assert category.item_count == 999
    assert "dry_run: True" in output


# --------------------------------------------------------------------------- #
# check_deploy
# --------------------------------------------------------------------------- #


def test_check_deploy_passes_on_a_healthy_configuration(s3, settings):
    settings.DEBUG = True  # skips the prod-only secret assertions
    output = run("check_deploy", "--skip-django-checks")

    assert "check_deploy passed" in output
    assert "database: reachable" in output
    assert "storage: reachable" in output
    assert "types: all three seeded" in output


def test_check_deploy_fails_on_an_insecure_secret_key(s3, settings):
    settings.DEBUG = False
    settings.SECRET_KEY = "dev-only-insecure-key-change-me"

    with pytest.raises(SystemExit, match="check_deploy failed"):
        run("check_deploy", "--skip-django-checks")


def test_check_deploy_fails_on_a_wildcard_allowed_host(s3, settings):
    settings.DEBUG = False
    settings.SECRET_KEY = "a-real-looking-secret-key-value"
    settings.ALLOWED_HOSTS = ["*"]
    settings.CRON_SECRET = "a-long-enough-cron-secret"

    with pytest.raises(SystemExit):
        run("check_deploy", "--skip-django-checks")


def test_check_deploy_fails_on_a_short_cron_secret(s3, settings):
    settings.DEBUG = False
    settings.SECRET_KEY = "a-real-looking-secret-key-value"
    settings.ALLOWED_HOSTS = ["api.example.com"]
    settings.CRON_SECRET = "short"

    with pytest.raises(SystemExit):
        run("check_deploy", "--skip-django-checks")


def test_check_deploy_fails_when_storage_is_unconfigured_in_prod(settings):
    settings.DEBUG = False
    settings.SECRET_KEY = "a-real-looking-secret-key-value"
    settings.ALLOWED_HOSTS = ["api.example.com"]
    settings.CRON_SECRET = "a-long-enough-cron-secret"
    settings.B2_KEY_ID = ""

    with pytest.raises(SystemExit):
        run("check_deploy", "--skip-django-checks")


def test_check_deploy_warns_but_passes_when_storage_is_unconfigured_in_debug(settings):
    """Local development has no B2 credentials, and that must not be fatal."""
    settings.DEBUG = True
    settings.B2_KEY_ID = ""

    output = run("check_deploy", "--skip-django-checks")

    assert "check_deploy passed" in output
    assert "WARNING" in output
    assert "B2 is not configured" in output


def test_check_deploy_flags_a_pooled_host_with_persistent_connections(s3, settings):
    """
    The Neon gotcha, caught before it becomes an intermittent production error.
    """
    settings.DEBUG = True
    settings.DATABASES = {
        **settings.DATABASES,
        "default": {
            **settings.DATABASES["default"],
            "ENGINE": "django.db.backends.postgresql",
            "HOST": "ep-cool-name-123456-pooler.us-east-2.aws.neon.tech",
            "CONN_MAX_AGE": 600,
            "DISABLE_SERVER_SIDE_CURSORS": False,
        },
    }

    with pytest.raises(SystemExit, match="check_deploy failed"):
        run("check_deploy", "--skip-django-checks")


def test_check_deploy_flags_the_placeholder_cdn_host(s3, settings):
    settings.DEBUG = False
    settings.SECRET_KEY = "a-real-looking-secret-key-value"
    settings.ALLOWED_HOSTS = ["api.example.com"]
    settings.CRON_SECRET = "a-long-enough-cron-secret"
    settings.MEDIA_CDN_BASE_URL = "https://cdn.example.com"

    with pytest.raises(SystemExit):
        run("check_deploy", "--skip-django-checks")


def test_check_deploy_reports_a_missing_seed(s3, settings):
    from apps.catalog.models import Feature

    settings.DEBUG = True
    # Nothing references battery yet, so it can be removed for the test.
    Feature.objects.filter(slug="battery").delete()

    with pytest.raises(SystemExit):
        run("check_deploy", "--skip-django-checks")


# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #


def test_no_model_changes_are_missing_a_migration():
    """
    A model edited without its migration passes every other test and then breaks
    on deploy, so it is caught here as well as in CI.
    """
    out = StringIO()
    call_command("makemigrations", "--check", "--dry-run", stdout=out)
    assert "No changes detected" in out.getvalue()


def test_seed_migration_is_rerunnable_and_preserves_tuned_limits(db):
    """
    Re-running the seed must not duplicate rows or undo operator changes.

    It uses get_or_create rather than update_or_create precisely so an operator
    who raised max_file_bytes in the admin keeps that change across a deploy.
    """
    from importlib import import_module

    from django.apps import apps as django_apps

    from apps.catalog.models import Feature

    # The migration module name starts with a digit, so import it by string.
    module = import_module("apps.catalog.migrations.0002_seed_features")

    Feature.objects.filter(slug="wallpaper").update(max_file_bytes=999_999_999)
    before = Feature.objects.count()

    module.seed_features(django_apps, None)

    assert Feature.objects.count() == before, "seeding twice must not duplicate rows"
    assert Feature.objects.get(slug="wallpaper").max_file_bytes == 999_999_999, (
        "an operator's tuned limit must survive a re-seed"
    )


def test_archived_items_are_excluded_from_counts(category):
    make_item(category=category, status=ItemStatus.READY)
    make_item(category=category, status=ItemStatus.ARCHIVED)

    run("reconcile_counts")

    category.refresh_from_db()
    assert category.item_count == 1
