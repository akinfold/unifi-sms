import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from .api_auth import authenticate_request
from .api_keys import KEY_DIGEST_PREFIX, check_api_key, is_api_key_digest, make_api_key_hash
from .middleware import TrustedProxyMiddleware
from .models import AccessWindow, IntegrationSettings, Message
from .proxy import client_address


@override_settings(TRUST_PROXY=True, TRUSTED_PROXY_CIDRS=("192.0.2.0/24", "2001:db8::/32"))
class TrustedProxyTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def address(self, peer, chain):
        return client_address(self.factory.get("/", REMOTE_ADDR=peer, HTTP_X_FORWARDED_FOR=chain))

    def test_untrusted_peer_cannot_spoof_client_address(self):
        self.assertEqual(self.address("198.51.100.1", "203.0.113.1"), "198.51.100.1")
        self.assertEqual(self.address("invalid", "203.0.113.1"), "unknown")

    def test_chain_stops_at_first_untrusted_hop_from_the_right(self):
        self.assertEqual(self.address("192.0.2.3", "203.0.113.99, 198.51.100.4, 192.0.2.2"), "198.51.100.4")
        self.assertEqual(self.address("2001:db8::2", "2001:db8:1::3, 2001:db8::1"), "2001:db8:1::3")
        self.assertEqual(self.address("192.0.2.3", "not-an-ip"), "192.0.2.3")
        self.assertEqual(self.address("192.0.2.3", ",".join(["198.51.100.1"] * 33)), "192.0.2.3")
        self.assertEqual(self.address("192.0.2.3", "1" * 2049), "192.0.2.3")

    @override_settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"))
    def test_forwarded_https_requires_explicit_trusted_peer_and_one_protocol(self):
        middleware = TrustedProxyMiddleware(lambda request: HttpResponse(request.scheme))
        for peer, header, expected in (
            ("198.51.100.1", "https", b"http"),
            ("192.0.2.2", "https", b"https"),
            ("192.0.2.2", "https, http", b"http"),
            ("192.0.2.2", "http", b"http"),
        ):
            with self.subTest(peer=peer, header=header):
                request = self.factory.get("/", REMOTE_ADDR=peer, HTTP_X_FORWARDED_PROTO=header)
                self.assertEqual(middleware(request).content, expected)
        with override_settings(TRUST_PROXY=False):
            request = self.factory.get("/", REMOTE_ADDR="192.0.2.2", HTTP_X_FORWARDED_PROTO="https")
            self.assertEqual(middleware(request).content, b"http")
        with override_settings(TRUSTED_PROXY_CIDRS=()):
            request = self.factory.get("/", REMOTE_ADDR="192.0.2.2", HTTP_X_FORWARDED_PROTO="https")
            self.assertEqual(middleware(request).content, b"http")


class ApiAuthenticationSecurityTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.key = "synthetic-random-api-key-32-characters-long"
        self.config = IntegrationSettings.objects.create(
            pk=1, rest_enabled=True, mcp_enabled=True,
            rest_key_hash=make_api_key_hash(self.key), mcp_key_hash=make_api_key_hash(self.key),
        )

    def request(self, key, kind="rest", peer="198.51.100.10"):
        request = self.factory.get("/api/v1/messages/", REMOTE_ADDR=peer, HTTP_AUTHORIZATION="Bearer " + key)
        return authenticate_request(request, kind)

    def test_digest_verification_avoids_password_hashing_and_uses_constant_time_comparison(self):
        with patch("inbox.api_keys.check_password") as slow, patch("inbox.api_keys.constant_time_compare", wraps=constant_time_compare) as compare:
            self.assertIsNone(self.request(self.key))
            self.assertEqual(self.request("wrong").status_code, 401)
            slow.assert_not_called()
            self.assertEqual(compare.call_count, 2)

    def test_unkeyed_sha256_cannot_substitute_for_key_or_saved_digest(self):
        raw_digest = hashlib.sha256(self.key.encode("utf-8")).hexdigest()
        with patch("inbox.api_keys.check_password") as slow:
            self.assertFalse(check_api_key(self.key, KEY_DIGEST_PREFIX + raw_digest))
            self.assertEqual(self.request(raw_digest).status_code, 401)
            slow.assert_not_called()

    def test_changed_application_secret_fails_closed_for_both_integrations(self):
        with override_settings(SECRET_KEY="a-different-synthetic-application-secret"), patch("inbox.api_keys.check_password") as slow:
            self.assertNotEqual(make_api_key_hash(self.key), self.config.rest_key_hash)
            for kind in ("rest", "mcp"):
                with self.subTest(kind=kind):
                    self.assertEqual(self.request(self.key, kind=kind).status_code, 401)
            slow.assert_not_called()
        self.assertIsNone(self.request(self.key))

    def test_successful_legacy_key_upgrades_without_changing_other_keys(self):
        old_hash = make_password(self.key)
        IntegrationSettings.objects.filter(pk=1).update(rest_key_hash=old_hash)
        self.assertIsNone(self.request(self.key))
        self.config.refresh_from_db()
        self.assertTrue(is_api_key_digest(self.config.rest_key_hash))
        self.assertTrue(check_api_key(self.key, self.config.rest_key_hash))
        self.assertTrue(check_api_key(self.key, self.config.mcp_key_hash))
        with patch("inbox.api_keys.check_password") as slow:
            self.assertIsNone(self.request(self.key))
            slow.assert_not_called()

    def test_legacy_upgrade_does_not_overwrite_concurrent_key_rotation(self):
        IntegrationSettings.objects.filter(pk=1).update(rest_key_hash=make_password(self.key))
        replacement = make_api_key_hash("a different replacement API key")
        def verify(*args):
            IntegrationSettings.objects.filter(pk=1).update(rest_key_hash=replacement)
            return True
        with patch("inbox.api_keys.check_password", side_effect=verify):
            self.assertIsNone(self.request(self.key))
        self.config.refresh_from_db()
        self.assertEqual(self.config.rest_key_hash, replacement)

    def test_oversized_and_non_ascii_bearer_values_never_reach_verification(self):
        for key in ("x" * 100000, "\ud800", "has whitespace", "x\n"):
            with self.subTest(length=len(key)), patch("inbox.api_auth.check_api_key") as verifier:
                self.assertEqual(self.request(key).status_code, 401)
                verifier.assert_not_called()

    def test_failure_limit_cannot_block_valid_digest_key_on_shared_client_address(self):
        for _ in range(30):
            self.assertEqual(self.request("invalid").status_code, 401)
        limited = self.request("invalid", kind="mcp")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited["Retry-After"], "60")
        self.assertIsNone(self.request(self.key))
        self.assertIsNone(self.request(self.key, kind="mcp"))

    def test_legacy_verification_has_client_and_global_budgets_then_recovers(self):
        IntegrationSettings.objects.filter(pk=1).update(rest_key_hash="legacy-hash")
        with patch("inbox.api_keys.check_password", return_value=False) as slow:
            for index in range(5):
                self.assertEqual(self.request("invalid").status_code, 401)
            self.assertEqual(self.request("invalid").status_code, 429)
            self.assertEqual(slow.call_count, 5)
            for index in range(15):
                self.assertEqual(self.request("invalid", peer=f"203.0.113.{index + 1}").status_code, 401)
            self.assertEqual(self.request("invalid", peer="203.0.113.100").status_code, 429)
            self.assertEqual(slow.call_count, 20)
        # A legacy key can be temporarily limited, then resumes without replacing its value.
        IntegrationSettings.objects.filter(pk=1).update(rest_key_hash=make_password(self.key))
        self.assertEqual(self.request(self.key).status_code, 429)
        AccessWindow.objects.update(started_at=timezone.now() - timedelta(seconds=61))
        self.assertIsNone(self.request(self.key))

    def test_invalid_legacy_key_preserves_saved_hash(self):
        old_hash = make_password(self.key)
        IntegrationSettings.objects.filter(pk=1).update(rest_key_hash=old_hash)
        self.assertEqual(self.request("invalid").status_code, 401)
        self.config.refresh_from_db()
        self.assertEqual(self.config.rest_key_hash, old_hash)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LoginRateSecurityTests(TestCase):
    def setUp(self):
        self.password = "Synthetic-test-password-94!"
        User.objects.create_superuser("administrator", password=self.password)
        User.objects.create_superuser("other-admin", password=self.password)

    def login(self, username="administrator", password="invalid", **headers):
        return self.client.post("/login/", {"username": username, "password": password}, **headers)

    def test_successful_logins_do_not_consume_failed_attempt_budget(self):
        for _ in range(12):
            self.assertEqual(self.login(password=self.password).status_code, 302)
            self.client.logout()
        self.assertFalse(AccessWindow.objects.exists())

    def test_failed_attempts_limit_account_client_pair_without_locking_other_account(self):
        for _ in range(10):
            self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.login().status_code, 429)
        self.assertEqual(self.login(username="other-admin", password=self.password).status_code, 302)

    def test_failed_budget_resets_after_successful_login(self):
        for _ in range(9):
            self.login()
        self.assertEqual(self.login(password=self.password).status_code, 302)
        self.client.logout()
        for _ in range(10):
            self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.login().status_code, 429)

    @override_settings(TRUSTED_PROXY_CIDRS=("192.0.2.0/24",))
    def test_clients_behind_trusted_proxy_have_separate_failure_budgets(self):
        headers = {"REMOTE_ADDR": "192.0.2.1", "HTTP_X_FORWARDED_FOR": "198.51.100.1"}
        for _ in range(10):
            self.login(**headers)
        self.assertEqual(self.login(**headers).status_code, 429)
        headers["HTTP_X_FORWARDED_FOR"] = "198.51.100.2"
        self.assertEqual(self.login(password=self.password, **headers).status_code, 302)

    def test_spoofed_forwarded_header_does_not_bypass_login_limit(self):
        for _ in range(10):
            self.login(REMOTE_ADDR="198.51.100.1", HTTP_X_FORWARDED_FOR="203.0.113.1")
        self.assertEqual(self.login(REMOTE_ADDR="198.51.100.1", HTTP_X_FORWARDED_FOR="203.0.113.2").status_code, 429)

    def test_client_failure_budget_limits_username_spraying(self):
        for index in range(100):
            self.assertEqual(self.login(username=f"unknown-{index}").status_code, 200)
        self.assertEqual(self.login(username="another-unknown").status_code, 429)


@override_settings(ALLOWED_HOSTS=["testserver"])
class MalformedAuthenticatedJsonTests(TestCase):
    def setUp(self):
        key = "synthetic-random-api-key-32-characters-long"
        IntegrationSettings.objects.create(pk=1, rest_enabled=True, mcp_enabled=True,
            rest_key_hash=make_api_key_hash(key), mcp_key_hash=make_api_key_hash(key))
        self.headers = {"HTTP_AUTHORIZATION": "Bearer " + key}

    def post(self, endpoint, body):
        return self.client.post(endpoint, body, content_type="application/json", **self.headers)

    def test_oversized_integer_and_surrogates_return_client_errors(self):
        for body in (b'{"id":' + b"9" * 5000 + b'}', b'{"id":"\\ud800"}', b'{"sender":"\\ud800"}', b'{"value":NaN}', b'{"id":1e9999}'):
            with self.subTest(body=body[:40]):
                self.assertEqual(self.post("/api/v1/messages/", body).status_code, 400)
                response = self.post("/mcp/", body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], -32700)
        self.assertFalse(Message.objects.exists())

    def test_invalid_rpc_id_types_are_rejected_without_echoing_them(self):
        for request_id in (True, [], {}):
            body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "ping"})
            payload = self.post("/mcp/", body).json()
            self.assertEqual(payload["error"]["code"], -32600)
            self.assertIsNone(payload["id"])

    def test_valid_rpc_id_types_are_preserved(self):
        for request_id in ("synthetic-id", 7, 1.5, None):
            body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "ping"})
            response = self.post("/mcp/", body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["id"], request_id)

    def test_notification_cannot_execute_mutating_tool(self):
        body = json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "create_sms", "arguments": {
            "sender": "synthetic", "text": "must not be written", "received_at": "2026-10-25T12:00:00Z", "read": False,
        }}})
        response = self.post("/mcp/", body)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, b"")
        self.assertFalse(Message.objects.exists())
