"""Staff ingest routes, mounted under /api/v1/admin/."""

from django.urls import path

from . import views

app_name = "ingest"

urlpatterns = [
    path("uploads/presign", views.PresignUploadView.as_view(), name="upload-presign"),
    path("uploads/commit", views.CommitUploadView.as_view(), name="upload-commit"),
    path("uploads/abort", views.AbortUploadView.as_view(), name="upload-abort"),
    path("items/<uuid:pk>", views.MediaItemAdminView.as_view(), name="item-admin"),
]
