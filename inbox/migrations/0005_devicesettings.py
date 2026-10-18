from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inbox", "0004_deviceinfo_model")]

    operations = [
        migrations.CreateModel(
            name="DeviceSettings",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, primary_key=True, serialize=False)),
                ("host", models.CharField(blank=True, max_length=253)),
                ("port", models.PositiveIntegerField(default=22)),
                ("username", models.CharField(blank=True, max_length=254)),
                ("auth_method", models.CharField(choices=[("key_file", "Key file"), ("private_key", "Private key"), ("password", "Password")], default="key_file", max_length=16)),
                ("key_file", models.CharField(default="/run/secrets/lte_key", max_length=1024)),
                ("known_hosts", models.CharField(default="/run/secrets/known_hosts", max_length=1024)),
                ("host_keys", models.TextField(blank=True)),
                ("device", models.CharField(default="/dev/ttyUSB2", max_length=1024)),
                ("storage", models.CharField(default="SM", max_length=2)),
                ("password", models.TextField(blank=True)),
                ("private_key", models.TextField(blank=True)),
            ],
        ),
    ]
