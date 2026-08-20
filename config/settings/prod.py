"""
Production settings.

Fails fast and loudly on missing configuration. A backend that boots with a
default SECRET_KEY or an open ALLOWED_HOSTS is worse than one that refuses to
start, because the breakage is silent.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *
from .base import CRON_SECRET, DATABASES, SECRET_KEY, env

DEBUG = False

_INSECURE_KEYS = {"", "dev-only-insecure-key-change-me"}
_required = {
    "DJANGO_SECRET_KEY": SECRET_KEY not in _INSECURE_KEYS,
    "DJANGO_ALLOWED_HOSTS": bool(env.list("DJANGO_ALLOWED_HOSTS", default=[])),
    "DATABASE_URL": bool(env("DATABASE_URL", default="")),
    "CRON_SECRET": len(CRON_SECRET) >= 16,
}
_missing = sorted(name for name, ok in _required.items() if not ok)
if _missing:
    raise ImproperlyConfigured(
        "Refusing to start: missing or insecure production settings: "
        + ", ".join(_missing)
        + ". See .env.example for what each one needs."
    )

if "*" in env.list("DJANGO_ALLOWED_HOSTS", default=[]):
    raise ImproperlyConfigured("ALLOWED_HOSTS must not contain '*' in production.")

# --------------------------------------------------------------------------- #
# TLS / proxy
# --------------------------------------------------------------------------- #
# Render and Railway both terminate TLS at their edge and forward over http.
# Without this header mapping, SECURE_SSL_REDIRECT sees http and redirect-loops.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)

SECURE_HSTS_SECONDS = env.int("DJANGO_HSTS_SECONDS", default=31536000)  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = False  # the admin's JS needs to read it
CSRF_COOKIE_SAMESITE = "Lax"

# --------------------------------------------------------------------------- #
# Database — Neon pooled connection
# --------------------------------------------------------------------------- #
# Neon's pooled endpoint is pgbouncer in transaction mode. Persistent
# connections and server-side cursors are both incompatible with it; leaving
# them on produces intermittent InterfaceError under load rather than a clean
# failure, which is why these are pinned rather than left to defaults.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DJANGO_CONN_MAX_AGE", default=0)
DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = True
DATABASES["default"].setdefault("OPTIONS", {})
DATABASES["default"]["OPTIONS"].setdefault("connect_timeout", 10)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = env.bool(
    "DJANGO_CONN_HEALTH_CHECKS", default=False
)

# --------------------------------------------------------------------------- #
# Error reporting — inert unless SENTRY_DSN is set
# --------------------------------------------------------------------------- #
_sentry_dsn = env("SENTRY_DSN", default="")
if _sentry_dsn:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration

        sentry_sdk.init(
            dsn=_sentry_dsn,
            integrations=[DjangoIntegration()],
            traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
            send_default_pii=False,
            environment=env("SENTRY_ENVIRONMENT", default="production"),
        )
    except ImportError:  # pragma: no cover
        pass
