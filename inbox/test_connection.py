import os
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase, TransactionTestCase, override_settings
from modem.tests.test_pdu import make_pdu, septets

from .connection import connection_values, decrypt_secret, get_connection
from .device import configured, phone_status
from .models import DeviceSettings, Installation, Receipt
from .sync import sync_once
from .test_helpers import seed_connection


class ConnectionSettingsTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        override = override_settings(DATA_DIR=Path(temp.name))
        override.enable()
        self.addCleanup(override.disable)

    def test_secrets_are_encrypted_and_unknown_device_is_inactive(self):
        stored = DeviceSettings.objects.create(pk=1, host="device.example.invalid", username="sms", auth_method="password", password="example-password")
        stored.refresh_from_db()
        self.assertNotIn("example-password", stored.password)
        self.assertEqual(decrypt_secret(stored.password), "example-password")
        self.assertFalse(get_connection().configured)

    def test_legacy_environment_is_ignored(self):
        seed_connection()
        with patch.dict(os.environ, {"LTE_HOST": "ignored.example.invalid", "LTE_PORT": "bad", "LTE_PASSWORD": ""}):
            values, keys = connection_values()
            self.assertEqual(keys, ())
            self.assertEqual(values["host"], "modem.example.invalid")
            self.assertTrue(configured())

    def test_invalid_ciphertext_has_safe_error(self):
        seed_connection()
        DeviceSettings.objects.update(password="bad-ciphertext")
        with self.assertRaises(ValueError) as error:
            connection_values()
        self.assertNotIn("bad-ciphertext", str(error.exception))
        self.assertEqual(phone_status()["state"], "error")

    def test_missing_or_changed_known_hosts_stops_import(self):
        seed_connection()
        from django.conf import settings
        (settings.DATA_DIR / "known_hosts").write_text("")
        self.assertFalse(configured())
        (settings.DATA_DIR / "known_hosts").unlink()
        self.assertFalse(configured())


class ConnectionSnapshotTests(TransactionTestCase):
    def test_sync_uses_one_snapshot_for_transport_and_source(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(DATA_DIR=Path(directory)):
            Installation.objects.create(pk=1, completed=True)
            saved = seed_connection("first.example.invalid")
            modem = MagicMock()
            modem.__enter__.return_value = modem
            modem.__exit__.return_value = False
            pdu = make_pdu(septets([72, 105]), udl=2)
            modem.list_pdus.return_value = {0: pdu}
            modem.read_pdu.return_value = pdu
            def change_config(**kwargs):
                saved.host = "second.example.invalid"
                saved.save(update_fields=["host"])
                return modem
            factory = MagicMock(side_effect=change_config)
            sync_once(factory)
            self.assertEqual(factory.call_args.kwargs["host"], "first.example.invalid")
            source = hashlib.sha256(b"first.example.invalid:22:/dev/ttyUSB2:SM").hexdigest()
            self.assertEqual(Receipt.objects.get().source, source)
