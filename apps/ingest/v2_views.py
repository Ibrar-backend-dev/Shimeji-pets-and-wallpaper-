"""Staff-only resumable multipart upload API."""

from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.clients.throttles import IngestRateThrottle
from apps.core.exceptions import ResourceNotFound

from . import services, validators
from .models import BatchStatus, MultipartUploadSession, UploadBatch
from .v2_serializers import (
    BatchCreateSerializer,
    FinalizeSerializer,
    PartURLsSerializer,
    PublishSerializer,
    SessionCreateSerializer,
)


def batch_data(batch: UploadBatch) -> dict:
    return {
        "id": str(batch.id),
        "type": batch.feature.slug,
        "category_id": str(batch.category_id),
        "subcategory_id": str(batch.subcategory_id) if batch.subcategory_id else None,
        "publish_mode": batch.publish_mode,
        "status": batch.status,
        "expires_at": batch.expires_at.isoformat(),
        "sessions": [session_data(session) for session in batch.sessions.all()],
        "items": [
            {"id": str(item.id), "status": item.status, "name": item.name}
            for item in batch.items.all()
        ],
    }


def session_data(session: MultipartUploadSession) -> dict:
    return {
        "id": str(session.id),
        "kind": session.kind,
        "filename": session.declared_name,
        "content_type": session.declared_mime,
        "size": session.declared_bytes,
        "object_key": session.object_key,
        "part_size": session.part_size,
        "part_count": session.part_count,
        "status": session.status,
        "failure_reason": session.failure_reason,
        "metadata": session.metadata,
    }


class V2StaffView(APIView):
    permission_classes = [IsAdminUser]
    throttle_classes = [IngestRateThrottle]

    def handle_exception(self, exc):
        if isinstance(exc, validators.ValidationFailure):
            exc = ValidationError({"upload": [exc.message]})
        return super().handle_exception(exc)

    def batch(self, pk) -> UploadBatch:
        batch = (
            UploadBatch.objects.select_related("feature", "category", "subcategory")
            .prefetch_related("sessions", "items")
            .filter(pk=pk, created_by=self.request.user)
            .first()
        )
        if batch is None:
            raise ResourceNotFound(message="Upload batch not found.")
        return batch

    def session(self, pk) -> MultipartUploadSession:
        session = (
            MultipartUploadSession.objects.select_related(
                "batch__feature", "batch__category", "batch__subcategory"
            )
            .filter(pk=pk, batch__created_by=self.request.user)
            .first()
        )
        if session is None:
            raise ResourceNotFound(message="Upload session not found.")
        return session


class UploadBatchListView(V2StaffView):
    success_message = "Upload batches fetched"

    def get(self, request):
        batches = (
            UploadBatch.objects.filter(created_by=request.user)
            .select_related("feature", "category", "subcategory")
            .prefetch_related("sessions", "items")
        )
        if request.query_params.get("active") in {"1", "true", "yes"}:
            batches = batches.filter(status=BatchStatus.OPEN, expires_at__gte=timezone.now())
        return Response({"items": [batch_data(batch) for batch in batches[:100]]})

    def post(self, request):
        serializer = BatchCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch = services.create_upload_batch(
            user=request.user,
            feature_slug=serializer.validated_data["type"],
            category_id=serializer.validated_data["category_id"],
            subcategory_id=serializer.validated_data.get("subcategory_id"),
            publish_mode=serializer.validated_data["publish_mode"],
        )
        response = Response(batch_data(batch), status=status.HTTP_201_CREATED)
        response.success_message = "Upload batch created"
        return response


class UploadBatchDetailView(V2StaffView):
    success_message = "Upload batch fetched"

    def get(self, request, pk):
        return Response(batch_data(self.batch(pk)))


class UploadBatchSessionView(V2StaffView):
    success_message = "Multipart session created"

    def post(self, request, pk):
        batch = self.batch(pk)
        if not batch.is_redeemable_by(request.user):
            return Response(
                {"detail": "Upload batch is expired or closed."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = SessionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = services.create_multipart_session(
            batch=batch, payload=serializer.validated_data
        )
        return Response(session_data(session), status=status.HTTP_201_CREATED)


class MultipartPartsView(V2StaffView):
    success_message = "Multipart parts fetched"

    def get(self, request, pk):
        return Response(
            {
                "session": session_data(self.session(pk)),
                "parts": services.multipart_parts(self.session(pk)),
            }
        )


class MultipartPartURLsView(V2StaffView):
    success_message = "Part URLs created"

    def post(self, request, pk):
        session = self.session(pk)
        if not session.is_redeemable_by(request.user):
            return Response(
                {"detail": "Upload session is expired or closed."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = PartURLsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            services.multipart_part_urls(
                session=session, part_numbers=serializer.validated_data["part_numbers"]
            )
        )


class MultipartCompleteView(V2StaffView):
    success_message = "Multipart upload completed"

    def post(self, request, pk):
        return Response(services.complete_multipart_session(session=self.session(pk)))


class MultipartFinalizeView(V2StaffView):
    success_message = "Media item finalized"

    def post(self, request, pk):
        serializer = FinalizeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = services.finalize_multipart_session(
            session=self.session(pk),
            preview_session_id=serializer.validated_data.get("preview_session_id"),
            request=request,
        )
        return Response(
            {"id": str(item.id), "status": item.status}, status=status.HTTP_201_CREATED
        )


class UploadBatchPublishView(V2StaffView):
    success_message = "Draft items published"

    def post(self, request, pk):
        serializer = PublishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            services.publish_batch(
                batch=self.batch(pk),
                item_ids=serializer.validated_data["item_ids"],
                request=request,
            )
        )


class UploadBatchAbortView(V2StaffView):
    success_message = "Upload batch aborted"

    def post(self, request, pk):
        batch = self.batch(pk)
        aborted = [
            str(session.id)
            for session in batch.sessions.all()
            if services.abort_multipart_session(session)
        ]
        batch.status = BatchStatus.ABORTED
        batch.save(update_fields=["status", "updated_at"])
        return Response({"aborted_sessions": aborted})
