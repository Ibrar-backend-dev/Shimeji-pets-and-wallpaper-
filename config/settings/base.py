"""
Settings shared by every environment.

Environment-specific modules (dev.py / prod.py) import * from here and then
tighten or relax. Nothing in this file may assume DEBUG either way.
"""

import datetime
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
# Read .env if present. Absent is fine: real deployments inject real env vars.
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    env.read_env(str(_env_file))

# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-only-insecure-key-change-me")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# Admin lives at a configurable path so prod can move it off /admin/.
ADMIN_URL = env("DJANGO_ADMIN_URL", default="admin/")

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
ROOT_URLCONF = "config.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third party
    "rest_framework",
    "corsheaders",
    # local
    "apps.core",
    "apps.catalog",
    "apps.clients",
    "apps.ingest",
]

MIDDLEWARE = [
    # RequestID first so every later log line and error envelope carries it.
    "apps.core.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    # Turns our ETag/Last-Modified headers into real 304s.
    "django.middleware.http.ConditionalGetMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Resolves X-API-Key onto request.app_client before any view or permission
    # runs. See apps/clients/authentication.py for why this is not a DRF
    # authenticator.
    "apps.clients.middleware.AppKeyMiddleware",
    # Access log last so it observes the final status code.
    "apps.core.middleware.AccessLogMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
# DATABASE_URL is required in prod (asserted in prod.py). In dev an empty value
# falls back to SQLite so the project runs with no Postgres installed.
_database_url = env("DATABASE_URL", default="")
if _database_url:
    DATABASES = {"default": env.db_url_config(_database_url)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(BASE_DIR / "db.sqlite3"),
        }
    }

# --------------------------------------------------------------------------- #
# Auth / i18n
# --------------------------------------------------------------------------- #
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------- #
# Static files (admin CSS only — all user media lives on B2)
# --------------------------------------------------------------------------- #
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Real media bytes are uploaded straight to B2 by the browser, so Django should
# never accept a large multipart body. Keep these small on purpose.
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024  # 2 MiB
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000

# --------------------------------------------------------------------------- #
# Cache — local memory by default, Redis when REDIS_URL is provided
# --------------------------------------------------------------------------- #
_redis_url = env("REDIS_URL", default="")
if _redis_url:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _redis_url,
            "TIMEOUT": 300,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "shimeji-default",
            "TIMEOUT": 300,
            "OPTIONS": {"MAX_ENTRIES": 5000},
        }
    }

# --------------------------------------------------------------------------- #
# Django REST Framework
# --------------------------------------------------------------------------- #
REST_FRAMEWORK = {
    # Every response — success and error — goes through the envelope renderer.
    "DEFAULT_RENDERER_CLASSES": [
        "apps.core.renderers.EnvelopeJSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "EXCEPTION_HANDLER": "apps.core.exceptions.envelope_exception_handler",
    # API keys are handled by AppKeyMiddleware, not here: they identify an app,
    # not a user. These two authenticate the *staff* users who drive ingest.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        # Public endpoints opt in explicitly; anything unmarked stays closed.
        "rest_framework.permissions.IsAdminUser",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.SkipLimitPagination",
    "DEFAULT_THROTTLE_RATES": {
        # Fallbacks; AppClient.rate_limit_per_min overrides per client.
        "public_read": "120/min",
        "ingest": "60/min",
    },
    "COERCE_DECIMAL_TO_STRING": False,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": datetime.timedelta(hours=2),
    "REFRESH_TOKEN_LIFETIME": datetime.timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# --------------------------------------------------------------------------- #
# CORS — Android does not need it, a web front-end does. Explicit allowlist only.
# --------------------------------------------------------------------------- #
CORS_ALLOWED_ORIGINS = env.list("DJANGO_CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_CREDENTIALS = False
CORS_ALLOW_HEADERS = (
    "accept",
    "authorization",
    "content-type",
    "origin",
    "x-api-key",
    "x-request-id",
    "if-none-match",
    "if-modified-since",
)

# --------------------------------------------------------------------------- #
# Backblaze B2 (S3-compatible)
# --------------------------------------------------------------------------- #
B2_KEY_ID = env("B2_KEY_ID", default="")
B2_APPLICATION_KEY = env("B2_APPLICATION_KEY", default="")
B2_BUCKET_NAME = env("B2_BUCKET_NAME", default="")
B2_ENDPOINT_URL = env("B2_ENDPOINT_URL", default="")
B2_REGION = env("B2_REGION", default="us-west-004")

# Public CDN base that fronts the bucket. Serializers build every URL from this,
# so no URL is ever persisted and swapping CDN hosts needs no migration.
MEDIA_CDN_BASE_URL = env("MEDIA_CDN_BASE_URL", default="").rstrip("/")

# --------------------------------------------------------------------------- #
# Upload policy
# --------------------------------------------------------------------------- #
PRESIGN_EXPIRY_SECONDS = env.int("PRESIGN_EXPIRY_SECONDS", default=900)
UPLOAD_BULK_MAX_FILES = env.int("UPLOAD_BULK_MAX_FILES", default=50)
INLINE_PROCESS_MAX_BYTES = env.int("INLINE_PROCESS_MAX_BYTES", default=12 * 1024 * 1024)
ARCHIVE_RETENTION_DAYS = env.int("ARCHIVE_RETENTION_DAYS", default=30)

# Zip-bomb ceilings, applied to Shimeji packs.
ZIP_MAX_ENTRIES = env.int("ZIP_MAX_ENTRIES", default=2000)
ZIP_MAX_UNCOMPRESSED_BYTES = env.int(
    "ZIP_MAX_UNCOMPRESSED_BYTES", default=256 * 1024 * 1024
)
ZIP_MAX_COMPRESSION_RATIO = env.int("ZIP_MAX_COMPRESSION_RATIO", default=100)

# Generated preview (thumbnail) box, in pixels. Aspect ratio is preserved.
PREVIEW_MAX_EDGE = env.int("PREVIEW_MAX_EDGE", default=720)
PREVIEW_JPEG_QUALITY = env.int("PREVIEW_JPEG_QUALITY", default=82)

# --------------------------------------------------------------------------- #
# API behaviour
# --------------------------------------------------------------------------- #
API_PAGE_SIZE_DEFAULT = env.int("API_PAGE_SIZE_DEFAULT", default=20)
API_PAGE_SIZE_MAX = env.int("API_PAGE_SIZE_MAX", default=100)
API_MAX_SKIP = env.int("API_MAX_SKIP", default=10_000)
API_LIST_CACHE_SECONDS = env.int("API_LIST_CACHE_SECONDS", default=300)
API_COUNT_CACHE_SECONDS = env.int("API_COUNT_CACHE_SECONDS", default=60)
API_CONFIG_CACHE_SECONDS = env.int("API_CONFIG_CACHE_SECONDS", default=120)
# How stale AppClient.last_used_at may get. Prevents a DB write per read.
APP_CLIENT_TOUCH_INTERVAL_SECONDS = env.int(
    "APP_CLIENT_TOUCH_INTERVAL_SECONDS", default=600
)

CRON_SECRET = env("CRON_SECRET", default="")

# --------------------------------------------------------------------------- #
# Logging — structured JSON to stdout (12-factor; the platform captures it)
# --------------------------------------------------------------------------- #
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOG_JSON = env.bool("LOG_JSON", default=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "apps.core.logging.RequestIDFilter"},
    },
    "formatters": {
        "json": {"()": "apps.core.logging.JSONFormatter"},
        "console": {
            "format": "%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s",
        },
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "json" if LOG_JSON else "console",
            "filters": ["request_id"],
        },
    },
    "root": {"handlers": ["stdout"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {
            "handlers": ["stdout"],
            "level": "WARNING",
            "propagate": False,
        },
        # Never raise to DEBUG in prod: it logs every query.
        "django.db.backends": {
            "handlers": ["stdout"],
            "level": "WARNING",
            "propagate": False,
        },
        "catalog.api": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
        "ingest.upload": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
        "storage.b2": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
        "clients.auth": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
        "core.access": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
    },
}
