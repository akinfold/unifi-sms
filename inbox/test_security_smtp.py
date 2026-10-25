from django.contrib.auth.hashers import check_password, make_password
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .connection import decrypt_secret
from .models import SMTPSettings
from .smtp_config import smtp_values


def raw_password(pk=1):
    with connection.cursor() as cursor:
        cursor.execute("SELECT password FROM inbox_smtpsettings WHERE id = %s", [pk])
        return cursor.fetchone()[0]


class SMTPEncryptionTests(TestCase):
    def test_value_based_orm_writes_encrypt_and_preserve_literal_prefixes(self):
        password = "fernet:synthetic-password-☀"
        config = SMTPSettings.objects.create(password=password)
        self.assertNotEqual(raw_password(), password)
        self.assertEqual(decrypt_secret(raw_password()), password)
        self.assertEqual(smtp_values()[0]["password"], password)
        config.refresh_from_db()
        config.host = "smtp.example.invalid"
        config.save(update_fields=["host"])
        self.assertEqual(decrypt_secret(raw_password()), password)
        config.save()
        self.assertEqual(SMTPSettings.objects.get().password, password)
        SMTPSettings.objects.filter(pk=1).update(password="replacement-secret")
        self.assertEqual(decrypt_secret(raw_password()), "replacement-secret")
        config = SMTPSettings.objects.get()
        config.password = "bulk-replacement"
        SMTPSettings.objects.bulk_update([config], ["password"])
        self.assertEqual(decrypt_secret(raw_password()), "bulk-replacement")

    def test_blank_password_and_removal_are_supported(self):
        SMTPSettings.objects.bulk_create([SMTPSettings(id=1, password="")])
        self.assertEqual(raw_password(), "")
        SMTPSettings.objects.update(password="temporary-secret")
        SMTPSettings.objects.update(password="")
        self.assertEqual(SMTPSettings.objects.get().password, "")

    def test_wrong_encryption_key_fails_closed_without_exposing_the_secret(self):
        SMTPSettings.objects.create(password="synthetic-password")
        with override_settings(SECRET_KEY="different-synthetic-key"):
            with self.assertRaisesMessage(ValueError, "Stored connection secret is invalid"):
                smtp_values()


class SMTPEncryptionMigrationTests(TransactionTestCase):
    migrate_from = ("inbox", "0008_integrationsettings_webhooksettings_webhookdelivery")
    migrate_to = ("inbox", "0009_encrypt_smtp_password")

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        self.old_apps = executor.loader.project_state([self.migrate_from]).apps
        self.addCleanup(self.restore_schema)

    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_upgrade_preserves_admin_settings_messages_and_integration_keys(self):
        user = self.old_apps.get_model("auth", "User")
        password_hash = make_password("Synthetic-admin-password-42")
        user.objects.create(username="migration-admin", password=password_hash, is_staff=True, is_superuser=True)
        installation = self.old_apps.get_model("inbox", "Installation")
        installation.objects.create(completed=True)
        smtp = self.old_apps.get_model("inbox", "SMTPSettings")
        smtp.objects.create(enabled=True, host="smtp.example.invalid", port=465, username="relay-user",
                            password="fernet:literal-old-password", security="tls",
                            from_email="sms@example.invalid", recipients="reader@example.invalid")
        smtp.objects.create(id=2, password="")
        integration = self.old_apps.get_model("inbox", "IntegrationSettings")
        integration.objects.create(rest_enabled=True, rest_key_hash=make_password("a" * 32),
                                   rest_key_suffix="aaaa", mcp_enabled=True, mcp_key_hash=make_password("b" * 32),
                                   mcp_key_suffix="bbbb")
        device = self.old_apps.get_model("inbox", "DeviceSettings")
        device.objects.create(host="device.example.invalid", username="device-user", password="fernet:existing-device-ciphertext")
        message = self.old_apps.get_model("inbox", "Message")
        sms = message.objects.create(source="migration-fixture", sender="+15550000000", received_at=timezone.now(),
                                     text="Synthetic upgrade SMS ☀")
        receipt = self.old_apps.get_model("inbox", "Receipt")
        receipt.objects.create(fingerprint="a" * 64, source=sms.source, message_id=sms.pk, pdu="synthetic-pdu", slot=1)
        models = [user, installation, integration, device, message, receipt]
        before = {model._meta.label: list(model.objects.values()) for model in models}
        smtp_before = list(smtp.objects.values())
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        for old_model in models:
            current = current_apps.get_model(old_model._meta.app_label, old_model._meta.model_name)
            self.assertEqual(list(current.objects.values()), before[old_model._meta.label])
        self.assertEqual(list(SMTPSettings.objects.order_by("pk").values()), smtp_before)
        self.assertEqual(decrypt_secret(raw_password()), "fernet:literal-old-password")
        self.assertEqual(raw_password(2), "")
        self.assertTrue(check_password("Synthetic-admin-password-42", user.objects.get().password))
        self.assertTrue(smtp_values()[0]["enabled"])
