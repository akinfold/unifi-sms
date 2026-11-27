from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from modem.outbound import OutboundError, OutboundSimChanged, OutboundUnknown
from modem.tests.test_pdu import make_pdu, septets

from .api_keys import make_api_key_hash
from .models import (
    DeviceSettings, IntegrationSettings, Message, OutboundMessage, OutboundProfile, OutboundSetup, Receipt,
)
from .outbound import connection_id, tick
from .sync import sync_once
from .test_helpers import seed_connection

PHONE = "+12025550100"
OTHER = "+12025550101"
SIM = "a" * 64
REST_KEY = "synthetic-api-key-for-outbound-testing"
MCP_KEY = "synthetic-mcp-key-for-outbound-testing"
QMI = "qmi_ims_off"
AT = "at_pdu"


class OutboundTests(TestCase):
    def setUp(self):
        super().setUp()
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(override_settings(DATA_DIR=Path(directory)))
        seed_connection()
        self.admin = get_user_model().objects.create_user("admin", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.factory = MagicMock()
        self.modem = self.factory.return_value.__enter__.return_value
        self.modem.outbound_capabilities.return_value = (SIM, [QMI, AT], PHONE)
        self.modem.send_sms.return_value = "7"
        self.send_args = {"to": OTHER, "text": "Example", "idempotency_key": "send-example"}
        IntegrationSettings.objects.create(
            rest_enabled=True, rest_key_hash=make_api_key_hash(REST_KEY),
            mcp_enabled=True, mcp_key_hash=make_api_key_hash(MCP_KEY),
        )

    def action(self, action, **payload):
        return self.client.post("/send/action/", {"action": action, **payload}, content_type="application/json")

    def status(self):
        return self.client.get("/send/status/").json()

    def run_tick(self):
        tick(client_factory=self.factory)

    def start(self):
        self.assertEqual(self.action("prepare").status_code, 200)
        self.run_tick()
        self.assertEqual(self.status()["detected_number"], PHONE)
        self.assertEqual(self.action("start", phone=PHONE).status_code, 200)

    def import_text(self, text, sender=PHONE):
        pdu = make_pdu(septets([ord(character) for character in text]), sender=sender.removeprefix("+"), udl=len(text))
        self.modem.list_pdus.return_value = {1: pdu}
        self.modem.read_pdu.return_value = pdu
        sync_once(client_factory=self.factory)

    def install_profile(self):
        return OutboundProfile.objects.create(connection_id=connection_id(), sim=SIM, profile=QMI)

    def rest(self, key="rest-request", **payload):
        return self.client.post(
            "/api/v1/outbound/", payload or {"to": OTHER, "text": "REST message"},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {REST_KEY}", HTTP_IDEMPOTENCY_KEY=key,
        )

    def mcp(self, method, params=None):
        return self.client.post("/mcp/", {
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
        }, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {MCP_KEY}").json()["result"]

    def tool_names(self):
        return {tool["name"] for tool in self.mcp("tools/list")["tools"]}

    def tool(self, name, **arguments):
        return self.mcp("tools/call", {"name": name, "arguments": arguments})

    def test_disabled_defaults_and_admin_csrf(self):
        self.assertContains(self.client.get("/"), 'href="/send/"')
        self.assertContains(self.client.get("/send/"), "Set up sending")
        self.assertFalse(self.status()["enabled"])
        self.assertEqual(self.action("send", **self.send_args).status_code, 409)
        self.assertEqual(self.rest().status_code, 403)
        self.assertNotIn("send_sms", self.tool_names())
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.post("/send/action/", {}).status_code, 403)
        self.assertEqual(Client().get("/send/status/").status_code, 403)
        self.modem.send_sms.assert_not_called()

    def test_wizard_after_timeout_ignores_late_receipt_and_verifies_next_profile(self):
        self.start()
        self.run_tick()
        first, _, first_text = self.modem.send_sms.call_args.args
        self.assertEqual(first, QMI)
        self.assertEqual(self.status()["state"], "waiting")
        OutboundSetup.objects.update(deadline=timezone.now() - timedelta(seconds=1))
        self.run_tick()
        self.assertEqual(self.status()["attempts"][0]["state"], "timed_out")
        self.run_tick()
        second, destination, second_text = self.modem.send_sms.call_args.args
        self.assertEqual((second, destination), (AT, PHONE))
        self.assertNotEqual(first_text, second_text)
        with patch("inbox.sync.queue_forward") as forward, patch("inbox.sync.queue_webhook") as webhook:
            self.import_text(first_text)
            self.assertEqual(self.status()["state"], "waiting")
            self.import_text(second_text, OTHER)
            self.assertEqual(self.status()["state"], "waiting")
            self.import_text(second_text)
        self.assertEqual(forward.call_args.args[0].sender, OTHER)
        self.assertEqual(webhook.call_args.args[0].sender, OTHER)
        self.assertEqual((forward.call_count, webhook.call_count), (1, 1))
        self.assertEqual(self.status()["state"], "verified")
        self.assertFalse(self.status()["enabled"])
        self.assertEqual(Message.objects.count(), 3)
        self.assertEqual(Receipt.objects.filter(cleared_at__isnull=False).count(), 3)
        self.assertTrue(self.action("finish").json()["enabled"])
        self.assertEqual(OutboundProfile.objects.get().profile, AT)
        self.assertEqual(self.action("prepare").status_code, 200)
        self.assertEqual(self.action("cancel").json()["state"], "canceled")
        self.assertTrue(self.status()["enabled"])

    def test_definite_rejection_tries_next_and_cancel_stops_test(self):
        self.modem.send_sms.side_effect = [OutboundError("Network rejected the test."), "7"]
        self.start()
        self.run_tick()
        self.assertEqual(self.status()["attempts"][0]["detail"], "Network rejected the test.")
        self.assertEqual(self.action("cancel").status_code, 200)
        self.run_tick()
        self.assertEqual(self.modem.send_sms.call_count, 1)

    def test_all_channels_enqueue_through_the_same_worker(self):
        self.install_profile()
        IntegrationSettings.objects.update(rest_send_enabled=True, mcp_send_enabled=True)
        self.assertIn("send_sms", self.tool_names())
        ui = self.action("send", **self.send_args)
        rest = self.rest()
        mcp = self.tool("send_sms", to=OTHER, text="MCP message", idempotency_key="mcp-example")
        self.assertEqual((ui.status_code, rest.status_code, mcp["isError"]), (202, 202, False))
        job_ids = [ui.json()["job"]["id"], rest.json()["id"], mcp["structuredContent"]["id"]]
        self.assertEqual(self.action("send", **self.send_args).json()["job"]["id"], job_ids[0])
        self.assertEqual(self.rest().json()["id"], job_ids[1])
        self.assertEqual(self.rest(to=PHONE, text="Changed").status_code, 409)
        self.modem.send_sms.assert_not_called()
        for _ in job_ids:
            self.run_tick()
        self.assertEqual(self.modem.send_sms.call_count, 3)
        self.assertEqual(self.client.get("/send/status/", {"job_id": job_ids[0]}).json()["job"]["status"], "accepted")
        response = self.client.get(rest["Location"], HTTP_AUTHORIZATION=f"Bearer {REST_KEY}")
        self.assertEqual(response.json()["modem_reference"], "7")
        self.assertEqual(self.tool("get_sms_send_status", id=job_ids[2])["structuredContent"]["status"], "accepted")
        self.assertTrue(self.tool("get_sms_send_status", id=job_ids[1])["isError"])
        self.assertFalse(OutboundMessage.objects.exclude(text="").exists())

    def test_revoke_and_retrust_block_queued_sends(self):
        self.install_profile()
        IntegrationSettings.objects.update(rest_send_enabled=True)
        self.assertEqual(self.rest().status_code, 202)
        IntegrationSettings.objects.update(rest_key_hash=make_api_key_hash("replacement-key"))
        self.run_tick()
        self.assertEqual(OutboundMessage.objects.get().status, "failed")
        self.assertEqual(self.action("send", **self.send_args).status_code, 202)
        DeviceSettings.objects.update(trusted_at=timezone.now() + timedelta(seconds=1))
        self.run_tick()
        self.assertFalse(self.status()["enabled"])
        self.assertEqual(list(OutboundMessage.objects.values_list("status", flat=True)), ["failed", "failed"])
        self.modem.send_sms.assert_not_called()

    def test_session_failure_before_submission_allows_retry(self):
        self.install_profile()
        self.factory.return_value.__enter__.side_effect = ConnectionError("Synthetic failure")
        response = self.action("send", **self.send_args)
        self.assertEqual(response.status_code, 202)
        self.run_tick()
        job = self.client.get("/send/status/", {"job_id": response.json()["job"]["id"]}).json()["job"]
        self.assertEqual(job["status"], "failed")
        self.assertIn("Could not open the modem session", job["detail"])
        self.modem.send_sms.assert_not_called()

    def test_uncertainty_and_interrupted_submission_are_never_resent(self):
        self.install_profile()
        first = self.action("send", **self.send_args).json()["job"]
        self.modem.send_sms.side_effect = OutboundUnknown("No response")
        self.run_tick()
        self.assertEqual(self.action("send", **self.send_args).json()["job"]["status"], "unknown")
        second = self.action("send", **(self.send_args | {"idempotency_key": "interrupted"})).json()["job"]
        OutboundMessage.objects.filter(pk=second["id"]).update(status="sending")
        self.run_tick()
        self.assertEqual(self.client.get("/send/status/", {"job_id": second["id"]}).json()["job"]["status"], "unknown")
        self.assertEqual(self.modem.send_sms.call_count, 1)
        self.assertNotEqual(first["id"], second["id"])

    def test_sim_change_invalidates_profile_and_rejects_job(self):
        self.install_profile()
        job = self.action("send", **self.send_args).json()["job"]
        self.modem.send_sms.side_effect = OutboundSimChanged("Changed SIM")
        self.run_tick()
        status = self.client.get("/send/status/", {"job_id": job["id"]}).json()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["job"]["status"], "failed")
