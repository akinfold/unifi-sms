from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inbox", "0003_deviceinfo")]

    operations = [
        migrations.AddField(
            model_name="deviceinfo",
            name="model_error",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="deviceinfo",
            name="model",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="deviceinfo",
            name="model_attempted_at",
            field=models.DateTimeField(null=True),
        ),
    ]
