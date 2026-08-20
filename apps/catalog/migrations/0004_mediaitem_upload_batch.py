import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("catalog", "0003_subcategory_category_fk"), ("ingest", "0002_multipart_uploads")]

    operations = [
        migrations.AddField(
            model_name="mediaitem",
            name="upload_batch",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="items", to="ingest.uploadbatch"),
        )
    ]
