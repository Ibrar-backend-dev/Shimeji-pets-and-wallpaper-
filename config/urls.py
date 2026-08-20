"""
Root URL configuration.

The admin path is configurable (DJANGO_ADMIN_URL) so production can move it off
the well-known /admin/ and out of the way of automated login attempts.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from .health import healthz, readyz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path(settings.ADMIN_URL, admin.site.urls),
    path("api/v1/", include("apps.catalog.urls")),
    path("api/v1/admin/", include("apps.ingest.urls")),
    path("internal/", include("apps.core.urls")),
]

if settings.DEBUG:
    try:
        import debug_toolbar  # noqa: F401

        urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
    except ImportError:
        pass
