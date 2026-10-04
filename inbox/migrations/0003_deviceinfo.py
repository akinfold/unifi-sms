from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inbox", "0002_smtpsettings_forwarddelivery")]

    operations = [
        migrations.CreateModel(
            name="DeviceInfo",
            fields=[
                ("source", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("numbers", models.JSONField(default=list)),
                ("requested_at", models.DateTimeField(null=True)),
                ("attempted_at", models.DateTimeField(null=True)),
                ("completed_at", models.DateTimeField(null=True)),
                ("checked_at", models.DateTimeField(null=True)),
                ("last_error", models.CharField(blank=True, max_length=250)),
            ],
        ),
    ]
