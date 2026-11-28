import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from modem.balance import balance_pdu, parse_cusd
from modem.client import ModemClient
from modem.outbound import OutboundError, OutboundSimChanged, OutboundUnknown, submit_pdu
from modem.tests.test_client import FakeChannel
from modem.tests.test_outbound import ICCID, SIM


class BalanceEncodingTests(unittest.TestCase):
    def test_short_destination_and_blank_body(self):
        self.assertEqual(balance_pdu("123", "").hex(), "000100038121f3000000")
        self.assertEqual(balance_pdu("+15550102003", "hello"),
                         submit_pdu("+15550102003", "hello"))
        self.assertEqual(balance_pdu("123", "BAL").hex(), "000100038121f3000003c22013")
        for destination in ("", "123;AT", "１２３", "1\nAT", "+", "1" * 16):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                balance_pdu(destination, "")
        with self.assertRaises(ValueError):
            submit_pdu("123", "")

    def test_plain_and_ucs2_balance_replies(self):
        self.assertEqual(parse_cusd('+CUSD: 0,"Balance GBP 12.34, expires soon",15'),
                         "Balance GBP 12.34, expires soon")
        text = "Balance £12.34"
        self.assertEqual(parse_cusd(f'+CUSD: 0,"{text.encode("utf-16-be").hex()}",72'), text)
        self.assertIsNone(parse_cusd("+CUSD: 2"))
        for line in ('+CUSD: 1,"Choose 1",15', '+CUSD: 4', '+CUSD: 5',
                     '+CUSD: 0,"broken",72', '+CUSD: 0,"text",245'):
            with self.subTest(line=line), self.assertRaises(OutboundError):
                parse_cusd(line)


class BalanceTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = ModemClient("example.invalid", 22, "sms", "key", "known_hosts")

    def setup_session(self, *responses):
        self.client._channel = FakeChannel(b"".join(
            b"__UNIFI_SMS_BEGIN__\n" + response for response in responses
        ))
        return self.client._channel

    def test_identity_only_returns_home_hint_and_fingerprint(self):
        self.setup_session(f"+CCID: {ICCID}\r\nOK\r\n".encode(),
                           b"234150000000001\r\nOK\r\n")
        self.assertEqual(self.client.balance_identity(), (SIM, "23415"))

    def test_missing_imsi_does_not_prevent_sim_identity(self):
        self.setup_session(f"+CCID: {ICCID}\r\nOK\r\n".encode(), b"ERROR\r\n")
        self.assertEqual(self.client.balance_identity(), (SIM, ""))

    def test_ussd_waits_for_reply_after_ok_then_cancels_and_restores(self):
        channel = self.setup_session(
            b"OK\r\n", f"+CCID: {ICCID}\r\nOK\r\n".encode(),
            b"+CUSD: 0\r\nOK\r\n",
            b'OK\r\n+CUSD: 0,"Balance GBP 12.34",15\r\n',
            b"OK\r\n", b"OK\r\n",
        )
        self.assertEqual(self.client.request_balance(
            {"transport": "ussd", "code": "*123#"}, expected_sim=SIM), "Balance GBP 12.34")
        self.assertEqual(channel.sent,
                         ["+CUSD=?\n", "+CCID\n", "+CUSD?\n", '+CUSD=1,"*123#",15\n',
                          "+CUSD=2\n", "+CUSD=0\n"])

    def test_ussd_uncertainty_never_replays(self):
        channel = self.setup_session(
            b"OK\r\n", f"+CCID: {ICCID}\r\nOK\r\n".encode(),
            b"+CUSD: 1\r\nOK\r\n", b"OK\r\n",
        )
        with self.assertRaises(OutboundUnknown):
            self.client.request_balance({"transport": "ussd", "code": "*123#"}, expected_sim=SIM)
        self.assertEqual(channel.sent.count('+CUSD=1,"*123#",15\n'), 1)
        self.assertEqual(channel.sent[-1], "+CUSD=2\n")

    def test_sim_mismatch_prevents_every_send(self):
        for transport in ("sms", "ussd"):
            self.client._channel = FakeChannel(b"")
            with patch.object(self.client, "_request", return_value=[]), \
                    patch.object(self.client, "_check_sim", side_effect=OutboundSimChanged), \
                    patch.object(self.client, "_send_at") as send, \
                    self.assertRaises(OutboundSimChanged):
                self.client.request_balance({"transport": transport, "code": "*123#",
                                             "destination": "123", "text": ""},
                                            expected_sim=SIM, sms_profile="at_pdu")
            send.assert_not_called()
            self.assertEqual(self.client._channel.sent, [])

    def test_sms_reuses_profile_or_defaults_to_qmi_off_without_retry(self):
        for selected, expected_ims in ((None, False), ("qmi_ims_on", True)):
            self.client._channel = FakeChannel(b"")
            wms = Mock()
            with patch("modem.client.open_wms", return_value=nullcontext(wms)), \
                    patch.object(self.client, "_sms_center", return_value=("12345", 145)), \
                    patch.object(self.client, "_check_sim"):
                self.assertIsNone(self.client.request_balance(
                    {"transport": "sms", "destination": "123", "text": ""},
                    expected_sim=SIM, sms_profile=selected))
                self.assertEqual(wms.send.call_args.args[1], expected_ims)
                wms.send.assert_called_once()
                wms.send.reset_mock()
                wms.send.side_effect = OutboundUnknown("Uncertain submission")
                with self.assertRaises(OutboundUnknown):
                    self.client.request_balance(
                        {"transport": "sms", "destination": "123", "text": ""},
                        expected_sim=SIM, sms_profile=selected)
                wms.send.assert_called_once()

    def test_invalid_or_manual_profiles_never_access_modem(self):
        self.client._channel = FakeChannel(b"")
        for profile in ({"transport": "ussd", "code": '*123#";AT'},
                        {"transport": "ussd", "code": "*123#", "timeout": 61},
                        {"transport": "code", "code": "123"},
                        {"transport": "sms", "destination": "123;AT"}):
            with self.subTest(profile=profile), self.assertRaises((OutboundError, ValueError)):
                self.client.request_balance(profile, expected_sim=SIM)
        self.assertEqual(self.client._channel.sent, [])

    def test_only_default_route_falls_back_after_preparation_failure(self):
        self.client._channel = FakeChannel(b"")
        for verified in (None, "qmi_ims_off"):
            with patch("modem.client.open_wms", side_effect=RuntimeError("Unsupported WMS")), \
                    patch.object(self.client, "_sms_center", return_value=("12345", 145)), \
                    patch.object(self.client, "_check_sim"), \
                    patch.object(self.client, "_send_at") as send:
                profile = {"transport": "sms", "destination": "123", "text": ""}
                if verified is None:
                    self.assertIsNone(self.client.request_balance(
                        profile, expected_sim=SIM, sms_profile=verified))
                    send.assert_called_once_with(balance_pdu("123", ""))
                else:
                    with self.assertRaises(OutboundError):
                        self.client.request_balance(profile, expected_sim=SIM, sms_profile=verified)
                    send.assert_not_called()

    def test_code_acknowledgement_returns_for_backend_sms_wait(self):
        channel = self.setup_session(
            b"OK\r\n", f"+CCID: {ICCID}\r\nOK\r\n".encode(),
            b"+CUSD: 0\r\nOK\r\n", b"OK\r\n", b"OK\r\n",
        )
        self.assertIsNone(self.client.request_balance(
            {"transport": "code", "code": "*123#", "timeout": 90}, expected_sim=SIM))
        self.assertEqual(channel.sent.count('+CUSD=1,"*123#",15\n'), 1)
        self.assertNotIn("+CUSD=2\n", channel.sent)
        self.assertEqual(channel.sent[-1], "+CUSD=0\n")

    def test_code_rejections_fail_without_starting_sms_wait(self):
        for response in (b"ERROR\r\n", b"+CME ERROR: 4\r\n", b"+CUSD: 4\r\nOK\r\n"):
            channel = self.setup_session(
                b"OK\r\n", f"+CCID: {ICCID}\r\nOK\r\n".encode(),
                b"+CUSD: 1\r\nOK\r\n", response, b"OK\r\n",
            )
            with self.subTest(response=response), self.assertRaises(OutboundError):
                self.client.request_balance(
                    {"transport": "code", "code": "*123#"}, expected_sim=SIM)
            self.assertEqual(channel.sent.count('+CUSD=1,"*123#",15\n'), 1)
            self.assertEqual(channel.sent[-1], "+CUSD=2\n")

    def test_code_missing_acknowledgement_is_unknown_and_never_replayed(self):
        channel = self.setup_session(
            b"OK\r\n", f"+CCID: {ICCID}\r\nOK\r\n".encode(),
            b"+CUSD: 1\r\nOK\r\n", b"",
        )
        with self.assertRaises(OutboundUnknown):
            self.client.request_balance(
                {"transport": "code", "code": "*123#"}, expected_sim=SIM)
        self.assertEqual(channel.sent.count('+CUSD=1,"*123#",15\n'), 1)
        self.assertEqual(channel.sent[-1], "+CUSD=2\n")
