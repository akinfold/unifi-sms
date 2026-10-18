import os
import tempfile
import uuid
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .connection import decrypt_secret, get_connection
from .models import DeviceInfo, DeviceSettings, DeviceSetup
from .probe import ProbeError
from .test_helpers import TEST_HOST_KEY, seed_connection
from .trust import key_fingerprint

URL = "/settings/device/"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ConnectionWizardTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        override = override_settings(DATA_DIR=Path(temp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.admin = User.objects.create_superuser("administrator", password="synthetic-password")
        self.client.force_login(self.admin)
        self.probe = {"address": "10.20.30.40", "model": "U-LTE-Pro-EU", "name": "LTE device", "numbers": ["+15550102001"], "fingerprint": key_fingerprint(TEST_HOST_KEY), "key_type": TEST_HOST_KEY.get_name(), "host_key": f"{TEST_HOST_KEY.get_name()} {TEST_HOST_KEY.get_base64()}"}

    def address(self):
        return self.client.post(URL, {"action": "choose-address", "host": "device.example.invalid"})

    def access(self, **changes):
        data = {"action": "save-access", "port": "22", "username": "sms", "auth_method": "password", "key_source": "paste", "password": "synthetic-device-secret"}
        data.update(changes)
        return self.client.post(URL, data)

    def checked(self):
        self.address()
        self.access()
        with patch("inbox.connection_views.probe_connection", return_value=self.probe):
            response = self.client.post(URL, {"action": "check"})
        self.assertContains(response, self.probe["fingerprint"])
        return DeviceSetup.objects.get()

    def test_unconfigured_administrator_enters_wizard(self):
        self.assertRedirects(self.client.get("/"), URL)
        self.assertContains(self.client.get(URL), "Where is the device?")

    def test_wizard_and_discovery_require_administrator_and_csrf(self):
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post(URL, {"action": "start"}).status_code, 403)
        self.assertEqual(csrf.post(URL, {"action": "generate-key"}).status_code, 403)
        self.assertEqual(csrf.post("/api/device/discover/").status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post("/api/device/discover/").status_code, 403)
        self.assertRedirects(self.client.get(URL), "/login/")

    def test_address_and_credentials_are_draft_only(self):
        self.address()
        self.access()
        draft = DeviceSetup.objects.get()
        self.assertNotIn("synthetic-device-secret", draft.password)
        self.assertEqual(decrypt_secret(draft.password), "synthetic-device-secret")
        self.assertFalse(DeviceSettings.objects.exists())
        self.assertFalse(get_connection().configured)
        self.assertNotContains(self.client.get(URL), "synthetic-device-secret")

    def test_generated_private_key_is_encrypted_and_only_public_key_is_shown(self):
        self.address()
        response = self.client.post(URL, {"action": "generate-key"})
        self.assertEqual(response.status_code, 200)
        draft = DeviceSetup.objects.get()
        self.assertEqual(response.json(), {"public_key": draft.public_key})
        private = decrypt_secret(draft.private_key)
        self.assertNotEqual(private, draft.private_key)
        self.assertNotContains(response, "BEGIN RSA PRIVATE KEY")
        self.assertFalse(DeviceSettings.objects.exists())
        self.assertIn("BEGIN RSA PRIVATE KEY", private)
        self.assertTrue(draft.public_key.startswith("ssh-rsa "))
        response = self.client.get(URL)
        self.assertContains(response, draft.public_key)
        self.assertNotContains(response, "BEGIN RSA PRIVATE KEY")
        self.assertContains(response, "Device SSH Settings")

    def test_generated_key_requires_an_explicit_button_action(self):
        self.address()
        with patch("inbox.connection_views.generate_private_key") as generate:
            response = self.access(auth_method="private_key", key_source="generate", password="")
        self.assertContains(response, "Select Generate SSH key before continuing.")
        generate.assert_not_called()
        self.assertFalse(DeviceSetup.objects.get().private_key)

    def test_generated_key_is_reused_and_cannot_be_replaced_by_form_input(self):
        self.address()
        self.client.post(URL, {"action": "generate-key"})
        original = DeviceSetup.objects.get()
        with patch("inbox.connection_views.generate_private_key") as generate:
            response = self.client.post(URL, {"action": "generate-key"})
        generate.assert_not_called()
        self.assertEqual(response.json()["public_key"], original.public_key)
        self.assertEqual(DeviceSetup.objects.get().revision, original.revision)
        response = self.access(auth_method="private_key", key_source="generate", password="",
                               private_key="submitted-private-marker")
        self.assertEqual(response.status_code, 302)
        saved = DeviceSetup.objects.get()
        self.assertEqual(decrypt_secret(saved.private_key), decrypt_secret(original.private_key))
        self.assertEqual(saved.public_key, original.public_key)
        self.assertNotContains(self.client.get(URL), "submitted-private-marker")
        self.client.post(URL, {"action": "edit-access"})
        response = self.client.get(URL)
        self.assertContains(response, "SSH key generated")
        self.assertContains(response, original.public_key)

    def test_key_generation_invalidates_a_previous_connection_check(self):
        original = self.checked()
        self.client.post(URL, {"action": "generate-key"})
        draft = DeviceSetup.objects.get()
        self.assertNotEqual(draft.revision, original.revision)
        self.assertFalse(draft.probe)
        self.assertIsNone(draft.probe_id)
        self.assertIsNone(draft.checked_at)
        self.assertEqual(draft.auth_method, "private_key")
        self.assertFalse(draft.password)
        self.client.post(URL, {"action": "trust", "probe_id": str(original.probe_id)})
        self.assertFalse(DeviceSettings.objects.exists())

    def test_key_generation_does_not_expose_another_administrators_draft(self):
        self.address()
        self.client.post(URL, {"action": "generate-key"})
        original = DeviceSetup.objects.get()
        other = User.objects.create_superuser("other-administrator", password="synthetic-password")
        self.client.force_login(other)
        response = self.client.post(URL, {"action": "generate-key"})
        self.assertEqual(response.status_code, 409)
        self.assertNotContains(response, original.public_key, status_code=409)
        self.assertEqual(DeviceSetup.objects.get().private_key, original.private_key)

    def test_pasted_key_is_retained_without_rendering_secret(self):
        stream = StringIO()
        TEST_HOST_KEY.write_private_key(stream)
        self.address()
        self.access(auth_method="private_key", private_key=stream.getvalue(), password="")
        self.client.post(URL, {"action": "edit-access"})
        response = self.access(auth_method="private_key", private_key="", password="")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(decrypt_secret(DeviceSetup.objects.get().private_key), stream.getvalue().strip())
        self.assertNotContains(self.client.get(URL), "BEGIN RSA PRIVATE KEY")

    def test_invalid_secret_is_not_rendered_in_errors(self):
        self.address()
        response = self.access(port="99999", password="submitted-private-marker")
        self.assertNotContains(response, "submitted-private-marker")
        self.assertFalse(DeviceSettings.objects.exists())

    def test_check_does_not_activate_or_persist_host_key(self):
        self.checked()
        self.assertFalse(DeviceSettings.objects.exists())
        self.assertFalse((settings.DATA_DIR / "known_hosts").exists())

    def test_trust_persists_pin_and_activates_connection(self):
        draft = self.checked()
        response = self.client.post(URL, {"action": "trust", "probe_id": str(draft.probe_id)})
        self.assertRedirects(response, "/")
        self.assertTrue(get_connection().configured)
        self.assertEqual(DeviceSettings.objects.get().fingerprint, self.probe["fingerprint"])
        self.assertEqual((settings.DATA_DIR / "known_hosts").stat().st_mode & 0o777, 0o600)
        self.assertEqual(DeviceInfo.objects.get().numbers, self.probe["numbers"])
        self.assertFalse(DeviceSetup.objects.exists())

    def test_expired_or_forged_check_cannot_be_trusted(self):
        draft = self.checked()
        for token in (str(uuid.uuid4()), str(draft.probe_id)):
            if token == str(draft.probe_id):
                DeviceSetup.objects.update(checked_at=timezone.now() - timedelta(minutes=11))
            response = self.client.post(URL, {"action": "trust", "probe_id": token})
            self.assertContains(response, "expired or changed")
            self.assertFalse(DeviceSettings.objects.exists())
            self.assertFalse((settings.DATA_DIR / "known_hosts").exists())

    def test_changed_credentials_and_failed_check_invalidate_confirmation(self):
        draft = self.checked()
        self.access(password="changed-synthetic-secret")
        self.assertIsNone(DeviceSetup.objects.get().probe_id)
        with patch("inbox.connection_views.probe_connection", side_effect=ProbeError("private marker")):
            response = self.client.post(URL, {"action": "check"})
        self.assertNotContains(response, "private marker")
        self.assertIsNone(DeviceSetup.objects.get().probe_id)
        self.client.post(URL, {"action": "trust", "probe_id": str(draft.probe_id)})
        self.assertFalse(DeviceSettings.objects.exists())

    def test_database_failure_restores_previous_pin_and_active_connection(self):
        seed_connection()
        previous = (settings.DATA_DIR / "known_hosts").read_bytes()
        draft = self.checked()
        with patch("inbox.connection_views.DeviceInfo.objects.update_or_create", side_effect=DatabaseError("private error")):
            response = self.client.post(URL, {"action": "trust", "probe_id": str(draft.probe_id)})
        self.assertContains(response, "previous connection is unchanged")
        self.assertEqual((settings.DATA_DIR / "known_hosts").read_bytes(), previous)
        self.assertEqual(get_connection().host, "modem.example.invalid")
        self.assertTrue(DeviceSetup.objects.exists())

    def test_cancel_and_expiry_preserve_active_configuration(self):
        active = seed_connection()
        self.address()
        self.client.post(URL, {"action": "cancel"})
        self.assertFalse(DeviceSetup.objects.exists())
        self.assertEqual(DeviceSettings.objects.get().host, active.host)
        self.address()
        DeviceSetup.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.client.get(URL)
        self.assertFalse(DeviceSetup.objects.exists())

    def test_probe_finishing_after_credential_change_is_discarded(self):
        self.address()
        self.access()
        def result(values):
            DeviceSetup.objects.update(revision=uuid.uuid4(), password="fernet:changed")
            return self.probe
        with patch("inbox.connection_views.probe_connection", side_effect=result):
            self.client.post(URL, {"action": "check"})
        self.assertIsNone(DeviceSetup.objects.get().probe_id)

    def test_other_administrator_cannot_trust_a_draft(self):
        draft = self.checked()
        other = User.objects.create_superuser("other", password="synthetic-password")
        self.client.force_login(other)
        self.client.post(URL, {"action": "trust", "probe_id": str(draft.probe_id)})
        self.assertFalse(DeviceSettings.objects.exists())

    def test_discovery_returns_only_discovery_results_and_invalid_subnet_is_rejected(self):
        found = [{"address": "10.20.30.40", "model": "U-LTE-Pro-EU", "name": "LTE device", "mac": "00:11:22:33:44:55"}]
        with patch("inbox.connection_views.discover_devices", return_value=found):
            self.assertEqual(self.client.post("/api/device/discover/").json()["devices"], found)
        self.assertEqual(self.client.post("/api/device/discover/", {"network": "8.8.8.0/24"}).status_code, 400)

    def test_legacy_environment_does_not_lock_wizard(self):
        with patch.dict(os.environ, {"LTE_HOST": "ignored.example.invalid", "LTE_PASSWORD": "secret"}):
            response = self.address()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["form"].fields["username"].disabled)
