import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings

from modem.tests.test_pdu import make_pdu, septets
from .models import Installation, Message, Receipt, WebhookDelivery, WebhookSettings
from .sync import sync_once
from .test_helpers import seed_connection
from .webhook_config import webhook_active
from .webhooks import deliver_webhooks


class WebhookImportCycleTests(TransactionTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings_override = override_settings(DATA_DIR=Path(self.temp.name))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

        self.admin = User.objects.create_superuser(
            "administrator", password="Example-test-password-94!"
        )
        self.client.force_login(self.admin)
        Installation.objects.create(pk=1, completed=True)
        seed_connection()

        self.requests = []
        test_case = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                test_case.requests.append((self.path, dict(self.headers.items()), body))
                self.send_response(204)
                self.end_headers()

            def log_message(self, format, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def test_import_queue_delivery_status_and_reread_deduplication(self):
        disabled_pdu = make_pdu(septets([79, 108, 100]), udl=3)
        pdu = make_pdu(septets([72, 105]), udl=2)
        modem = _FakeModem({0: disabled_pdu}, self)

        def factory(**kwargs):
            return modem

        self.assertFalse(WebhookSettings.objects.exists())
        self.assertFalse(webhook_active())
        modem.before_delete = lambda slot: (
            self.assertFalse(connection_in_atomic_block()),
            self.assertEqual(Message.objects.filter(text="Old").count(), 1),
            self.assertEqual(Receipt.objects.count(), 1),
            self.assertIsNone(Receipt.objects.get().cleared_at),
            self.assertFalse(WebhookDelivery.objects.exists()),
        )
        sync_once(factory)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertFalse(WebhookDelivery.objects.exists())

        WebhookSettings.objects.create(
            pk=1,
            enabled=True,
            url=f"http://127.0.0.1:{self.server.server_port}/hook",
            secret="synthetic-webhook-secret",
            secret_header="X-Test-Webhook-Key",
        )
        self.assertTrue(webhook_active())

        modem.pdus[1] = pdu
        modem.before_delete = lambda slot: (
            self.assertFalse(connection_in_atomic_block()),
            self.assertEqual(Message.objects.filter(text="Hi").count(), 1),
            self.assertEqual(Receipt.objects.count(), 2),
            self.assertEqual(Receipt.objects.filter(cleared_at__isnull=True).count(), 1),
            self.assertEqual(WebhookDelivery.objects.count(), 1),
        )
        sync_once(factory)

        message = Message.objects.get(text="Hi")
        receipt = Receipt.objects.get(message=message)
        delivery = WebhookDelivery.objects.get()
        self.assertEqual(message.text, "Hi")
        self.assertIsNotNone(receipt.cleared_at)
        self.assertEqual(delivery.message_id, message.pk)
        self.assertEqual(delivery.attempts, 0)
        self.assertEqual(modem.delete_calls, [0, 1])
        self.assertEqual(WebhookDelivery.objects.count(), 1)

        deliver_webhooks()

        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.sent_at)
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(len(self.requests), 1)
        path, headers, body = self.requests[0]
        self.assertEqual(path, "/hook")
        self.assertEqual(headers["X-Test-Webhook-Key"], "synthetic-webhook-secret")
        self.assertEqual(headers["Idempotency-Key"], str(delivery.token))
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(body), delivery.payload)
        self.assertEqual(delivery.payload["event"], "sms.received")
        self.assertEqual(delivery.payload["sms"]["id"], message.pk)
        self.assertEqual(delivery.payload["sms"]["text"], "Hi")

        listing = self.client.get("/api/messages/")
        detail = self.client.post(f"/api/messages/{message.pk}/")
        expected_status = {"state": "sent", "sent_at": delivery.sent_at.isoformat()}
        self.assertIs(listing.json()["webhook_enabled"], True)
        listed_message = next(row for row in listing.json()["messages"] if row["id"] == message.pk)
        self.assertEqual(listed_message["webhook"], expected_status)
        self.assertIs(detail.json()["webhook_enabled"], True)
        self.assertEqual(detail.json()["webhook"], expected_status)

        # Reimport the same device record to exercise receipt fingerprint deduplication.
        modem.pdus[0] = pdu
        sync_once(factory)
        self.assertEqual(Message.objects.count(), 2)
        self.assertEqual(Receipt.objects.count(), 2)
        self.assertEqual(WebhookDelivery.objects.count(), 1)
        self.assertEqual(modem.delete_calls, [0, 1, 0])

        settings = WebhookSettings.objects.get(pk=1)
        settings.enabled = False
        settings.save(update_fields=["enabled"])
        disabled = self.client.get("/api/messages/").json()
        self.assertIs(disabled["webhook_enabled"], False)
        # The UI uses webhook_enabled to hide this retained historical status.
        listed_message = next(row for row in disabled["messages"] if row["id"] == message.pk)
        self.assertEqual(listed_message["webhook"], expected_status)
        self.assertEqual(len(self.requests), 1)


class _FakeModem:
    def __init__(self, pdus, test_case):
        self.pdus = pdus
        self.test_case = test_case
        self.delete_calls = []
        self.before_delete = lambda slot: None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def list_pdus(self):
        return dict(self.pdus)

    def read_pdu(self, slot):
        return self.pdus[slot]

    def delete(self, slot):
        self.before_delete(slot)
        self.delete_calls.append(slot)
        del self.pdus[slot]


def connection_in_atomic_block():
    from django.db import connection

    return connection.in_atomic_block
