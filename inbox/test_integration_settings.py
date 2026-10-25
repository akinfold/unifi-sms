from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.test import Client, TestCase

from .api_keys import check_api_key, is_api_key_digest
from .connection import decrypt_secret
from .models import IntegrationSettings, WebhookSettings


REST_KEY = "synthetic-rest-key-" + "r" * 32
MCP_KEY = "synthetic-mcp-key-" + "m" * 32


class IntegrationSettingsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("integration-admin", password="SyntheticPass-475!")
        self.client.force_login(self.admin)

    def test_settings_default_to_disabled_and_require_administrator(self):
        for endpoint in ("/settings/integrations/", "/settings/webhooks/"):
            self.assertEqual(self.client.get(endpoint).status_code, 200)
        self.assertFalse(IntegrationSettings().rest_enabled)
        self.assertFalse(IntegrationSettings().mcp_enabled)
        self.assertFalse(WebhookSettings().enabled)
        self.client.logout()
        for endpoint in ("/settings/integrations/", "/settings/webhooks/"):
            self.assertRedirects(self.client.get(endpoint), "/login/")
        self.assertEqual(self.client.post("/api/integrations/key/").status_code, 403)
        reader = User.objects.create_user("reader", password="SyntheticPass-475!")
        self.client.force_login(reader)
        self.assertRedirects(self.client.get("/settings/integrations/"), "/login/")
        self.assertEqual(self.client.post("/api/integrations/key/").status_code, 403)

    def test_generate_is_csrf_protected_and_does_not_activate_key(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.post("/api/integrations/key/").status_code, 403)
        csrf_client.get("/settings/integrations/")
        response = csrf_client.post(
            "/api/integrations/key/", HTTP_X_CSRFTOKEN=csrf_client.cookies["csrftoken"].value
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        key = response.json()["key"]
        self.assertGreaterEqual(len(key), 32)
        self.assertNotEqual(self.client.post("/api/integrations/key/").json()["key"], key)
        self.assertFalse(IntegrationSettings.objects.exists())

    def test_save_keeps_hashes_private_and_preserves_keys_when_blank(self):
        response = self.client.post("/settings/integrations/", {
            "rest_enabled": "on", "rest_key": REST_KEY,
            "mcp_enabled": "on", "mcp_key": MCP_KEY,
        })
        self.assertRedirects(response, "/settings/integrations/")
        stored = IntegrationSettings.objects.get(pk=1)
        self.assertTrue(stored.rest_enabled)
        self.assertTrue(stored.mcp_enabled)
        self.assertTrue(check_api_key(REST_KEY, stored.rest_key_hash))
        self.assertTrue(check_api_key(MCP_KEY, stored.mcp_key_hash))
        self.assertNotEqual(stored.rest_key_hash, REST_KEY)
        page = self.client.get("/settings/integrations/")
        for value in (REST_KEY, MCP_KEY, stored.rest_key_hash, stored.mcp_key_hash):
            self.assertNotContains(page, value)
        self.client.post("/settings/integrations/", {"rest_enabled": "on", "mcp_enabled": "on"})
        stored.refresh_from_db()
        self.assertTrue(check_api_key(REST_KEY, stored.rest_key_hash))
        self.assertTrue(check_api_key(MCP_KEY, stored.mcp_key_hash))

    def test_generated_and_manual_keys_both_use_keyed_hashes(self):
        generated = self.client.post("/api/integrations/key/").json()["key"]
        manual = "a" * 32
        self.assertRedirects(self.client.post("/settings/integrations/", {
            "rest_enabled": "on", "rest_key": manual,
            "mcp_enabled": "on", "mcp_key": generated,
        }), "/settings/integrations/")
        stored = IntegrationSettings.objects.get(pk=1)
        for key, encoded in ((manual, stored.rest_key_hash), (generated, stored.mcp_key_hash)):
            with self.subTest(key=key):
                self.assertTrue(is_api_key_digest(encoded))
                self.assertTrue(check_api_key(key, encoded))
        self.assertEqual(self.client.get("/api/v1/messages/", HTTP_AUTHORIZATION="Bearer " + manual).status_code, 200)
        self.assertEqual(self.client.post("/mcp/", data={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                          content_type="application/json", HTTP_AUTHORIZATION="Bearer " + generated).status_code, 200)

    def test_key_validation_revoke_and_rotation(self):
        self.client.post("/settings/integrations/", {"rest_enabled": "on", "rest_key": "too-short"})
        self.assertFalse(IntegrationSettings.objects.exists())
        self.client.post("/settings/integrations/", {"mcp_enabled": "on", "mcp_key": "x" * 32 + "\n"})
        self.assertFalse(IntegrationSettings.objects.exists())
        stored = IntegrationSettings.objects.create(
            rest_enabled=True, rest_key_hash=make_password(REST_KEY), rest_key_suffix=REST_KEY[-4:]
        )
        response = self.client.post("/settings/integrations/", {
            "rest_enabled": "on", "rest_key_action": "revoke",
        })
        self.assertContains(response, "disable access to revoke")
        stored.refresh_from_db()
        self.assertTrue(check_api_key(REST_KEY, stored.rest_key_hash))
        replacement = "replacement-synthetic-key-" + "z" * 32
        self.client.post("/settings/integrations/", {"rest_enabled": "on", "rest_key": replacement})
        stored.refresh_from_db()
        self.assertFalse(check_api_key(REST_KEY, stored.rest_key_hash))
        self.assertTrue(check_api_key(replacement, stored.rest_key_hash))
        self.client.post("/settings/integrations/", {"rest_key_action": "revoke"})
        stored.refresh_from_db()
        self.assertFalse(stored.rest_enabled)
        self.assertEqual(stored.rest_key_hash, "")

    def test_invalid_forms_do_not_echo_submitted_secrets(self):
        page = self.client.post("/settings/integrations/", {
            "rest_enabled": "on", "rest_key": "secret-that-is-too-short", "mcp_key_action": "invalid",
        })
        self.assertNotContains(page, "secret-that-is-too-short")
        page = self.client.post("/settings/webhooks/", {
            "enabled": "on", "url": "invalid", "secret": "private-webhook-secret-marker",
            "secret_header": "X-Webhook-Secret",
        })
        self.assertNotContains(page, "private-webhook-secret-marker")

    def test_webhook_settings_save_encrypt_keep_and_remove_secret(self):
        data = {
            "enabled": "on", "url": "https://receiver.example.invalid/sms",
            "secret_header": "Authorization", "secret": "Bearer synthetic-test-secret",
        }
        self.assertRedirects(self.client.post("/settings/webhooks/", data), "/settings/webhooks/")
        stored = WebhookSettings.objects.get(pk=1)
        self.assertNotIn("synthetic-test-secret", stored.secret)
        self.assertEqual(decrypt_secret(stored.secret), data["secret"])
        self.assertNotContains(self.client.get("/settings/webhooks/"), data["secret"])
        data["secret"] = ""
        self.client.post("/settings/webhooks/", data)
        stored.refresh_from_db()
        self.assertEqual(decrypt_secret(stored.secret), "Bearer synthetic-test-secret")
        data["secret_action"] = "remove"
        self.client.post("/settings/webhooks/", data)
        stored.refresh_from_db()
        self.assertEqual(stored.secret, "")
        self.assertTrue(stored.enabled)

    def test_settings_posts_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post("/settings/integrations/", {"rest_key": REST_KEY}).status_code, 403)
        self.assertEqual(client.post("/settings/webhooks/", {"url": "https://example.invalid"}).status_code, 403)
        self.assertFalse(IntegrationSettings.objects.exists())
        self.assertFalse(WebhookSettings.objects.exists())

    def test_corrupt_webhook_secret_requires_explicit_replacement_or_removal(self):
        stored = WebhookSettings.objects.create(
            enabled=True, url="https://receiver.example.invalid/sms", secret_header="X-Webhook-Secret"
        )
        WebhookSettings.objects.filter(pk=stored.pk).update(secret="fernet:corrupt-synthetic-ciphertext")
        self.assertContains(self.client.get("/settings/webhooks/"), "cannot be decrypted")
        form = {"enabled": "on", "url": stored.url, "secret_header": stored.secret_header}
        response = self.client.post("/settings/webhooks/", form)
        self.assertContains(response, "Replace or remove the unreadable saved secret")
        stored.refresh_from_db()
        self.assertEqual(stored.secret, "fernet:corrupt-synthetic-ciphertext")
        self.assertRedirects(self.client.post("/settings/webhooks/", {**form, "secret_action": "remove"}), "/settings/webhooks/")
        stored.refresh_from_db()
        self.assertEqual(stored.secret, "")
