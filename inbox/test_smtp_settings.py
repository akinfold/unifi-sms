import os
import smtplib
import ssl
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import ForwardDelivery, SMTPSettings
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

    def test_legacy_environment_does_not_disable_editing(self):
        with patch.dict(os.environ, {"SMTP_HOST": "ignored.example.test", "SMTP_PASSWORD": "environment-secret-marker"}):
            page = self.client.get("/settings/smtp/")
            self.assertFalse(page.context["form"].fields["host"].disabled)
            self.assertNotContains(page, "environment-secret-marker")
            self.assertNotContains(page, "ignored.example.test")
            self.assertRedirects(self.client.post("/settings/smtp/", self.valid_payload()), "/settings/smtp/")
        self.assertEqual(SMTPSettings.objects.get().host, "smtp.example.test")

    def test_legacy_environment_does_not_enable_forwarding(self):
        with patch.dict(os.environ, {"SMTP_ENABLED": "yes"}):
            page = self.client.get("/settings/smtp/")
        self.assertFalse(page.context["enabled_checked"])
        self.assertFalse(page.context["form"].errors)

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


    def test_email_uses_unsaved_values_without_enabling_or_saving_forwarding(self):
        for security in ("starttls", "tls", "none"):
            with self.subTest(security=security), patch("inbox.smtp_views.EmailBackend") as backend:
                connection = backend.return_value.__enter__.return_value
                connection.send_messages.return_value = 1
                payload = self.valid_payload(action="test-email", security=security)
                payload.pop("enabled")
                response = self.client.post("/settings/smtp/", payload)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["ok"])
                self.assertIn("Settings were not saved", response.json()["message"])
                backend.assert_called_once_with(
                    host="smtp.example.test", port=587,
                    username="mailer", password="secret-password",
                    use_tls=security == "starttls", use_ssl=security == "tls",
                    timeout=10, fail_silently=False,
                )
                emails = [call.args[0][0] for call in connection.send_messages.call_args_list]
                self.assertEqual([email.to for email in emails], [["one@example.test"], ["two@example.test"]])
                for email in emails:
                    self.assertEqual(email.subject, "UniFi SMS: SMTP test")
                    self.assertEqual(email.from_email, "archive@example.test")
                    self.assertIn("No SMS content is included", email.body)
                    self.assertNotIn("secret-password", email.body)
                self.assertFalse(SMTPSettings.objects.exists())
                self.assertFalse(ForwardDelivery.objects.exists())

    def test_email_keeps_saved_password_and_does_not_persist_replacements_or_removal(self):
        self.client.post("/settings/smtp/", self.valid_payload())
        saved_values = SMTPSettings.objects.values().get(pk=1)
        cases = (
            ({"password": "", "password_action": "keep"}, "mailer", "secret-password"),
            ({"password": "replacement-secret", "password_action": "replace"}, "mailer", "replacement-secret"),
            ({"password": "", "password_action": "remove"}, "", ""),
        )
        for changes, username, password in cases:
            with self.subTest(action=changes["password_action"]), patch("inbox.smtp_views.EmailBackend") as backend:
                backend.return_value.__enter__.return_value.send_messages.return_value = 1
                response = self.client.post(
                    "/settings/smtp/",
                    self.valid_payload(action="test-email", host="unsaved.example.test", **changes),
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(backend.call_args.kwargs["host"], "unsaved.example.test")
                self.assertEqual(backend.call_args.kwargs["username"], username)
                self.assertEqual(backend.call_args.kwargs["password"], password)
                self.assertEqual(SMTPSettings.objects.values().get(pk=1), saved_values)
                self.assertFalse(ForwardDelivery.objects.exists())

    def test_email_requires_complete_settings_even_when_forwarding_is_disabled(self):
        cases = (
            ({"host": ""}, "host"),
            ({"from_email": ""}, "from_email"),
            ({"recipients": ""}, "recipients"),
            ({"password": ""}, "password"),
            ({"username": ""}, "username"),
            ({"port": "0"}, "port"),
            ({"password": "", "password_action": "replace"}, "password"),
        )
        with patch("inbox.smtp_views.EmailBackend") as backend:
            for changes, field in cases:
                with self.subTest(field=field, changes=changes):
                    payload = self.valid_payload(action="test-email", **changes)
                    payload.pop("enabled")
                    response = self.client.post("/settings/smtp/", payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.json()["ok"])
                    self.assertIn(field, response.json()["errors"])
                    self.assertNotIn("secret-password", response.content.decode())
            backend.assert_not_called()
        self.assertFalse(SMTPSettings.objects.exists())

    def test_email_requires_administrator_access_and_csrf(self):
        payload = self.valid_payload(action="test-email")
        with patch("inbox.smtp_views.EmailBackend") as backend:
            self.client.logout()
            self.assertRedirects(self.client.post("/settings/smtp/", payload), "/login/")
            reader = User.objects.create_user("test-reader", password="safe-test-password")
            self.client.force_login(reader)
            self.assertRedirects(self.client.post("/settings/smtp/", payload), "/login/")
            csrf_client = Client(enforce_csrf_checks=True)
            csrf_client.force_login(self.admin)
            self.assertEqual(csrf_client.post("/settings/smtp/", payload).status_code, 403)
            backend.assert_not_called()
        self.assertFalse(SMTPSettings.objects.exists())

    def test_email_reports_delivery_errors_without_exposing_server_details(self):
        marker = "private-server-secret"
        cases = (
            (smtplib.SMTPAuthenticationError(535, marker.encode()), "sign-in failed"),
            (smtplib.SMTPRecipientsRefused({"one@example.test": (550, marker)}), "rejected a recipient"),
            (smtplib.SMTPSenderRefused(550, marker, "archive@example.test"), "rejected the sender"),
            (smtplib.SMTPNotSupportedError(marker), "does not support"),
            (ssl.SSLError(marker), "secure SMTP connection failed"),
            (TimeoutError(marker), "timed out"),
            (OSError(marker), "Could not connect"),
            (RuntimeError(marker), "could not be sent to every recipient"),
        )
        for error, message in cases:
            with self.subTest(error=type(error).__name__), patch("inbox.smtp_views.EmailBackend") as backend:
                backend.return_value.__enter__.side_effect = error
                response = self.client.post("/settings/smtp/", self.valid_payload(action="test-email"))
                self.assertEqual(response.status_code, 502)
                self.assertFalse(response.json()["ok"])
                self.assertIn(message, response.json()["message"])
                self.assertNotIn(marker, response.content.decode())
                self.assertNotIn("secret-password", response.content.decode())
        self.assertFalse(SMTPSettings.objects.exists())

    def test_email_does_not_report_success_if_a_recipient_message_is_not_sent(self):
        with patch("inbox.smtp_views.EmailBackend") as backend:
            connection = backend.return_value.__enter__.return_value
            connection.send_messages.side_effect = [1, 0]
            response = self.client.post("/settings/smtp/", self.valid_payload(action="test-email"))
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(connection.send_messages.call_count, 2)
        self.assertFalse(SMTPSettings.objects.exists())
        self.assertFalse(ForwardDelivery.objects.exists())
