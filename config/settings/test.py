"""Test settings: fast, hermetic, and never touching real B2 or Redis."""

from .base import *
from .base import CACHES  # noqa: F401

DEBUG = False
SECRET_KEY = "test-only-key-not-a-secret"
ALLOWED_HOSTS = ["*", "testserver"]

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

# Fake credentials so boto3 never reaches for ambient AWS config; moto intercepts.
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

LOGGING["root"]["level"] = "CRITICAL"
