from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import Message, WebhookDelivery, WebhookSettings


class MessageWebhookStatusTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            "administrator", password="Example-test-password-94!"
        )
        self.client.force_login(self.admin)
        self.message = Message.objects.create(
            source="synthetic-source",
            sender="+15555550123",
            received_at=timezone.now(),
            text="Synthetic message",
        )

    def set_webhook(self, **values):
        defaults = {
            "enabled": True,
            "url": "https://hooks.example.invalid/sms",
            "secret": "synthetic-secret-value",
            "secret_header": "X-Webhook-Secret",
        }
        defaults.update(values)
        return WebhookSettings.objects.update_or_create(pk=1, defaults=defaults)[0]

    def test_list_and_detail_report_not_queued_when_no_delivery_exists(self):
        self.set_webhook()

        listing = self.client.get("/api/messages/")
        detail = self.client.post(f"/api/messages/{self.message.pk}/")

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            listing.json()["messages"][0]["webhook"],
            {"state": "not_queued", "sent_at": None},
        )
        self.assertEqual(detail.json()["webhook"], {"state": "not_queued", "sent_at": None})
        self.assertIs(listing.json()["webhook_enabled"], True)
        self.assertIs(detail.json()["webhook_enabled"], True)

    def test_pending_error_and_sent_states_do_not_expose_delivery_details(self):
        delivery = WebhookDelivery.objects.create(
            message=self.message,
            payload={"url": "https://private.example.invalid/secret", "secret": "synthetic-secret-value"},
            last_error="synthetic transport failure",
        )
        for endpoint in (
            self.client.get("/api/messages/"),
            self.client.post(f"/api/messages/{self.message.pk}/"),
        ):
            payload = endpoint.json()
            status = payload["messages"][0]["webhook"] if "messages" in payload else payload["webhook"]
            self.assertEqual(status, {"state": "error", "sent_at": None})
            self.assertNotIn("last_error", status)
            self.assertNotIn("payload", status)
            self.assertNotIn("synthetic transport failure", str(payload))
            self.assertNotIn("synthetic-secret-value", str(payload))
            self.assertNotIn("private.example.invalid", str(payload))

        delivery.last_error = ""
        delivery.save(update_fields=["last_error"])
        self.assertEqual(
            self.client.get("/api/messages/").json()["messages"][0]["webhook"],
            {"state": "pending", "sent_at": None},
        )

        sent_at = timezone.now() - timedelta(minutes=3)
        delivery.sent_at = sent_at
        delivery.last_error = "old error"
        delivery.save(update_fields=["sent_at", "last_error"])
        detail = self.client.post(f"/api/messages/{self.message.pk}/")
        self.assertEqual(
            detail.json()["webhook"],
            {"state": "sent", "sent_at": sent_at.isoformat()},
        )

    def test_webhook_enabled_requires_a_valid_enabled_configuration(self):
        self.set_webhook(enabled=False)
        self.assert_visibility(False)

        self.set_webhook(url="")
        self.assert_visibility(False)

        self.set_webhook(url="https://hooks.example.invalid/sms")
        self.assert_visibility(True)

        self.set_webhook(url="https://user:password@hooks.example.invalid/sms")
        self.assert_visibility(False)

    def test_opened_webhook_is_independent_of_filters_and_does_not_mark_read(self):
        self.set_webhook()
        WebhookDelivery.objects.create(message=self.message, last_error="private failure")

        payload = self.client.get(
            "/api/messages/", {"q": "no match", "opened": self.message.pk}
        ).json()

        self.assertEqual(payload["opened_webhook"], {
            "id": self.message.pk,
            "webhook": {"state": "error", "sent_at": None},
        })
        self.assertNotIn("private failure", str(payload))
        self.message.refresh_from_db()
        self.assertIsNone(self.message.read_at)

    def test_anonymous_and_non_staff_users_cannot_read_webhook_status(self):
        self.client.logout()
        self.assertEqual(self.client.get("/api/messages/").status_code, 403)
        self.assertEqual(self.client.post(f"/api/messages/{self.message.pk}/").status_code, 403)

        reader = User.objects.create_user("reader", password="Example-test-password-94!")
        self.client.force_login(reader)
        self.assertEqual(self.client.get("/api/messages/").status_code, 403)
        self.assertEqual(self.client.post(f"/api/messages/{self.message.pk}/").status_code, 403)

    def assert_visibility(self, expected):
        listing = self.client.get("/api/messages/")
        detail = self.client.post(f"/api/messages/{self.message.pk}/")
        self.assertIs(listing.json()["webhook_enabled"], expected)
        self.assertIs(detail.json()["webhook_enabled"], expected)
