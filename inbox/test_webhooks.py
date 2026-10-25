import json
import tempfile
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from django.forms import Select
from django.test import TestCase, override_settings
from django.utils import timezone

from .connection import decrypt_secret, is_encrypted_secret
from .models import Message, WebhookDelivery, WebhookSettings
from .webhook_config import webhook_active, webhook_values
from .webhook_forms import WebhookSettingsForm
from .webhooks import deliver_webhooks, queue_webhook


class WebhookTests(TestCase):
    def setUp(self):
        self.data_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(DATA_DIR=Path(self.data_dir.name))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.data_dir.cleanup)
        self.requests = []
        self.statuses = []
        self.redirect_target = None

        test_case = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                test_case.requests.append((self.path, dict(self.headers.items()), request_body))
                status = test_case.statuses.pop(0) if test_case.statuses else 200
                if status == 302:
                    self.send_response(302)
                    self.send_header("Location", test_case.redirect_target)
                    self.end_headers()
                    return
                self.send_response(status)
                self.end_headers()

            def log_message(self, format, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self._stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def make_message(self, **changes):
        values = {
            "source": "modem-1",
            "sender": "+15551234567",
            "received_at": timezone.now(),
            "text": "Hello, мир",
            "total_parts": 1,
            "saved_parts": 1,
        }
        values.update(changes)
        return Message.objects.create(**values)

    def enable_webhook(self, **changes):
        values = {"enabled": True, "url": self.url}
        values.update(changes)
        return WebhookSettings.objects.create(pk=1, **values)

    def test_defaults_disabled_and_settings_expose_plaintext_secret(self):
        self.assertFalse(webhook_active())
        self.assertEqual(
            webhook_values(),
            {"enabled": False, "url": "", "secret": "", "secret_header": "X-Webhook-Secret"},
        )
        saved = self.enable_webhook(secret="saved-secret")
        saved.refresh_from_db()
        self.assertTrue(is_encrypted_secret(saved.secret))
        self.assertNotEqual(saved.secret, "saved-secret")
        self.assertEqual(decrypt_secret(saved.secret), "saved-secret")
        self.assertEqual(webhook_values()["secret"], "saved-secret")
        self.assertTrue(webhook_active())

    def test_form_validates_url_secret_header_and_strict_boolean(self):
        base = {"enabled": "true", "url": "https://127.0.0.1/hook"}
        for value in (
            "maybe",
            "",
        ):
            with self.subTest(enabled=value):
                self.assertFalse(WebhookSettingsForm(base | {"enabled": value}).is_valid())
        for url in (
            "ftp://example.test/hook",
            "https://user:pass@example.test/hook",
            "https://example.test/hook#fragment",
            "https://example.test/hook#",
            "https://example.test:0/hook",
            "not a URL",
        ):
            with self.subTest(url=url):
                form = WebhookSettingsForm(base | {"url": url})
                self.assertFalse(form.is_valid())
        self.assertFalse(WebhookSettingsForm(base | {"url": ""}).is_valid())
        for header in ("Content-Type", "Host", "Idempotency-Key", "Bad Header", "X-Secret\nX-Evil"):
            with self.subTest(header=header):
                form = WebhookSettingsForm(
                    base | {"secret": "secret", "secret_header": header}
                )
                self.assertFalse(form.is_valid())
        form = WebhookSettingsForm(
            base | {"secret": "secret", "secret_header": "Authorization"}
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["secret"], "secret")

    def test_secret_actions_keep_replace_remove_and_reject_invalid_replacement(self):
        self.assertIsInstance(WebhookSettingsForm().fields["secret_action"].widget, Select)
        common = {"enabled": "on", "url": self.url, "secret_header": "X-Webhook-Secret"}
        form = WebhookSettingsForm(common | {"secret": "", "secret_action": "keep"}, stored_secret="old")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["secret"], "old")
        form = WebhookSettingsForm(common | {"secret": "new", "secret_action": "replace"}, stored_secret="old")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["secret"], "new")
        form = WebhookSettingsForm(common | {"secret": "", "secret_action": "remove"}, stored_secret="old")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["secret"], "")
        form = WebhookSettingsForm(common | {"secret": "", "secret_action": "replace"}, stored_secret="old")
        self.assertFalse(form.is_valid())
        form = WebhookSettingsForm(common | {"secret": "bad\r\nvalue"})
        self.assertFalse(form.is_valid())
        form = WebhookSettingsForm(common | {"secret": "emoji-🔒"})
        self.assertFalse(form.is_valid())

    def test_queue_only_complete_messages_when_enabled_and_is_idempotent(self):
        incomplete = self.make_message(total_parts=2, saved_parts=1)
        self.assertIsNone(queue_webhook(incomplete))
        complete = self.make_message()
        self.assertIsNone(queue_webhook(complete))
        self.assertFalse(WebhookDelivery.objects.exists())

        self.enable_webhook()
        delivery = queue_webhook(complete)
        self.assertIsNotNone(delivery)
        self.assertEqual(queue_webhook(complete).pk, delivery.pk)
        self.assertEqual(WebhookDelivery.objects.count(), 1)
        self.assertEqual(delivery.payload["event"], "sms.received")
        self.assertEqual(delivery.payload["event_id"], str(delivery.token))
        self.assertEqual(delivery.payload["sms"]["text"], "Hello, мир")

    def test_success_sends_payload_optional_secret_and_idempotency_key(self):
        self.enable_webhook(secret="delivery-secret", secret_header="Authorization")
        message = self.make_message()
        delivery = queue_webhook(message)
        deliver_webhooks()

        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.sent_at)
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(delivery.last_error, "")
        path, headers, body = self.requests[0]
        self.assertEqual(path, "/hook")
        self.assertEqual(headers["Authorization"], "delivery-secret")
        self.assertEqual(headers["Idempotency-Key"], str(delivery.token))
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(body), delivery.payload)
        deliver_webhooks()
        self.assertEqual(len(self.requests), 1)

    def test_request_omits_secret_header_when_no_secret_is_configured(self):
        self.enable_webhook()
        delivery = queue_webhook(self.make_message())
        deliver_webhooks()
        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.sent_at)
        headers = self.requests[0][1]
        self.assertNotIn("X-Webhook-Secret", headers)
        self.assertEqual(headers["Idempotency-Key"], str(delivery.token))

    def test_retry_preserves_payload_and_idempotency_key(self):
        self.enable_webhook()
        delivery = queue_webhook(self.make_message())
        original_payload = delivery.payload
        self.statuses = [503, 200]
        deliver_webhooks()
        delivery.refresh_from_db()
        self.assertIsNone(delivery.sent_at)
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(delivery.last_error, "Webhook returned HTTP 503. A retry is scheduled.")
        self.assertGreater(delivery.next_attempt_at, timezone.now())

        WebhookDelivery.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        deliver_webhooks()
        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.sent_at)
        self.assertEqual(delivery.attempts, 2)
        self.assertEqual(delivery.payload, original_payload)
        self.assertEqual(self.requests[0][1]["Idempotency-Key"], self.requests[1][1]["Idempotency-Key"])
        self.assertEqual(json.loads(self.requests[1][2]), original_payload)

    def test_redirect_does_not_forward_secret_to_another_host(self):
        target_requests = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_POST(inner_self):
                target_requests.append(dict(inner_self.headers.items()))
                inner_self.send_response(200)
                inner_self.end_headers()

            def log_message(self, format, *args):
                return

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        self.addCleanup(target.shutdown)
        self.addCleanup(target.server_close)
        self.addCleanup(target_thread.join, 2)
        self.redirect_target = f"http://127.0.0.1:{target.server_port}/collect"
        self.statuses = [302]
        self.enable_webhook(secret="do-not-forward")
        delivery = queue_webhook(self.make_message())
        deliver_webhooks()
        delivery.refresh_from_db()
        self.assertIsNone(delivery.sent_at)
        self.assertEqual(delivery.last_error, "Webhook returned HTTP 302. A retry is scheduled.")
        self.assertEqual(target_requests, [])

    def test_disabled_or_invalid_configuration_pauses_pending_delivery(self):
        saved = self.enable_webhook()
        delivery = queue_webhook(self.make_message())
        saved.enabled = False
        saved.save()
        deliver_webhooks()
        delivery.refresh_from_db()
        self.assertEqual(delivery.attempts, 0)
        self.assertFalse(self.requests)

        saved.enabled = True
        saved.url = "file:///tmp/hook"
        saved.save()
        deliver_webhooks()
        delivery.refresh_from_db()
        self.assertEqual(delivery.attempts, 0)
        self.assertFalse(self.requests)

    def test_corrupt_or_unencrypted_saved_secret_fails_closed_and_pauses_worker(self):
        saved = self.enable_webhook(secret="saved-secret")
        delivery = queue_webhook(self.make_message())
        for bad_secret in ("plaintext-secret", "fernet:invalid-ciphertext"):
            with self.subTest(secret=bad_secret):
                WebhookSettings.objects.filter(pk=saved.pk).update(secret=bad_secret)
                with self.assertRaises(ValueError):
                    webhook_values()
                self.assertFalse(webhook_active())
                deliver_webhooks()
                delivery.refresh_from_db()
                self.assertEqual(delivery.attempts, 0)
                self.assertFalse(self.requests)

