from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from modem.outbound import OutboundError, OutboundSimChanged
from modem.tests.test_pdu import make_pdu

from . import balance
from .models import BalanceMonitor, BalanceRun, Message, OutboundProfile, Receipt, SMTPSettings
from .outbound import connection_id
from .sync import sync_once
from .test_helpers import seed_connection

SIM = "a" * 64
PHONE = "+12025550123"


class BalanceTests(TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(override_settings(DATA_DIR=Path(directory)))
        seed_connection()
        self.admin = get_user_model().objects.create_user("balance-admin", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.factory = MagicMock()
        self.modem = self.factory.return_value.__enter__.return_value
        self.modem.balance_identity.return_value = (SIM, "26201")
        self.modem.request_balance.return_value = "Balance: 12.50 EUR"

    def action(self, operation, **payload):
        response = self.client.post(
            "/balance/action/", {"action": operation, **payload}, content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def status(self):
        return self.client.get("/balance/status/").json()

    def tick(self):
        balance.tick(client_factory=self.factory)

    def prepare(self):
        self.action("prepare")
        self.tick()
        self.assertEqual(self.status()["wizard"]["network"], "26201")

    def enable_direct(self, **settings):
        self.prepare()
        self.action("auto", carrier="telekom-de")
        self.tick()
        self.assertFalse(self.status()["summary"]["enabled"])
        self.action("enable", **settings)
        self.tick()
        return self.status()["summary"]

    def import_reply(self, text, sender="2000", age=0):
        stamp = (timezone.now() - timedelta(seconds=age)).strftime("%y%m%d%H%M%S") + "00"
        pdu = make_pdu(
            text.encode("utf-16-be"), sender=sender, toa=0x81, dcs=8,
            stamp=bytes.fromhex("".join(stamp[i:i + 2][::-1] for i in range(0, len(stamp), 2))),
        )
        self.modem.list_pdus.return_value = {1: pdu}
        self.modem.read_pdu.return_value = pdu
        sync_once(client_factory=self.factory)

    def test_setup_is_admin_only_and_disabled_by_default(self):
        self.assertContains(self.client.get("/"), 'href="/balance/"')
        self.assertContains(self.client.get("/balance/"), "Set up monitoring")
        self.assertFalse(self.status()["summary"]["enabled"])
        self.assertEqual(Client().get("/balance/status/").status_code, 403)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post("/balance/action/", {"action": "prepare"}).status_code, 403)
        self.tick()
        self.factory.assert_not_called()

    def test_auto_fallback_matches_only_a_fresh_sms_from_the_current_sim_and_sender(self):
        self.prepare()
        self.modem.request_balance.side_effect = [None, None]
        self.action("auto", carrier="telekom-de", sender="2000")
        self.tick()
        self.assertEqual(self.status()["wizard"]["attempts"][0]["state"], "failed")
        self.assertIn("no USSD balance", self.status()["wizard"]["attempts"][0]["detail"])
        self.tick()
        self.assertEqual(self.status()["wizard"]["state"], "waiting")
        self.assertEqual(self.modem.request_balance.call_args.args[0]["transport"], "sms")
        with patch("inbox.sync.queue_forward") as forward, patch("inbox.sync.queue_webhook") as webhook:
            self.import_reply("Balance: 9 EUR", sender="2001")
            self.import_reply("Balance: 8 EUR", age=120)
            self.modem.balance_identity.return_value = ("b" * 64, "26201")
            self.import_reply("Balance: 7 EUR")
            self.assertEqual(self.status()["wizard"]["state"], "waiting")
            self.modem.balance_identity.return_value = (SIM, "26201")
            self.import_reply("Balance: 6 EUR")
        self.assertEqual((forward.call_count, webhook.call_count), (3, 3))
        self.assertEqual(Message.objects.get(is_balance_reply=True).text, "Balance: 6 EUR")
        self.assertEqual(Receipt.objects.filter(cleared_at__isnull=False).count(), 4)
        self.action("enable")
        self.tick()
        result = self.client.get("/api/status/").json()["balance"]
        self.assertEqual((result["enabled"], result["amount"], result["interval_hours"]), (True, "6", 24))

    def test_manual_reply_can_be_recognized_with_a_literal_marker_and_currency(self):
        self.prepare()
        self.modem.request_balance.return_value = "Offer: 12.50. Remaining credit: 5.75"
        self.action("manual", transport="ussd", code="*123#")
        self.tick()
        wizard = self.status()["wizard"]
        self.assertEqual(wizard["state"], "manual_result")
        self.assertIn("Remaining credit", wizard["reply"])
        result = self.action("recognize", currency="EUR", marker="Remaining credit:")["wizard"]
        self.assertEqual((result["state"], result["amount"]), ("verified", "5.75"))
        self.action("enable", interval_hours="4")
        self.tick()
        self.assertEqual(self.status()["summary"]["interval_hours"], 4)

    def test_timeout_and_interrupted_submission_do_not_enable_or_repeat_a_request(self):
        self.prepare()
        self.modem.request_balance.return_value = None
        self.action("manual", transport="sms", destination="2000", sender="2000")
        self.tick()
        BalanceRun.objects.filter(pk="setup").update(deadline=timezone.now() - timedelta(seconds=1))
        self.tick()
        result = self.status()
        self.assertEqual(result["wizard"]["state"], "failed")
        self.assertFalse(result["summary"]["enabled"])
        self.action("manual", transport="sms", destination="2000", sender="2000")
        BalanceRun.objects.filter(pk="setup").update(state="requesting")
        self.tick()
        self.assertIn("may have been submitted", self.status()["wizard"]["detail"])
        self.modem.request_balance.assert_called_once()

    def test_check_failure_keeps_last_confirmed_balance_and_sim_change_suspends_it(self):
        result = self.enable_direct()
        self.assertEqual((result["amount"], result["interval_hours"]), ("12.5", 6))
        self.modem.request_balance.side_effect = [OutboundError("Request failed."), OutboundSimChanged()]
        self.action("check")
        self.tick()
        failure = self.status()["summary"]
        self.assertEqual((failure["amount"], failure["checked_at"]), ("12.5", result["checked_at"]))
        self.assertTrue(failure["stale"])
        self.action("check")
        self.tick()
        self.assertFalse(self.status()["summary"]["enabled"])
        self.assertEqual(BalanceMonitor.objects.get().state, "suspended")

    def test_alert_channels_are_independent_and_send_once_per_low_balance_episode(self):
        SMTPSettings.objects.create(host="smtp.example.invalid", from_email="alerts@example.invalid")
        OutboundProfile.objects.create(connection_id=connection_id(), sim=SIM, profile="at_pdu")
        with patch("inbox.balance_alerts._send_email", side_effect=OSError) as email:
            self.enable_direct(threshold="12.5", phone=PHONE, email="owner@example.invalid")
            self.assertTrue(self.status()["summary"]["low"])
            self.modem.send_sms.assert_called_once()
            self.assertEqual(self.modem.send_sms.call_args.args[:2], ("at_pdu", PHONE))
            email.assert_called_once()
            self.tick()
            for response in ("Balance: 6 EUR", "Balance: 15 EUR", "Balance: 5 EUR"):
                self.modem.request_balance.return_value = response
                self.action("check")
                self.tick()
            self.assertEqual(self.modem.send_sms.call_count, 2)
            self.assertEqual(email.call_count, 2)
        self.action("disable")
        self.assertFalse(self.status()["summary"]["enabled"])
