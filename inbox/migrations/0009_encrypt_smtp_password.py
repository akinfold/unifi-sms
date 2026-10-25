import base64
import hashlib

from cryptography.fernet import Fernet
from django.conf import settings
from django.db import migrations

import inbox.fields


def cipher():
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_passwords(apps, schema_editor):
    model = apps.get_model("inbox", "SMTPSettings")
    database = schema_editor.connection.alias
    encryption = cipher()
    for row in model.objects.using(database).exclude(password="").iterator():
        # Every value in the old field is plaintext, including passwords beginning with fernet:.
        encrypted = "fernet:" + encryption.encrypt(row.password.encode("utf-8")).decode("ascii")
        model.objects.using(database).filter(pk=row.pk).update(password=encrypted)


def decrypt_passwords(apps, schema_editor):
    model = apps.get_model("inbox", "SMTPSettings")
    database = schema_editor.connection.alias
    encryption = cipher()
    for row in model.objects.using(database).exclude(password="").iterator():
        plaintext = encryption.decrypt(row.password.removeprefix("fernet:").encode("ascii")).decode("utf-8")
        model.objects.using(database).filter(pk=row.pk).update(password=plaintext)


class Migration(migrations.Migration):
    dependencies = [("inbox", "0008_integrationsettings_webhooksettings_webhookdelivery")]
    operations = [
        migrations.RunPython(encrypt_passwords, decrypt_passwords),
        migrations.AlterField(
            model_name="smtpsettings",
            name="password",
            field=inbox.fields.EncryptedTextField(blank=True),
        ),
    ]
