import json
from datetime import datetime, timezone as datetime_timezone

from django.contrib.auth.hashers import make_password
from django.test import Client, TestCase, override_settings
from django.urls import path

from . import api_views
from .models import ForwardDelivery, IntegrationSettings, Message, Receipt

urlpatterns = [
    path("api/v1/messages/", api_views.messages),
    path("api/v1/messages/<int:pk>/", api_views.message),
]


@override_settings(ROOT_URLCONF=__name__, ALLOWED_HOSTS=["testserver"])
class SmsRestApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.key = "rest-test-key-value"
        IntegrationSettings.objects.create(
            pk=1,
            rest_enabled=True,
            rest_key_hash=make_password(self.key),
            rest_key_suffix=self.key[-4:],
            mcp_enabled=True,
            mcp_key_hash=make_password("different-mcp-key"),
            mcp_key_suffix="key",
        )
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {self.key}"}
        self.data = {
            "sender": "+15551234567",
            "text": "Test message",
            "received_at": "2026-10-24T12:30:00+03:00",
            "read": False,
        }

    def test_auth_disabled_invalid_and_separate_keys(self):
        IntegrationSettings.objects.filter(pk=1).update(rest_enabled=False)
        self.assertEqual(self.client.get("/api/v1/messages/").status_code, 404)
        self.assertEqual(self.client.options("/api/v1/messages/").status_code, 404)
        IntegrationSettings.objects.filter(pk=1).update(rest_enabled=True)
        denied = self.client.get("/api/v1/messages/")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied["WWW-Authenticate"], 'Bearer realm="sms-archive"')
        self.assertEqual(self.client.options("/api/v1/messages/", **self.headers).status_code, 405)
        self.assertEqual(self.client.get("/api/v1/messages/", HTTP_AUTHORIZATION="Bearer different-mcp-key").status_code, 401)

    def test_create_list_detail_update_and_delete(self):
        created = self.client.post("/api/v1/messages/", json.dumps(self.data), content_type="application/json", **self.headers)
        self.assertEqual(created.status_code, 201)
        payload = created.json()
        self.assertEqual(payload["source"], "api")
        self.assertEqual(payload["text"], self.data["text"])
        self.assertFalse(payload["pending_cleanup"])
        self.assertFalse(ForwardDelivery.objects.exists())
        listing = self.client.get("/api/v1/messages/?page_size=1&read=false", **self.headers)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["count"], 1)
        detail = self.client.get(f"/api/v1/messages/{payload['id']}/", **self.headers)
        self.assertEqual(detail.json()["sender"], self.data["sender"])
        patched = self.client.patch(f"/api/v1/messages/{payload['id']}/", json.dumps({"read": True}), content_type="application/json", **self.headers)
        self.assertTrue(patched.json()["read"])
        deleted = self.client.delete(f"/api/v1/messages/{payload['id']}/", **self.headers)
        self.assertEqual(deleted.status_code, 204)

    def test_validation_and_page_bounds(self):
        bad = {**self.data, "source": "other"}
        self.assertEqual(self.client.post("/api/v1/messages/", json.dumps(bad), content_type="application/json", **self.headers).status_code, 400)
        naive = {**self.data, "received_at": "2026-10-24T12:30:00"}
        self.assertEqual(self.client.post("/api/v1/messages/", json.dumps(naive), content_type="application/json", **self.headers).status_code, 400)
        impossible = {**self.data, "received_at": "2026-02-30T12:00:00Z"}
        self.assertEqual(self.client.post("/api/v1/messages/", json.dumps(impossible), content_type="application/json", **self.headers).status_code, 400)
        for timestamp in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
            with self.subTest(timestamp=timestamp):
                data = {**self.data, "received_at": timestamp}
                self.assertEqual(self.client.post("/api/v1/messages/", json.dumps(data), content_type="application/json", **self.headers).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/messages/?page_size=101", **self.headers).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/messages/?page_size=1&page=2", **self.headers).status_code, 400)

    def test_deeply_nested_json_returns_safe_client_error(self):
        body = b"{" * 2000 + b"0" + b"}" * 2000
        response = self.client.post("/api/v1/messages/", body, content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "Request body must contain valid JSON."})

    def test_delete_and_content_mutation_preserve_pending_cleanup(self):
        message = Message.objects.create(source="modem", sender="+15550000000", text="Original", received_at=datetime(2026, 10, 24, tzinfo=datetime_timezone.utc))
        receipt = Receipt.objects.create(fingerprint="a" * 64, source="modem", message=message, pdu="0011", slot=1)
        path = f"/api/v1/messages/{message.pk}/"
        self.assertEqual(self.client.delete(path, **self.headers).status_code, 409)
        changed = self.client.patch(path, json.dumps({"text": "changed"}), content_type="application/json", **self.headers)
        self.assertEqual(changed.status_code, 409)
        read = self.client.patch(path, json.dumps({"read": True}), content_type="application/json", **self.headers)
        self.assertEqual(read.status_code, 200)
        receipt.cleared_at = datetime(2026, 10, 24, tzinfo=datetime_timezone.utc)
        receipt.save(update_fields=["cleared_at"])
        self.assertEqual(self.client.delete(path, **self.headers).status_code, 204)
        receipt.refresh_from_db()
        self.assertEqual(receipt.pdu, "")
        self.assertEqual(receipt.fingerprint, "a" * 64)

    def test_incomplete_message_content_cannot_change_but_read_can(self):
        message = Message.objects.create(
            source="modem",
            sender="+15550000000",
            text="Partial [Missing part 2]",
            received_at=datetime(2026, 10, 24, tzinfo=datetime_timezone.utc),
            total_parts=2,
            saved_parts=1,
        )
        path = f"/api/v1/messages/{message.pk}/"
        changed = self.client.patch(path, json.dumps({"sender": "replacement"}), content_type="application/json", **self.headers)
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(self.client.patch(path, json.dumps({"read": True}), content_type="application/json", **self.headers).status_code, 200)

    def test_response_does_not_expose_receipt_or_device_secrets(self):
        message = Message.objects.create(source="modem", sender="+15550000000", text="Body", received_at=datetime(2026, 10, 24, tzinfo=datetime_timezone.utc))
        Receipt.objects.create(fingerprint="b" * 64, source="modem", message=message, pdu="PRIVATE-PDU", slot=2)
        response = self.client.get(f"/api/v1/messages/{message.pk}/", **self.headers)
        encoded = response.content.decode()
        self.assertNotIn("PRIVATE-PDU", encoded)
        self.assertNotIn("fingerprint", encoded)
        self.assertNotIn("password", encoded)
