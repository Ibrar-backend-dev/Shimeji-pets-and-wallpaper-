"""
Test settings: fast, hermetic, and incapable of touching real infrastructure.

"Hermetic" is doing real work here. `base.py` reads `.env`, so without the
overrides below a developer who has filled in their own `DATABASE_URL` would
have the suite create a `test_<their database>` on their **production** server,
and one with `REDIS_URL` set would have the suite share cache state with a live
instance. Neither is acceptable, and neither is visible from a passing run — so
every external dependency is pinned here rather than inherited.
"""

from .base import *
from .base import LOGGING, env

DEBUG = False
SECRET_KEY = "test-only-key-not-a-secret"
ALLOWED_HOSTS = ["*", "testserver"]

# --------------------------------------------------------------------------- #
# Database — never the developer's
# --------------------------------------------------------------------------- #
# DATABASE_URL is deliberately ignored. Running the suite against Postgres is an
# explicit opt-in via TEST_DATABASE_URL, which CI sets and a laptop does not, so
# a real DSN sitting in .env can never be reached from here.
_test_database_url = env("TEST_DATABASE_URL", default="")
if _test_database_url:
    DATABASES = {"default": env.db_url_config(_test_database_url)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
            "TEST": {"NAME": ":memory:"},
        }
    }

# --------------------------------------------------------------------------- #
# Cache — always local, never shared
# --------------------------------------------------------------------------- #
# Pinned rather than inherited: REDIS_URL in a developer's .env would otherwise
# make test results depend on the state of a live cache.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-cache",
        "OPTIONS": {"MAX_ENTRIES": 5000},
    }
}

# Serve static via finders rather than a collected STATIC_ROOT: the manifest
# storage would otherwise warn on every request that the directory is missing.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

# Cheap hasher: the suite creates users constantly and never checks crypto strength.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# --------------------------------------------------------------------------- #
# Storage — fake credentials; moto intercepts every call
# --------------------------------------------------------------------------- #

# Pinned for the same reason as DATABASES and CACHES above: base.py reads .env,
# so a developer who has switched their own machine to local media storage would
# otherwise run the entire suite against the filesystem backend — silently not
# testing the B2 path that actually ships. Tests that want the local backend opt
# in with override_settings.
MEDIA_LOCAL_STORAGE = False

B2_KEY_ID = "testing"
B2_APPLICATION_KEY = "testing"
B2_BUCKET_NAME = "test-bucket"
# An AWS-shaped endpoint on purpose. moto intercepts by matching the request URL
# against AWS host patterns, so a B2-style host (s3.us-west-004.backblazeb2.com)
# would fall straight through to real DNS and time out. The code path under test
# is identical either way — only the hostname differs.
B2_ENDPOINT_URL = "https://s3.us-east-1.amazonaws.com"
B2_REGION = "us-east-1"
MEDIA_CDN_BASE_URL = "https://cdn.test.example.com"

CRON_SECRET = "test-cron-secret-value"

# Throttling and response caching are asserted explicitly where they matter, so
# they stay off by default to keep unrelated tests from interfering.
API_LIST_CACHE_SECONDS = 0
API_COUNT_CACHE_SECONDS = 0
API_CONFIG_CACHE_SECONDS = 0

# Sentry must never be reachable from the suite.
SENTRY_DSN = ""

LOGGING["root"]["level"] = "CRITICAL"
