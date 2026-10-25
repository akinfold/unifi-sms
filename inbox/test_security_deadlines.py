import os
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone as datetime_timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Message, WebhookDelivery, WebhookSettings
from .webhooks import _post, _safe_error, deliver_webhooks, queue_webhook


class WebhookDeadlineTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.override = override_settings(DATA_DIR=Path(self.directory.name))
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.slow_requests = 0
        self.received = []
        self.children = []
        self.stop = threading.Event()
        self.addCleanup(self.stop.set)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.received.append((dict(self.headers), body))
                if owner.slow_requests:
                    owner.slow_requests -= 1
                    try:
                        self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                        while not owner.stop.wait(0.02):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                    except OSError:
                        pass
                    return
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        real_popen = subprocess.Popen

        def tracked_popen(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            self.children.append(child)
            return child

        self.process_patch = patch("inbox.webhooks.subprocess.Popen", side_effect=tracked_popen)
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)

    def close_server(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def message(self):
        return Message.objects.create(source="synthetic", sender="+15550102001",
                                      received_at=timezone.now(), text="Hello",
                                      total_parts=1, saved_parts=1)

    def assert_reaped(self):
        self.assertTrue(self.children)
        for child in self.children:
            self.assertIsNotNone(child.poll())
            self.assertTrue(child.stdin.closed)
            self.assertTrue(child.stdout.closed)

    def test_slow_headers_time_out_then_worker_delivers_next_message_and_retries(self):
        WebhookSettings.objects.create(pk=1, enabled=True, url=self.url, secret="synthetic-secret")
        first = queue_webhook(self.message())
        second = queue_webhook(self.message())
        self.slow_requests = 1
        started = time.monotonic()
        with patch("inbox.webhooks._POST_TIMEOUT", 0.8):
            deliver_webhooks()
        self.assertLess(time.monotonic() - started, 2.5)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.sent_at)
        self.assertEqual(first.last_error, "Webhook request timed out. A retry is scheduled.")
        self.assertEqual(first.attempts, 1)
        self.assertGreater(first.next_attempt_at, timezone.now())
        self.assertIsNotNone(second.sent_at, second.last_error)
        self.assert_reaped()
        WebhookDelivery.objects.filter(pk=first.pk).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1))
        with patch("inbox.webhooks._POST_TIMEOUT", 0.8):
            deliver_webhooks()
        first.refresh_from_db()
        self.assertIsNotNone(first.sent_at)
        self.assertEqual(first.attempts, 2)
        self.assertEqual(self.received[0][0]["Idempotency-Key"],
                         self.received[2][0]["Idempotency-Key"])
        self.assertEqual(self.received[0][1], self.received[2][1])
        self.assert_reaped()

    def test_repeated_timeouts_reap_processes_and_close_pipes(self):
        self.slow_requests = 3
        with patch("inbox.webhooks._POST_TIMEOUT", 0.2):
            for _ in range(3):
                with self.assertRaises(TimeoutError):
                    _post(self.url, {}, "secret", "X-Secret", "same-token")
                self.assert_reaped()
        self.assertEqual(len(self.children), 3)

    def test_environment_proxy_is_disabled_for_admin_configured_lan_http(self):
        environment = {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1",
                       "all_proxy": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1",
                       "no_proxy": "", "NO_PROXY": ""}
        with patch.dict(os.environ, environment), patch("inbox.webhooks._POST_TIMEOUT", 1):
            _post(self.url, {}, "", "X-Secret", "token")
        self.assertEqual(len(self.received), 1)
        self.assert_reaped()

    def test_blocked_dns_resolution_is_cancelled_with_the_child(self):
        from . import webhook_http

        wrapper = Path(self.directory.name) / "blocked_dns.py"
        wrapper.write_text(
            "import runpy, socket, time\n"
            "def blocked(*args, **kwargs):\n    time.sleep(30)\n"
            "socket.getaddrinfo = blocked\n"
            f"runpy.run_path({str(Path(webhook_http.__file__))!r}, run_name='__main__')\n"
        )
        # Use the real process factory rather than the tracking patch recursively.
        self.process_patch.stop()
        real_popen = subprocess.Popen

        def spawn_blocked(*args, **kwargs):
            child = real_popen([sys.executable, "-I", str(wrapper)], **kwargs)
            self.children.append(child)
            return child

        started = time.monotonic()
        with patch("inbox.webhooks.subprocess.Popen", side_effect=spawn_blocked), patch(
                "inbox.webhooks._POST_TIMEOUT", 0.2):
            with self.assertRaises(TimeoutError):
                _post("http://synthetic.invalid/", {}, "", "X-Secret", "token")
        self.assertLess(time.monotonic() - started, 0.8)
        self.assert_reaped()

    def test_stalled_tls_handshake_and_request_upload_have_total_deadlines(self):
        stopped = threading.Event()

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                stopped.wait(2)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

        server = Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for scheme, payload in (("https", {}), ("http", {"text": "x" * 8_000_000})):
                with self.subTest(scheme=scheme):
                    started = time.monotonic()
                    with patch("inbox.webhooks._POST_TIMEOUT", 0.3):
                        with self.assertRaises(TimeoutError):
                            _post(f"{scheme}://127.0.0.1:{server.server_address[1]}/", payload,
                                  "", "X-Secret", "token")
                    self.assertLess(time.monotonic() - started, 1)
                    self.assert_reaped()
        finally:
            stopped.set()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_https_rejects_an_untrusted_certificate(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(datetime_timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                       .public_key(key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - timedelta(minutes=1))
                       .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
        certificate_path = Path(self.directory.name) / "synthetic-cert.pem"
        key_path = Path(self.directory.name) / "synthetic-key.pem"
        certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                              serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate_path, key_path)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        with patch("inbox.webhooks._POST_TIMEOUT", 1):
            with self.assertRaises(ssl.SSLError) as error:
                _post(self.url.replace("http:", "https:"), {}, "", "X-Secret", "token")
        self.assertEqual(_safe_error(error.exception),
                         "Webhook TLS verification failed. A retry is scheduled.")
        self.assertEqual(self.received, [])
        self.assert_reaped()
