import os
import socketserver
import tempfile
import threading
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from modem.tests.test_pdu import make_pdu, septets
from .forwarding import forward_pending, forwarding_lock
from .models import ForwardDelivery, Installation, Message, Receipt, SMTPSettings
from .smtp_config import smtp_values
from .test_helpers import seed_connection
from .sync import archive, sync_once

PDU = make_pdu(septets([72, 105]), udl=2)


class ForwardingTests(TransactionTestCase):
    def setUp(self):
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("SMTP_")
        }
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        override = override_settings(DATA_DIR=Path(self.temp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.config = SMTPSettings.objects.create(
            enabled=True,
            host="smtp.example.invalid",
            port=587,
            security="starttls",
            username="relay-user",
            password="synthetic-password",
            from_email="sms@example.com",
            recipients="reader@example.com",
        )
        self.backend_patch = patch("inbox.forwarding.EmailBackend")
        self.backend = self.backend_patch.start()
        self.addCleanup(self.backend_patch.stop)
        self.connection = self.backend.return_value.__enter__.return_value
        self.connection.send_messages.return_value = 1

    def test_complete_message_is_queued_once_and_forwarded_with_timestamp(self):
        receipt = archive("test", 0, PDU)
        archive("test", 3, PDU)
        self.assertEqual(ForwardDelivery.objects.count(), 1)
        forward_pending()
        email = self.connection.send_messages.call_args.args[0][0]
        self.assertEqual(email.to, ["reader@example.com"])
        self.assertIn(receipt.message.sender, email.subject)
        self.assertIn(
            Message.objects.get(pk=receipt.message_id).received_at.isoformat(), email.body
        )
        self.assertTrue(email.body.endswith("Hi"))
        self.assertIsNotNone(ForwardDelivery.objects.get().sent_at)
        forward_pending()
        self.assertEqual(self.connection.send_messages.call_count, 1)
        self.backend.assert_called_once_with(
            host="smtp.example.invalid",
            port=587,
            username="relay-user",
            password="synthetic-password",
            use_tls=True,
            use_ssl=False,
            timeout=10,
            fail_silently=False,
        )

    def test_multipart_waits_for_all_parts(self):
        first = make_pdu(
            bytes.fromhex("050003AB0201") + "Hello ".encode("utf-16-be"), dcs=8, first=0x40
        )
        second = make_pdu(
            bytes.fromhex("050003AB0202") + "world".encode("utf-16-be"), dcs=8, first=0x40
        )
        archive("test", 2, second)
        self.assertFalse(ForwardDelivery.objects.exists())
        archive("test", 0, first)
        archive("test", 2, second)
        self.assertEqual(ForwardDelivery.objects.count(), 1)
        forward_pending()
        self.assertTrue(
            self.connection.send_messages.call_args.args[0][0].body.endswith("Hello world")
        )

    def test_disabled_does_not_queue_or_backfill_old_messages(self):
        self.config.enabled = False
        self.config.save()
        archive("test", 0, PDU)
        self.config.enabled = True
        self.config.save()
        archive("test", 0, PDU)
        forward_pending()
        self.assertFalse(ForwardDelivery.objects.exists())
        self.backend.assert_not_called()

    def test_failure_retries_with_same_message_id_without_exposing_error(self):
        archive("test", 0, PDU)
        self.connection.send_messages.side_effect = RuntimeError("private-server-secret")
        forward_pending()
        failed_email = self.connection.send_messages.call_args.args[0][0]
        delivery = ForwardDelivery.objects.get()
        self.assertIsNone(delivery.sent_at)
        self.assertEqual(delivery.attempts, 1)
        self.assertGreater(delivery.next_attempt_at, timezone.now())
        self.assertNotIn("private-server-secret", delivery.last_error)
        forward_pending()
        self.assertEqual(self.connection.send_messages.call_count, 1)
        ForwardDelivery.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.connection.send_messages.side_effect = None
        forward_pending()
        sent_email = self.connection.send_messages.call_args.args[0][0]
        self.assertEqual(
            failed_email.extra_headers["Message-ID"], sent_email.extra_headers["Message-ID"]
        )
        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.sent_at)
        self.assertEqual(delivery.last_error, "")

    def test_partial_recipient_failure_does_not_resend_successful_recipients(self):
        self.config.recipients = "first@example.com, second@example.com"
        self.config.save()
        archive("test", 0, PDU)
        self.connection.send_messages.side_effect = [1, RuntimeError("refused")]
        forward_pending()
        self.assertEqual(ForwardDelivery.objects.get().delivered_to, ["first@example.com"])
        self.config.recipients = "new@example.com"
        self.config.save()
        ForwardDelivery.objects.update(next_attempt_at=timezone.now())
        self.connection.send_messages.side_effect = None
        forward_pending()
        addresses = [call.args[0][0].to for call in self.connection.send_messages.call_args_list]
        self.assertEqual(
            addresses, [["first@example.com"], ["second@example.com"], ["second@example.com"]]
        )
        self.assertIsNotNone(ForwardDelivery.objects.get().sent_at)

    @override_settings(LTE_HOST="modem.example.invalid", LTE_USERNAME="sms")
    def test_smtp_failure_does_not_prevent_modem_cleanup(self):
        Installation.objects.create(completed=True)
        seed_connection()
        modem = MagicMock()
        modem.__enter__.return_value = modem
        modem.list_pdus.return_value = {0: PDU}
        modem.read_pdu.return_value = PDU
        with patch.dict(os.environ, {"LTE_HOST": "modem.example.invalid"}):
            sync_once(MagicMock(return_value=modem))
        self.connection.send_messages.side_effect = OSError("offline")
        forward_pending()
        modem.delete.assert_called_once_with(0)
        self.assertIsNotNone(Receipt.objects.get().cleared_at)
        self.assertEqual(Message.objects.get().text, "Hi")
        self.assertIsNone(ForwardDelivery.objects.get().sent_at)

    def test_legacy_environment_does_not_override_saved_smtp_settings(self):
        archive("test", 0, PDU)
        with patch.dict(os.environ, {"SMTP_ENABLED": "1", "SMTP_HOST": ""}):
            values, keys = smtp_values()
            self.assertEqual(values["password"], "synthetic-password")
            self.assertEqual(keys, ())
            forward_pending()
        self.assertIsNotNone(ForwardDelivery.objects.get().sent_at)

    def test_disabled_queue_pauses_and_deleting_sms_removes_pending_delivery(self):
        archive("test", 0, PDU)
        self.config.enabled = False
        self.config.save()
        forward_pending()
        self.backend.assert_not_called()
        self.assertTrue(ForwardDelivery.objects.exists())
        Message.objects.all().delete()
        self.assertFalse(ForwardDelivery.objects.exists())

    def test_lock_prevents_overlapping_delivery(self):
        archive("test", 0, PDU)
        with forwarding_lock() as acquired:
            self.assertTrue(acquired)
            forward_pending()
        self.backend.assert_not_called()
        self.assertEqual(ForwardDelivery.objects.get().attempts, 0)

    def test_real_smtp_transport_with_unicode_body(self):
        captured = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"220 localhost test SMTP\r\n")
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    command = line.split(b" ", 1)[0].strip().upper()
                    if command == b"DATA":
                        self.wfile.write(b"354 End with dot\r\n")
                        body = []
                        while (line := self.rfile.readline()) not in (b".\r\n", b""):
                            body.append(line[1:] if line.startswith(b"..") else line)
                        captured.append(b"".join(body))
                        self.wfile.write(b"250 Accepted\r\n")
                    elif command == b"QUIT":
                        self.wfile.write(b"221 Bye\r\n")
                        return
                    else:
                        self.wfile.write(b"250 OK\r\n")

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        server_thread.start()
        try:
            self.backend_patch.stop()
            self.config.host = "127.0.0.1"
            self.config.port = server.server_address[1]
            self.config.security = "none"
            self.config.username = self.config.password = ""
            self.config.save()
            pdu = make_pdu("Hello, café ☀".encode("utf-16-be"), dcs=8)
            archive("test", 0, pdu)
            forward_pending()
            self.assertEqual(len(captured), 1)
            email = BytesParser(policy=policy.default).parsebytes(captured[0])
            self.assertEqual(email["To"], "reader@example.com")
            self.assertIn("Hello, café ☀", email.get_content())
            self.assertIsNotNone(ForwardDelivery.objects.get().sent_at)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
