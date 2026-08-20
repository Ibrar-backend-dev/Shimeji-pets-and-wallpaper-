"""
Internal cron endpoints.

Guarded by X-Cron-Secret rather than a user session, because the callers are
schedulers: Railway cron, GitHub Actions, or a free external cron service
pointed at these URLs. Render's free tier offers neither cron nor a worker, which
is why an HTTP trigger exists at all.

They deliberately do not use the public throttles — a scheduler is not a client —
but they are cheap, bounded per run, and idempotent.
"""

from __future__ import annotations

from rest_framework.response import Response
from rest_framework.views import APIView

from apps.clients.permissions import HasCronSecret

from . import maintenance
from .maintenance import DEFAULT_BATCH_LIMIT


def _bounded_limit(request, default: int = DEFAULT_BATCH_LIMIT) -> int:  # noqa: ANN001
    """Allow a caller to lower the batch size, never to raise it past the cap."""
    raw = request.query_params.get("limit")
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, 1000))


class CronView(APIView):
    permission_classes = [HasCronSecret]
    authentication_classes: list = []
    throttle_classes: list = []


class ReapOrphansView(CronView):
    """`POST /internal/cron/reap-orphans`"""

    success_message = "Orphan reap complete"

    def post(self, request):  # noqa: ANN001, ANN201
        stats = maintenance.reap_orphans(
            batch_limit=_bounded_limit(request),
            request=request,
            dry_run=request.query_params.get("dry_run") in {"1", "true", "yes"},
        )
        return Response(stats)


class ReconcileCountsView(CronView):
    """`POST /internal/cron/reconcile-counts`"""

    success_message = "Count reconcile complete"

    def post(self, request):  # noqa: ANN001, ANN201
        stats = maintenance.reconcile_counts(
            request=request,
            dry_run=request.query_params.get("dry_run") in {"1", "true", "yes"},
        )
        return Response(stats)
