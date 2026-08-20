from django.urls import path

from . import v2_views as views

app_name = "ingest-v2"

urlpatterns = [
    path("upload-batches", views.UploadBatchListView.as_view()),
    path("upload-batches/<uuid:pk>", views.UploadBatchDetailView.as_view()),
    path("upload-batches/<uuid:pk>/sessions", views.UploadBatchSessionView.as_view()),
    path("upload-batches/<uuid:pk>/publish", views.UploadBatchPublishView.as_view()),
    path("upload-batches/<uuid:pk>/abort", views.UploadBatchAbortView.as_view()),
    path("multipart-sessions/<uuid:pk>/parts", views.MultipartPartsView.as_view()),
    path("multipart-sessions/<uuid:pk>/part-urls", views.MultipartPartURLsView.as_view()),
    path("multipart-sessions/<uuid:pk>/complete", views.MultipartCompleteView.as_view()),
    path("multipart-sessions/<uuid:pk>/finalize", views.MultipartFinalizeView.as_view()),
]
