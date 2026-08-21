import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("catalog", "0004_mediaitem_upload_batch")]

    operations = [
        migrations.AddField(
            model_name="mediaitem",
            name="color_code",
            field=models.CharField(
                blank=True,
                help_text="Optional six-digit hex colour for Shimeji and Battery items.",
                max_length=7,
                null=True,
                validators=[
                    django.core.validators.RegexValidator(
                        "^#[0-9a-fA-F]{6}$",
                        "Use a six-digit hexadecimal colour code, for example #1a2b3c.",
                    )
                ],
            ),
        )
    ]
