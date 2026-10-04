import django.db.models.deletion
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inbox", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SMTPSettings",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(default=1, primary_key=True, serialize=False),
                ),
                ("enabled", models.BooleanField(default=False)),
                ("host", models.CharField(blank=True, max_length=253)),
                ("port", models.PositiveIntegerField(default=587)),
                ("username", models.CharField(blank=True, max_length=254)),
                ("password", models.TextField(blank=True)),
                (
                    "security",
                    models.CharField(
                        choices=[("starttls", "STARTTLS"), ("tls", "TLS"), ("none", "None")],
                        default="starttls",
                        max_length=8,
                    ),
                ),
                ("from_email", models.EmailField(blank=True, max_length=254)),
                ("recipients", models.TextField(blank=True)),
            ],
        ),
        migrations.CreateModel(
            name="ForwardDelivery",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("token", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("sent_at", models.DateTimeField(null=True)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("next_attempt_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_error", models.CharField(blank=True, max_length=250)),
                ("recipients", models.JSONField(default=list)),
                ("delivered_to", models.JSONField(default=list)),
                (
                    "message",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="forwarding",
                        to="inbox.message",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["sent_at", "next_attempt_at"], name="forward_due_idx")
                ],
            },
        ),
    ]
