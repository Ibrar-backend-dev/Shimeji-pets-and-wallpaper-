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
    path("api/v2/admin/", include("apps.ingest.v2_urls")),
    path("internal/", include("apps.core.urls")),
]

# Local media: the upload target and the serve route that stand in for B2 and
# the CDN. Mounted only when the flag is on, so a production deployment has no
# route to them at all — the views re-check the flag anyway, but not mounting
# them is the stronger guarantee.
if settings.MEDIA_LOCAL_STORAGE:
    from apps.ingest import views_media

    urlpatterns += [
        path("media/upload/", views_media.upload_object, name="local-media-upload"),
        path("media/upload-part/", views_media.upload_part, name="local-media-upload-part"),
        # <path:key> so the slashes inside an object key survive routing.
        path("media/<path:key>", views_media.serve_media, name="local-media-serve"),
    ]

if settings.DEBUG:
    try:
        import debug_toolbar  # noqa: F401

        urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
    except ImportError:
        pass
