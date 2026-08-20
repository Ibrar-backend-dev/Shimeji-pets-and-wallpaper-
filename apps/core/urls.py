"""
Internal endpoints, mounted under /internal/.

Cron work is exposed both as HTTP (for hosted schedulers and free external cron
services) and as management commands, because Render's free tier has no cron and
no background worker. Same code path either way.
"""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("cron/reap-orphans", views.ReapOrphansView.as_view(), name="cron-reap-orphans"),
    path(
        "cron/reconcile-counts",
        views.ReconcileCountsView.as_view(),
        name="cron-reconcile-counts",
    ),
]
