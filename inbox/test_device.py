import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .device import device_source, phone_status, request_phone_refresh
from .models import DeviceInfo, Installation, Message
from .sync import importer_lock, refresh_phone_once, sync_once


@override_settings(
    LTE_HOST="modem.example.invalid",
    LTE_USERNAME="sms",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class PhoneNumberTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.override = override_settings(DATA_DIR=Path(self.temp.name))
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.admin = User.objects.create_superuser("administrator", password="Test-password-94!")
        self.client.force_login(self.admin)
        self.modem = MagicMock()
        self.modem.__enter__.return_value = self.modem
        self.modem.__exit__.return_value = False
        self.modem.phone_numbers.return_value = ["+15550102001"]
        self.modem.list_pdus.return_value = {}
        self.factory = MagicMock(return_value=self.modem)

    def test_first_check_is_cached_until_requested_or_one_hour_passes(self):
        refresh_phone_once(self.factory)
        first = phone_status()
        self.assertEqual(first["numbers"], ["+15550102001"])
        self.assertEqual(first["state"], "ok")
        self.assertIsNotNone(first["checked_at"])
        refresh_phone_once(self.factory)
        self.modem.phone_numbers.assert_called_once_with()
        DeviceInfo.objects.update(attempted_at=timezone.now() - timedelta(hours=2))
        refresh_phone_once(self.factory)
        self.assertEqual(self.modem.phone_numbers.call_count, 2)
        self.modem.list_pdus.assert_not_called()
        self.modem.delete.assert_not_called()
        self.assertFalse(Message.objects.exists())

    def test_refresh_endpoint_queues_work_and_updates_changed_number(self):
        refresh_phone_once(self.factory)
        response = self.client.post("/api/phone/refresh/")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["state"], "queued")
        self.modem.phone_numbers.assert_called_once_with()
        self.modem.phone_numbers.return_value = ["+15550102002"]
        refresh_phone_once(self.factory)
        response = self.client.get("/api/status/")
        self.assertEqual(response.json()["phone"]["numbers"], ["+15550102002"])
        self.assertEqual(response.json()["phone"]["state"], "ok")

    def test_failure_preserves_last_number_and_does_not_expose_transport_output(self):
        refresh_phone_once(self.factory)
        checked = phone_status()["checked_at"]
        request_phone_refresh()
        self.modem.phone_numbers.side_effect = RuntimeError("private transport output")
        refresh_phone_once(self.factory)
        result = self.client.get("/api/status/").json()["phone"]
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["numbers"], ["+15550102001"])
        self.assertEqual(result["checked_at"], checked)
        self.assertNotIn("private", result["error"])
        sync_once(self.factory)
        self.modem.list_pdus.assert_called_once_with()

    def test_successful_empty_response_clears_previous_number(self):
        refresh_phone_once(self.factory)
        request_phone_refresh()
        self.modem.phone_numbers.return_value = []
        refresh_phone_once(self.factory)
        self.assertEqual(phone_status()["state"], "empty")
        self.assertEqual(phone_status()["numbers"], [])

    def test_failed_connection_without_cache(self):
        self.factory.side_effect = TimeoutError("private output")
        refresh_phone_once(self.factory)
        self.assertEqual(phone_status()["state"], "error")
        self.assertEqual(phone_status()["numbers"], [])
        self.assertIsNone(phone_status()["checked_at"])

    def test_other_device_does_not_reuse_cached_number(self):
        refresh_phone_once(self.factory)
        with override_settings(LTE_HOST="other.example.invalid"):
            self.assertEqual(phone_status()["numbers"], [])
            self.assertIsNone(phone_status()["checked_at"])

    def test_refresh_uses_importer_lock(self):
        with importer_lock() as acquired:
            self.assertTrue(acquired)
            refresh_phone_once(self.factory)
        self.factory.assert_not_called()
        refresh_phone_once(self.factory)
        self.modem.phone_numbers.assert_called_once_with()

    def test_recovery_after_worker_interruption_and_stale_queue(self):
        old = timezone.now() - timedelta(minutes=10)
        DeviceInfo.objects.create(source=device_source(), attempted_at=old)
        self.assertEqual(phone_status()["state"], "error")
        refresh_phone_once(self.factory)
        self.assertEqual(phone_status()["state"], "ok")
        DeviceInfo.objects.update(attempted_at=old, requested_at=old + timedelta(seconds=1))
        self.assertEqual(phone_status()["state"], "error")
        refresh_phone_once(self.factory)
        self.assertEqual(phone_status()["state"], "ok")

    def test_first_run_and_missing_connection_do_not_contact_device(self):
        User.objects.all().delete()
        refresh_phone_once(self.factory)
        self.factory.assert_not_called()
        Installation.objects.create(pk=1, completed=True)
        with override_settings(LTE_HOST=""):
            refresh_phone_once(self.factory)
            self.factory.assert_not_called()
            self.assertEqual(phone_status()["state"], "unconfigured")

    def test_refresh_requires_admin_post_and_csrf(self):
        self.assertEqual(self.client.get("/api/phone/refresh/").status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.post("/api/phone/refresh/").status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post("/api/phone/refresh/").status_code, 403)
        self.client.force_login(User.objects.create_user("reader"))
        self.assertEqual(self.client.post("/api/phone/refresh/").status_code, 403)
        self.assertEqual(self.client.get("/api/status/").status_code, 403)
        self.assertFalse(DeviceInfo.objects.exists())

    def test_unconfigured_connection_rejects_refresh(self):
        with override_settings(LTE_HOST=""):
            self.assertEqual(self.client.post("/api/phone/refresh/").status_code, 409)
        self.assertFalse(DeviceInfo.objects.exists())

    def test_request_during_refresh_is_coalesced(self):
        def read_number():
            self.assertEqual(phone_status()["state"], "refreshing")
            request_phone_refresh()
            return ["+15550102001"]

        self.modem.phone_numbers.side_effect = read_number
        refresh_phone_once(self.factory)
        refresh_phone_once(self.factory)
        self.modem.phone_numbers.assert_called_once_with()
        self.assertEqual(phone_status()["state"], "ok")
