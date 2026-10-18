import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.db import OperationalError, connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from modem.client import ModemError
from modem.tests.test_pdu import make_pdu, septets
from .models import Installation, Message, Receipt, SyncStatus
from .test_helpers import seed_connection
from .sync import archive, sync_once

PASSWORD = "Example-test-password-94!"
PDU = make_pdu(septets([72, 105]), udl=2)


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AccessTests(TestCase):
    def setup_data(self, password=PASSWORD):
        return {
            "username": "administrator",
            "password1": password,
            "password2": password,
        }

    def test_first_run_accepts_eight_character_password_then_closes_wizard(self):
        self.assertRedirects(self.client.get("/"), "/setup/")
        page = self.client.get("/setup/")
        self.assertNotContains(page, "setup_token")
        self.assertContains(page, "at least 8 characters")
        self.assertRedirects(self.client.post("/setup/", self.setup_data("R8!k2pZx")), "/", target_status_code=302)
        self.assertTrue(User.objects.get().check_password("R8!k2pZx"))
        self.assertTrue(User.objects.get().is_superuser)
        self.assertTrue(Installation.objects.get(pk=1).completed)
        self.client.logout()
        self.assertRedirects(self.client.post("/setup/", self.setup_data()), "/login/")
        self.assertEqual(User.objects.count(), 1)

    def test_setup_rejects_password_shorter_than_eight_characters(self):
        response = self.client.post("/setup/", self.setup_data("R8!k2pZ"))
        self.assertContains(response, "at least 8 characters")
        self.assertFalse(User.objects.exists())

    def test_anonymous_and_non_staff_cannot_read_messages(self):
        for path in ("/api/messages/", "/api/status/"):
            self.assertEqual(self.client.get(path).status_code, 403)
        user = User.objects.create_user("reader", password=PASSWORD)
        self.client.force_login(user)
        self.assertEqual(self.client.get("/api/messages/").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/delete/", {"ids": [1]}, content_type="application/json"
            ).status_code,
            403,
        )

    def test_login_and_logout(self):
        User.objects.create_superuser("administrator", password=PASSWORD)
        self.assertRedirects(
            self.client.post("/login/", {"username": "administrator", "password": PASSWORD}), "/", target_status_code=302
        )
        self.assertEqual(self.client.get("/api/messages/").status_code, 200)
        self.assertEqual(self.client.get("/logout/").status_code, 405)
        self.client.post("/logout/")
        self.assertEqual(self.client.get("/api/messages/").status_code, 403)

    def test_csrf_protects_setup_and_deletion(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post("/setup/", self.setup_data()).status_code, 403)
        user = User.objects.create_superuser("administrator", password=PASSWORD)
        client.force_login(user)
        self.assertEqual(
            client.post("/api/delete/", {"ids": [1]}, content_type="application/json").status_code,
            403,
        )

    def test_setup_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.client.post("/setup/", {}).status_code, 200)
        self.assertEqual(self.client.post("/setup/", {}).status_code, 429)

    def test_read_search_and_selected_delete_preserve_tombstones(self):
        self.client.force_login(User.objects.create_superuser("administrator", password=PASSWORD))
        first = Message.objects.create(
            source="test", sender="Example", text="Hello", received_at=timezone.now()
        )
        second = Message.objects.create(
            source="test", sender="Another", text="Stay", received_at=timezone.now()
        )
        receipt = Receipt.objects.create(
            source="test", fingerprint="digest", message=first, pdu=PDU, slot=0
        )
        result = self.client.get("/api/messages/?q=Hello").json()
        self.assertEqual(result["count"], 1)
        self.assertFalse(result["messages"][0]["read"])
        self.assertEqual(self.client.get(f"/api/messages/{first.pk}/").status_code, 405)
        self.assertTrue(self.client.post(f"/api/messages/{first.pk}/").json()["read"])
        self.assertEqual(self.client.get("/api/messages/?unread=1").json()["count"], 1)
        payload = json.dumps({"ids": [first.pk]})
        self.assertEqual(
            self.client.post("/api/delete/", payload, content_type="application/json").status_code,
            409,
        )
        receipt.cleared_at = timezone.now()
        receipt.save()
        self.assertEqual(
            self.client.post("/api/delete/", payload, content_type="application/json").json()[
                "deleted"
            ],
            1,
        )
        receipt.refresh_from_db()
        self.assertIsNone(receipt.message_id)
        self.assertEqual(receipt.pdu, "")
        self.assertTrue(Message.objects.filter(pk=second.pk).exists())
        for ids in ([], [True], [0], "1", [1] * 101):
            self.assertEqual(
                self.client.post(
                    "/api/delete/", {"ids": ids}, content_type="application/json"
                ).status_code,
                400,
            )


@override_settings(LTE_HOST="modem.example.invalid", LTE_USERNAME="sms")
class SyncTests(TransactionTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"LTE_HOST": "modem.example.invalid"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings_override = override_settings(DATA_DIR=Path(self.temp.name))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        seed_connection()
        Installation.objects.create(pk=1, completed=True)
        self.modem = MagicMock()
        self.modem.__enter__.return_value = self.modem
        self.modem.__exit__.return_value = False
        self.modem.list_pdus.return_value = {0: PDU}
        self.modem.read_pdu.return_value = PDU
        self.factory = MagicMock(return_value=self.modem)

    def test_commit_and_verify_before_delete(self):
        def deleted(slot):
            self.assertEqual(slot, 0)
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(Message.objects.get().text, "Hi")
            self.assertIsNone(Receipt.objects.get().cleared_at)
            self.modem.read_pdu.assert_called_once_with(0)

        self.modem.delete.side_effect = deleted
        sync_once(self.factory)
        self.assertIsNotNone(Receipt.objects.get().cleared_at)
        self.assertEqual(SyncStatus.objects.get().state, "ok")

    def test_database_failure_never_deletes_slot(self):
        with patch("inbox.sync.Receipt.objects.create", side_effect=OperationalError("disk full")):
            sync_once(self.factory)
        self.modem.delete.assert_not_called()
        self.assertFalse(Message.objects.exists())
        self.assertFalse(Receipt.objects.exists())
        self.assertEqual(SyncStatus.objects.get().state, "error")

    def test_changed_slot_is_retained(self):
        self.modem.read_pdu.return_value = "different"
        sync_once(self.factory)
        self.modem.delete.assert_not_called()
        self.assertEqual(Message.objects.count(), 1)
        self.assertIsNone(Receipt.objects.get().cleared_at)
        self.assertEqual(SyncStatus.objects.get().state, "warning")

    def test_ambiguous_delete_is_retried_without_duplicate_archive(self):
        self.modem.delete.side_effect = ModemError("timeout")
        sync_once(self.factory)
        self.assertIsNone(Receipt.objects.get().cleared_at)
        self.modem.delete.side_effect = None
        sync_once(self.factory)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertIsNotNone(Receipt.objects.get().cleared_at)

    def test_crash_after_delete_reconciles_missing_slot(self):
        self.modem.delete.side_effect = ModemError("lost acknowledgement")
        sync_once(self.factory)
        self.modem.list_pdus.return_value = {}
        sync_once(self.factory)
        self.assertIsNotNone(Receipt.objects.get().cleared_at)
        self.assertEqual(Message.objects.get().text, "Hi")

    def test_unknown_encoding_remains_on_device(self):
        self.modem.list_pdus.return_value = {0: make_pdu(b"x", dcs=4)}
        sync_once(self.factory)
        self.modem.delete.assert_not_called()
        self.assertFalse(Message.objects.exists())
        self.assertEqual(SyncStatus.objects.get().state, "warning")

    def test_multipart_out_of_order_survives_repeated_import(self):
        first = make_pdu(
            bytes.fromhex("050003AB0201") + "Hello ".encode("utf-16-be"), dcs=8, first=0x40
        )
        second = make_pdu(
            bytes.fromhex("050003AB0202") + "world".encode("utf-16-be"), dcs=8, first=0x40
        )
        archive("test", 2, second)
        self.assertEqual(Message.objects.get().saved_parts, 1)
        archive("test", 0, first)
        archive("test", 7, second)
        self.assertEqual(Message.objects.get().text, "Hello world")
        self.assertEqual(Message.objects.get().saved_parts, 2)
        self.assertEqual(Receipt.objects.count(), 2)

    def test_deleted_archive_does_not_reappear(self):
        sync_once(self.factory)
        Receipt.objects.update(pdu="")
        Message.objects.all().delete()
        sync_once(self.factory)
        self.assertFalse(Message.objects.exists())
        self.assertEqual(Receipt.objects.count(), 1)

    def test_import_waits_for_first_run_wizard(self):
        Installation.objects.all().delete()
        sync_once(self.factory)
        self.factory.assert_not_called()
        self.assertEqual(SyncStatus.objects.get().state, "waiting")
