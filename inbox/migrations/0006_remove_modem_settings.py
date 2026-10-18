from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("inbox", "0005_devicesettings")]

    operations = [
        migrations.RemoveField(model_name="devicesettings", name="device"),
        migrations.RemoveField(model_name="devicesettings", name="storage"),
    ]
