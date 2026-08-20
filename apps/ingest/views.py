"""
Staff ingest endpoints.

Every view here requires a staff user (session for the browser uploader, JWT for
a decoupled tool) and is throttled separately from public reads, so a runaway
bulk-upload script trips a limit rather than filling the bucket.

Commit returns 207 Multi-Status when a batch is mixed: the uploader needs to know
*which* of fifty files failed, and a flat 400 would throw away forty-nine
successful uploads.
"""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import ItemStatus, MediaItem
from apps.clients.throttles import IngestRateThrottle
from apps.core.exceptions import ResourceNotFound

from . import services
from .serializers import (
    AbortRequestSerializer,
    CommitRequestSerializer,
    MediaItemUpdateSerializer,
    PresignRequestSerializer,
)

logger = logging.getLogger("ingest.upload")


class StaffIngestView(APIView):
    """Base: staff only, ingest-scoped throttling."""

    permission_classes = [IsAdminUser]
    throttle_classes = [IngestRateThrottle]


class PresignUploadView(StaffIngestView):
    """
    `POST /api/v1/admin/uploads/presign`

    Returns one presigned PUT per accepted file. Rejected files come back in
    `rejected` rather than failing the whole batch, so an uploader learns about
    all fifty problems in a single round trip.
    """

    success_message = "Upload slots created"

    def post(self, request):  # noqa: ANN001, ANN201
        serializer = PresignRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        slots, rejected = services.presign_uploads(
            user=request.user,
            feature=data["feature_obj"],
            category=data["category_obj"],
            subcategory=data["subcategory_obj"],
            files=data["files"],
            request=request,
        )

        body = {
            "slots": slots,
            "rejected": rejected,
            "accepted_count": len(slots),
            "rejected_count": len(rejected),
        }
        # Nothing usable came back, so this is a client error, not a partial success.
        if not slots and rejected:
            response = Response(body, status=status.HTTP_400_BAD_REQUEST)
            response.success_message = "No files were accepted"
            return response

        code = status.HTTP_207_MULTI_STATUS if rejected else status.HTTP_201_CREATED
        response = Response(body, status=code)
        response.success_message = (
            f"{len(slots)} upload slot(s) created, {len(rejected)} rejected"
            if rejected
            else f"{len(slots)} upload slot(s) created"
        )
        return response


class CommitUploadView(StaffIngestView):
    """
    `POST /api/v1/admin/uploads/commit`

    Verifies each uploaded object against its ticket, validates the real bytes,
    derives a preview, and creates the MediaItem. Per-item results.
    """

    success_message = "Uploads committed"

    def post(self, request):  # noqa: ANN001, ANN201
        serializer = CommitRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        results = services.commit_uploads(
            user=request.user,
            items=serializer.validated_data["items"],
            request=request,
        )

        succeeded = [r for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]

        body = {
            "results": results,
            "committed_count": len(succeeded),
            "failed_count": len(failed),
        }

        if not succeeded:
            response = Response(body, status=status.HTTP_400_BAD_REQUEST)
            response.success_message = "No items were committed"
            return response

        code = status.HTTP_207_MULTI_STATUS if failed else status.HTTP_201_CREATED
        response = Response(body, status=code)
        response.success_message = (
            f"{len(succeeded)} item(s) committed, {len(failed)} failed"
            if failed
            else f"{len(succeeded)} item(s) committed"
        )
        return response


class AbortUploadView(StaffIngestView):
    """
    `POST /api/v1/admin/uploads/abort`

    Cancels issued tickets and deletes any bytes that already reached B2. Worth
    calling explicitly — the reaper would get there eventually, but a cancelled
    bulk upload otherwise leaves the bucket paying for it until then.
    """

    success_message = "Uploads aborted"

    def post(self, request):  # noqa: ANN001, ANN201
        serializer = AbortRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = services.abort_uploads(
            user=request.user,
            ticket_ids=[str(t) for t in serializer.validated_data["ticket_ids"]],
            request=request,
        )
        response = Response(result, status=status.HTTP_200_OK)
        response.success_message = f"{len(result['aborted'])} upload(s) aborted"
        return response


class MediaItemAdminView(GenericAPIView):
    """
    `PATCH /api/v1/admin/items/{id}` — edit editorial fields.
    `DELETE /api/v1/admin/items/{id}` — archive (soft delete).
    """

    permission_classes = [IsAdminUser]
    throttle_classes = [IngestRateThrottle]
    serializer_class = MediaItemUpdateSerializer
    queryset = MediaItem.objects.all()

    def get_object(self) -> MediaItem:
        item = (
            MediaItem.objects.select_related("feature", "category", "subcategory")
            .filter(pk=self.kwargs["pk"])
            .first()
        )
        if item is None:
            raise ResourceNotFound(message="Item not found.")
        return item

    def patch(self, request, *args, **kwargs):  # noqa: ANN001, ANN201, ARG002
        item = self.get_object()
        serializer = self.get_serializer(item, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        from apps.core import audit

        audit.record(
            "ITEM_UPDATE",
            request=request,
            object_type="MediaItem",
            object_id=str(item.id),
            payload={"changed": sorted(serializer.validated_data.keys())},
        )
        response = Response({"id": str(item.id)}, status=status.HTTP_200_OK)
        response.success_message = "Item updated successfully"
        return response

    def delete(self, request, *args, **kwargs):  # noqa: ANN001, ANN201, ARG002
        item = self.get_object()
        if item.status == ItemStatus.ARCHIVED:
            response = Response({"id": str(item.id)}, status=status.HTTP_200_OK)
            response.success_message = "Item was already archived"
            return response

        services.archive_item(item=item, request=request)
        response = Response(
            {
                "id": str(item.id),
                "status": ItemStatus.ARCHIVED,
                "note": "Bytes are purged after ARCHIVE_RETENTION_DAYS by the reaper.",
            },
            status=status.HTTP_200_OK,
        )
        response.success_message = "Item archived successfully"
        return response
