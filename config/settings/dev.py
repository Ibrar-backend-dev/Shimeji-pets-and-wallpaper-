"""Local development settings. Never use for a deployed environment."""

from .base import *  # noqa: F401,F403
from .base import INSTALLED_APPS, LOGGING, MIDDLEWARE, env

DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = ["*"]

# Relaxed so a plain `runserver` on http works.
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Handy for spotting N+1s by eye; the test suite asserts query counts anyway.
if env.bool("DJANGO_SHOW_SQL", default=False):
    LOGGING["loggers"]["django.db.backends"]["level"] = "DEBUG"

if env.bool("DJANGO_DEBUG_TOOLBAR", default=False):
    try:
        import debug_toolbar  # noqa: F401

        INSTALLED_APPS = [*INSTALLED_APPS, "debug_toolbar"]
        MIDDLEWARE = [
            "debug_toolbar.middleware.DebugToolbarMiddleware",
            *MIDDLEWARE,
        ]
        INTERNAL_IPS = ["127.0.0.1"]
    except ImportError:
        pass
