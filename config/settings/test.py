"""Test settings: fast, hermetic, and never touching real B2 or Redis."""

from .base import *  # noqa: F401,F403
from .base import CACHES  # noqa: F401

DEBUG = False
SECRET_KEY = "test-only-key-not-a-secret"
ALLOWED_HOSTS = ["*", "testserver"]

# Cheap hasher: the suite creates users constantly and never checks crypto strength.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Fake credentials so boto3 never reaches for ambient AWS config; moto intercepts.
B2_KEY_ID = "testing"
B2_APPLICATION_KEY = "testing"
B2_BUCKET_NAME = "test-bucket"
B2_ENDPOINT_URL = "https://s3.test.example.com"
B2_REGION = "us-east-1"
MEDIA_CDN_BASE_URL = "https://cdn.test.example.com"

CRON_SECRET = "test-cron-secret-value"

# Throttling and response caching are asserted explicitly where they matter, so
# they stay off by default to keep unrelated tests from interfering.
API_LIST_CACHE_SECONDS = 0
API_COUNT_CACHE_SECONDS = 0
API_CONFIG_CACHE_SECONDS = 0

LOGGING["root"]["level"] = "CRITICAL"  # noqa: F405
