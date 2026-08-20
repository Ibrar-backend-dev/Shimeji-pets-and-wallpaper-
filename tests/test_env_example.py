"""
`.env.example` has to actually work.

The README tells a new developer to `cp .env.example .env`, so a mistake in that
file is a project that does not start — which is exactly what happened once:
every value carrying a trailing `# comment` was read as part of the value,
because django-environ (unlike python-dotenv) does not strip them.

The failure modes were not equally loud, which is what makes this worth a test:

    PRESIGN_EXPIRY_SECONDS=900  # note   -> ValueError, at least visible
    LOG_JSON=True               # note   -> silently False
    REDIS_URL=                  # note   -> a truthy string, used as a real URL

Inline comments are not stripped in the settings module on purpose: a `#` is
perfectly legal inside a password or a URL fragment, so stripping would corrupt
real credentials. The fix belongs in the file, and this test keeps it there.
"""

from __future__ import annotations

from pathlib import Path

import environ
import pytest

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

# Every variable base.py casts to something other than a string, with the cast
# it uses. Keep in step with config/settings/base.py.
INT_VARS = [
    "PRESIGN_EXPIRY_SECONDS",
    "UPLOAD_BULK_MAX_FILES",
    "INLINE_PROCESS_MAX_BYTES",
    "ARCHIVE_RETENTION_DAYS",
    "ZIP_MAX_ENTRIES",
    "ZIP_MAX_UNCOMPRESSED_BYTES",
    "ZIP_MAX_COMPRESSION_RATIO",
    "PREVIEW_MAX_EDGE",
    "PREVIEW_JPEG_QUALITY",
    "API_PAGE_SIZE_DEFAULT",
    "API_PAGE_SIZE_MAX",
    "API_MAX_SKIP",
    "API_LIST_CACHE_SECONDS",
    "API_COUNT_CACHE_SECONDS",
    "API_CONFIG_CACHE_SECONDS",
    "APP_CLIENT_TOUCH_INTERVAL_SECONDS",
]
BOOL_VARS = ["DJANGO_DEBUG", "LOG_JSON"]
LIST_VARS = ["DJANGO_ALLOWED_HOSTS", "DJANGO_CSRF_TRUSTED_ORIGINS"]


def parse_example() -> dict[str, str]:
    """Read .env.example the way django-environ would, without touching os.environ."""
    values: dict[str, str] = {}
    for raw_line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value
    return values


def test_env_example_exists():
    assert ENV_EXAMPLE.is_file(), "the README instructs copying this file"


def test_no_value_carries_an_inline_comment():
    """
    The general guard.

    Every value in this file is a placeholder or a plain number, so a `#`
    anywhere in one means a comment has crept back onto a value line.
    """
    offenders = {k: v for k, v in parse_example().items() if "#" in v}
    assert not offenders, (
        "inline comments are not stripped by django-environ; move these onto "
        f"their own line: {offenders}"
    )


def test_no_value_has_trailing_whitespace():
    """Trailing spaces survive parsing and break an otherwise-correct comparison."""
    offenders = {k: v for k, v in parse_example().items() if v != v.rstrip()}
    assert not offenders, f"trailing whitespace in: {list(offenders)}"


@pytest.mark.parametrize("name", INT_VARS)
def test_int_vars_cast_cleanly(name):
    values = parse_example()
    assert name in values, f"{name} is read by base.py but absent from .env.example"
    int(values[name])  # raises if a comment or stray text got in


@pytest.mark.parametrize("name", BOOL_VARS)
def test_bool_vars_cast_to_a_real_boolean(name):
    """
    The dangerous case: an unparseable bool does not raise, it just becomes
    False. So assert the parsed value round-trips to what the file plainly says.
    """
    values = parse_example()
    assert name in values

    env = environ.Env()
    env.ENVIRON = values
    parsed = env.bool(name)

    literal = values[name].strip().lower()
    expected = literal in {"true", "on", "ok", "y", "yes", "1"}
    assert parsed is expected, f"{name} reads as {values[name]!r} but parses to {parsed!r}"


@pytest.mark.parametrize("name", LIST_VARS)
def test_list_vars_have_no_stray_entries(name):
    values = parse_example()
    if not values.get(name):
        return
    entries = [part.strip() for part in values[name].split(",")]
    assert all(entries), f"{name} has an empty element: {values[name]!r}"
    assert not any("#" in e for e in entries)


def test_optional_vars_are_genuinely_empty():
    """
    `REDIS_URL` and `SENTRY_DSN` switch features on merely by being non-empty,
    so a comment or a stray space here silently enables a broken integration.
    """
    values = parse_example()
    for name in ("REDIS_URL", "SENTRY_DSN", "DJANGO_CORS_ALLOWED_ORIGINS", "DATABASE_URL"):
        assert values.get(name, "") == "", (
            f"{name} must be blank in .env.example so it stays disabled by "
            f"default, got {values.get(name)!r}"
        )


def test_example_values_match_the_settings_defaults():
    """
    The file should document reality.

    A developer reading `.env.example` to learn a default, and getting a
    different number from the one base.py actually falls back to, is worse than
    no documentation.
    """
    from django.conf import settings

    values = parse_example()
    for name in INT_VARS:
        if not hasattr(settings, name):
            continue
        # test.py overrides a few cache TTLs to 0 to keep tests hermetic.
        if name in {
            "API_LIST_CACHE_SECONDS",
            "API_COUNT_CACHE_SECONDS",
            "API_CONFIG_CACHE_SECONDS",
        }:
            continue
        assert int(values[name]) == getattr(settings, name), (
            f"{name}: .env.example says {values[name]}, settings resolve to "
            f"{getattr(settings, name)}"
        )


def test_cron_secret_example_meets_the_minimum_length():
    """prod.py rejects a CRON_SECRET under 16 characters, and HasCronSecret under 8."""
    values = parse_example()
    assert len(values["CRON_SECRET"]) >= 8, (
        "the example value must at least satisfy HasCronSecret, or local cron "
        "calls fail confusingly"
    )


def test_every_documented_var_is_actually_read_somewhere():
    """
    Catches the reverse rot: a variable removed from the code but left in the
    example, which sends someone configuring something that does nothing.
    """
    settings_dir = Path(__file__).resolve().parent.parent / "config" / "settings"
    source = "\n".join(path.read_text(encoding="utf-8") for path in settings_dir.glob("*.py"))
    # These are read by Django or the tooling itself, not via env().
    externally_consumed = {"DJANGO_SETTINGS_MODULE"}

    unused = [
        name
        for name in parse_example()
        if name not in externally_consumed and name not in source
    ]
    assert not unused, f".env.example documents variables nothing reads: {unused}"


# --------------------------------------------------------------------------- #
# Test-settings isolation
# --------------------------------------------------------------------------- #
#
# These guard a defect that was live for a while: config/settings/test.py
# inherited DATABASES from base.py, which reads .env — so a developer with a
# real DATABASE_URL had the suite create a `test_<their db>` on their own
# production server. A passing run gives no hint that happened, which is exactly
# why it needs an assertion rather than a convention.


def test_suite_never_targets_a_remote_database():
    """The database under test must be local, or explicitly opted into."""
    import os

    from django.conf import settings

    host = str(settings.DATABASES["default"].get("HOST") or "")
    opted_in = bool(os.environ.get("TEST_DATABASE_URL"))

    if not opted_in:
        assert host in ("", "localhost", "127.0.0.1"), (
            f"the suite is pointed at {host!r}. test.py must ignore DATABASE_URL "
            "and only honour TEST_DATABASE_URL"
        )


def test_suite_ignores_the_developers_database_url(monkeypatch):
    """
    Re-import the test settings with a hostile DATABASE_URL and confirm it is
    not picked up.
    """
    import importlib

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:pw@production.example.com:5432/live_db?sslmode=require",
    )
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)

    module = importlib.reload(importlib.import_module("config.settings.test"))

    assert "sqlite" in module.DATABASES["default"]["ENGINE"]
    assert "production.example.com" not in str(module.DATABASES)


def test_suite_never_uses_a_shared_cache():
    """A live Redis would make test results depend on another process's state."""
    from django.conf import settings

    assert "locmem" in settings.CACHES["default"]["BACKEND"].lower()


def test_suite_has_no_real_credentials():
    from django.conf import settings

    assert settings.B2_KEY_ID == "testing"
    assert settings.B2_APPLICATION_KEY == "testing"
    assert settings.SENTRY_DSN == ""
    assert "test" in settings.SECRET_KEY
