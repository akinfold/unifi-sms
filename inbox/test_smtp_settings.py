import os
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import SMTPSettings
from .smtp_forms import SMTPSettingsForm


class SMTPSettingsTests(TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        for key in tuple(os.environ):
            if key.startswith("SMTP_"):
                os.environ.pop(key)
        self.admin = User.objects.create_superuser("admin", "admin@example.test", "safe-test-password")
        self.client.force_login(self.admin)

    def valid_payload(self, **overrides):
        data = {
            "enabled": "on",
            "host": "smtp.example.test",
            "port": "587",
            "username": "mailer",
            "password": "secret-password",
            "security": "starttls",
            "from_email": "archive@example.test",
            "recipients": "one@example.test, two@example.test",
        }
        data.update(overrides)
        return data

    def test_administrator_access_and_csrf(self):
        self.client.logout()
        self.assertRedirects(self.client.get("/settings/smtp/"), "/login/")
        reader = User.objects.create_user("reader", password="safe-test-password")
        self.client.force_login(reader)
        self.assertRedirects(self.client.get("/settings/smtp/"), "/login/")
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(
            csrf_client.post("/settings/smtp/", self.valid_payload()).status_code,
            403,
        )
        self.assertFalse(SMTPSettings.objects.exists())

    def test_database_settings_save_and_password_retention_and_clear(self):
        response = self.client.post("/settings/smtp/", self.valid_payload())
        self.assertRedirects(response, "/settings/smtp/")
        saved = SMTPSettings.objects.get(pk=1)
        self.assertTrue(saved.enabled)
        self.assertEqual(saved.port, 587)
        self.assertEqual(saved.recipients, "one@example.test, two@example.test")
        self.assertEqual(saved.password, "secret-password")

        self.client.post(
            "/settings/smtp/",
            self.valid_payload(password="", recipients="one@example.test"),
        )
        saved.refresh_from_db()
        self.assertEqual(saved.password, "secret-password")

        self.client.post(
            "/settings/smtp/",
            self.valid_payload(
                password="", password_action="remove", recipients="one@example.test"
            ),
        )
        saved.refresh_from_db()
        self.assertEqual(saved.password, "")
        self.assertEqual(saved.username, "")

    def test_password_is_never_rendered_in_page_or_errors(self):
        SMTPSettings.objects.create(
            pk=1,
            enabled=False,
            host="smtp.example.test",
            username="mailer",
            password="stored-secret-marker",
            from_email="archive@example.test",
        )
        page = self.client.get("/settings/smtp/")
        self.assertNotContains(page, "stored-secret-marker")
        self.assertContains(page, "Password saved")
        self.assertContains(page, "Replace password")
        self.assertNotContains(page, "Clear saved password")
        response = self.client.post(
            "/settings/smtp/",
            self.valid_payload(password="submitted-secret-marker", port="99999"),
        )
        self.assertNotContains(response, "submitted-secret-marker")
        self.assertNotContains(response, "stored-secret-marker")
        self.assertContains(response, 'id="id_password_action" value="replace"')
        self.assertContains(response, "Enter the new password again")

    def test_environment_settings_disable_editing_and_show_invalid_configuration(self):
        os.environ["SMTP_ENABLED"] = "perhaps"
        os.environ["SMTP_HOST"] = "smtp.env.example.test"
        os.environ["SMTP_PORT"] = "2468"
        os.environ["SMTP_PASSWORD"] = "environment-secret-marker"
        page = self.client.get("/settings/smtp/")
        self.assertContains(page, "SMTP settings are managed by environment variables")
        self.assertContains(page, "SMTP_ENABLED")
        self.assertContains(page, "smtp.env.example.test")
        self.assertContains(page, 'value="2468"')
        self.assertTrue(page.context["form"].fields["host"].disabled)
        self.assertContains(page, "Enter true, false, yes, no, on, or off.")
        self.assertNotContains(page, "environment-secret-marker")
        self.assertContains(page, "Provided by environment")
        self.assertNotContains(page, 'id="remove-credentials"')
        self.assertNotContains(page, 'id="password-editor"')
        self.assertContains(page, "disabled")
        self.assertEqual(
            self.client.post("/settings/smtp/", self.valid_payload()).status_code,
            403,
        )
        self.assertFalse(SMTPSettings.objects.exists())

    def test_environment_true_with_missing_required_fields_is_reported(self):
        os.environ["SMTP_ENABLED"] = "yes"
        page = self.client.get("/settings/smtp/")
        self.assertContains(page, "SMTP host is required when email forwarding is enabled.")
        self.assertContains(page, "From address is required when email forwarding is enabled.")

    def test_form_rejects_bad_boolean_port_addresses_and_header_newlines(self):
        cases = (
            {"enabled": "maybe"},
            {"enabled": ""},
            {"port": "0"},
            {"port": "65536"},
            {"from_email": "Display Name <person@example.test>"},
            {"recipients": "valid@example.test, not-an-address"},
            {"host": "smtp.example.test\r\nBcc: victim@example.test"},
            {"username": "mailer\nInjected: yes"},
        )
        for change in cases:
            with self.subTest(change=change):
                form = SMTPSettingsForm(self.valid_payload(**change))
                self.assertFalse(form.is_valid())

    def test_replacement_requires_new_password_and_cancel_keeps_saved_credentials(self):
        self.client.post("/settings/smtp/", self.valid_payload())
        response = self.client.post(
            "/settings/smtp/", self.valid_payload(password="", password_action="replace")
        )
        self.assertContains(response, "Enter the new password, or cancel the replacement.")
        self.assertEqual(SMTPSettings.objects.get(pk=1).password, "secret-password")
        response = self.client.post(
            "/settings/smtp/", self.valid_payload(password="", password_action="keep")
        )
        self.assertRedirects(response, "/settings/smtp/")
        self.assertEqual(SMTPSettings.objects.get(pk=1).password, "secret-password")
        response = self.client.post(
            "/settings/smtp/",
            self.valid_payload(password="replacement-secret", password_action="replace"),
        )
        self.assertRedirects(response, "/settings/smtp/")
        self.assertEqual(SMTPSettings.objects.get(pk=1).password, "replacement-secret")

    def test_removal_waits_for_valid_save_and_can_be_undone_after_error(self):
        self.client.post("/settings/smtp/", self.valid_payload())
        response = self.client.post(
            "/settings/smtp/",
            self.valid_payload(password="", password_action="remove", port="0"),
        )
        self.assertContains(response, "Username and password will be removed when you save.")
        self.assertContains(response, 'id="id_password_action" value="remove"')
        self.assertEqual(SMTPSettings.objects.get(pk=1).password, "secret-password")
        response = self.client.post(
            "/settings/smtp/", self.valid_payload(password="", password_action="keep")
        )
        self.assertRedirects(response, "/settings/smtp/")
        self.assertEqual(SMTPSettings.objects.get(pk=1).username, "mailer")
        self.assertEqual(SMTPSettings.objects.get(pk=1).password, "secret-password")

    def test_empty_password_state_is_explicit(self):
        page = self.client.get("/settings/smtp/")
        self.assertContains(page, "No password saved")
        self.assertContains(page, '<label for="id_password">Password:</label>')
        self.assertNotContains(page, 'id="remove-credentials"')
