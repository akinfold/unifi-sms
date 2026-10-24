from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import ForwardDelivery, Message, SMTPSettings


class MessageForwardingStatusTests(TestCase):
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

    def test_list_reports_not_queued_without_claiming_it_will_be_sent(self):
        result = self.client.get("/api/messages/")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(
            result.json()["messages"][0]["forwarding"],
            {"state": "not_queued", "sent_at": None},
        )

    def test_list_reports_pending_and_error_states_without_delivery_details(self):
        delivery = ForwardDelivery.objects.create(
            message=self.message,
            recipients=["recipient@example.invalid"],
        )
        delivery.last_error = "synthetic transport failure"
        delivery.save(update_fields=["last_error"])

        payload = self.client.get("/api/messages/").json()["messages"][0]

        self.assertEqual(
            payload["forwarding"], {"state": "error", "sent_at": None}
        )
        self.assertNotIn("last_error", payload["forwarding"])
        self.assertNotIn("recipients", payload["forwarding"])
        self.assertNotIn("synthetic transport failure", str(payload))
        self.assertNotIn("recipient@example.invalid", str(payload))

        delivery.last_error = ""
        delivery.save(update_fields=["last_error"])
        payload = self.client.get("/api/messages/").json()["messages"][0]
        self.assertEqual(
            payload["forwarding"], {"state": "pending", "sent_at": None}
        )

    def test_sent_state_includes_iso_timestamp_and_wins_over_old_error(self):
        sent_at = timezone.now() - timedelta(minutes=3)
        ForwardDelivery.objects.create(
            message=self.message,
            sent_at=sent_at,
            last_error="stale error from a previous attempt",
        )

        self.assertEqual(self.client.get(f"/api/messages/{self.message.pk}/").status_code, 405)
        detail = self.client.post(f"/api/messages/{self.message.pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            detail.json()["forwarding"],
            {"state": "sent", "sent_at": sent_at.isoformat()},
        )
        self.assertNotIn("last_error", detail.json()["forwarding"])

    def test_detail_reports_not_queued_and_forwarding_errors_do_not_leak(self):
        response = self.client.post(f"/api/messages/{self.message.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["forwarding"],
            {"state": "not_queued", "sent_at": None},
        )

    def test_anonymous_and_non_staff_users_cannot_read_forwarding_status(self):
        self.client.logout()
        self.assertEqual(self.client.get("/api/messages/").status_code, 403)
        self.assertEqual(
            self.client.post(f"/api/messages/{self.message.pk}/").status_code, 403
        )

        reader = User.objects.create_user(
            "reader", password="Example-test-password-94!"
        )
        self.client.force_login(reader)
        self.assertEqual(self.client.get("/api/messages/").status_code, 403)
        self.assertEqual(
            self.client.post(f"/api/messages/{self.message.pk}/").status_code, 403
        )

    def assert_forwarding_visible(self, expected):
        listing = self.client.get("/api/messages/")
        detail = self.client.post(f"/api/messages/{self.message.pk}/")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertIs(listing.json()["forwarding_enabled"], expected)
        self.assertIs(detail.json()["forwarding_enabled"], expected)

    def test_no_smtp_configuration_hides_status_even_with_sent_history(self):
        ForwardDelivery.objects.create(message=self.message, sent_at=timezone.now())
        self.assert_forwarding_visible(False)

    def test_configured_forwarding_is_visible_only_while_enabled(self):
        config = SMTPSettings.objects.create(
            enabled=False,
            host="smtp.example.invalid",
            from_email="sms@example.com",
            recipients="reader@example.com",
        )
        self.assert_forwarding_visible(False)
        config.enabled = True
        config.save(update_fields=["enabled"])
        self.assert_forwarding_visible(True)
        config.enabled = False
        config.save(update_fields=["enabled"])
        self.assert_forwarding_visible(False)

    def test_enabled_but_invalid_smtp_configuration_hides_status(self):
        valid = {
            "enabled": True,
            "host": "smtp.example.invalid",
            "from_email": "sms@example.com",
            "recipients": "reader@example.com",
        }
        for field, value in (
            ("host", ""),
            ("from_email", ""),
            ("recipients", ""),
            ("recipients", "invalid-address"),
            ("username", "user-without-password"),
            ("port", 0),
            ("security", "invalid"),
        ):
            with self.subTest(field=field):
                SMTPSettings.objects.update_or_create(pk=1, defaults={
                    **valid, "port": 587, "security": "starttls", "username": "",
                    field: value,
                })
                self.assert_forwarding_visible(False)

    def test_opened_status_is_independent_of_filters_and_does_not_mark_read(self):
        ForwardDelivery.objects.create(message=self.message, last_error="private failure")
        for params in (
            {"q": "does not match"},
            {"unread": "1"},
            {"page": "2"},
        ):
            with self.subTest(params=params):
                payload = self.client.get(
                    "/api/messages/", {**params, "opened": self.message.pk}
                ).json()
                self.assertEqual(payload["opened_forwarding"], {
                    "id": self.message.pk,
                    "forwarding": {"state": "error", "sent_at": None},
                })
                self.assertNotIn("private failure", str(payload))
        self.message.refresh_from_db()
        self.assertIsNone(self.message.read_at)
        self.message.read_at = timezone.now()
        self.message.save(update_fields=["read_at"])
        payload = self.client.get(
            "/api/messages/", {"unread": "1", "opened": self.message.pk}
        ).json()
        self.assertEqual(payload["messages"], [])
        self.assertEqual(payload["opened_forwarding"]["id"], self.message.pk)

    def test_missing_or_invalid_opened_message_does_not_break_listing(self):
        for value in ("", "invalid", "-1", "0", str(2**63), "9" * 5000):
            with self.subTest(value=value[:30]):
                response = self.client.get("/api/messages/", {"opened": value})
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.json()["opened_forwarding"])
        self.message.delete()
        self.assertIsNone(self.client.get(
            "/api/messages/", {"opened": 999999}
        ).json()["opened_forwarding"])
