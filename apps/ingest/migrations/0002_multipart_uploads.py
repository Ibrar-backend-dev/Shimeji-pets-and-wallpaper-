import apps.core.ids
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("catalog", "0003_subcategory_category_fk"), migrations.swappable_dependency(settings.AUTH_USER_MODEL), ("ingest", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="UploadBatch",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=apps.core.ids.uuid7, editable=False, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("publish_mode", models.CharField(max_length=10, choices=[("DRAFT", "Draft: publish explicitly"), ("IMMEDIATE", "Publish when finalized")], default="DRAFT")),
                ("status", models.CharField(max_length=10, choices=[("OPEN", "Open"), ("COMPLETED", "Completed"), ("ABORTED", "Aborted"), ("EXPIRED", "Expired")], default="OPEN")),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("category", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="upload_batches", to="catalog.category")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="upload_batches", to=settings.AUTH_USER_MODEL)),
                ("feature", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="upload_batches", to="catalog.feature")),
                ("subcategory", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="upload_batches", to="catalog.subcategory")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="MultipartUploadSession",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=apps.core.ids.uuid7, editable=False, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("kind", models.CharField(max_length=8, choices=[("ASSET", "Main asset"), ("PREVIEW", "Preview image")], default="ASSET")),
                ("object_key", models.CharField(max_length=512, unique=True)),
                ("storage_upload_id", models.CharField(max_length=512, unique=True)),
                ("declared_name", models.CharField(max_length=255, blank=True)),
                ("declared_mime", models.CharField(max_length=100)),
                ("declared_bytes", models.BigIntegerField()),
                ("part_size", models.PositiveIntegerField()),
                ("metadata", models.JSONField(default=dict, blank=True)),
                ("status", models.CharField(max_length=12, choices=[("UPLOADING", "Uploading"), ("UPLOADED", "Uploaded to storage"), ("FINALIZED", "Media item created"), ("ABORTED", "Aborted"), ("EXPIRED", "Expired"), ("FAILED", "Validation failed")], default="UPLOADING")),
                ("failure_reason", models.TextField(blank=True)),
                ("completed_at", models.DateTimeField(null=True, blank=True)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sessions", to="ingest.uploadbatch")),
            ],
            options={"ordering": ("created_at",)},
        ),
        migrations.AddIndex(model_name="uploadbatch", index=models.Index(fields=["created_by", "status", "-created_at"], name="batch_owner_status_idx")),
        migrations.AddIndex(model_name="multipartuploadsession", index=models.Index(fields=["status", "updated_at"], name="multipart_status_idx")),
    ]
