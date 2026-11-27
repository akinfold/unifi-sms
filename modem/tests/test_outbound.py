import hashlib
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from modem.client import ATRejected, ModemClient
from modem.outbound import OutboundError, OutboundSimChanged, OutboundUnknown, submit_pdu, validate_text
from modem.tests.test_client import FakeChannel


PHONE = "+15550102003"
ICCID = "8900000000000000001"
SIM = hashlib.sha256(ICCID.encode()).hexdigest()
PDU = "0001000B915155102000F3000005E8329BFD06"


class EncodingTests(unittest.TestCase):
    def test_submit_pdu_with_and_without_service_center(self):
        self.assertEqual(submit_pdu(PHONE, "hello").hex().upper(), PDU)
        pdu = submit_pdu(PHONE, "hello", ("12345", 145))
        self.assertEqual(pdu.hex().upper(), "04912143F5" + PDU[2:])
        self.assertTrue(submit_pdu(PHONE, "Я").hex().endswith("000802042f"))
        self.assertTrue(submit_pdu(PHONE, "^").hex().endswith("0000021b0a"))

    def test_segment_limits_count_extensions_and_utf16_code_units(self):
        for text in ["a" * 160, "^" * 80, "Я" * 70, "😀" * 35]:
            validate_text(text)
        for text in ["", "  ", "a" * 161, "^" * 81, "Я" * 71, "😀" * 36, "\ud800"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_text(text)
        for phone in ["1555", "+0001", "+１２３４", "+1;pwd"]:
            with self.assertRaises(ValueError):
                submit_pdu(phone, "hello")


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.client = ModemClient("example.invalid", 22, "sms", "key", "known_hosts")

    def setup_session(self, *responses):
        data = b"".join(b"__UNIFI_SMS_BEGIN__\n" + response for response in responses)
        self.client._channel = FakeChannel(data)
        return self.client._channel

    def test_iccid_standard_file_fallback(self):
        self.setup_session(b"ERROR\r\n", b'+CRSM: 144,0,"980000000000000010F0"\r\nOK\r\n')
        expected = hashlib.sha256(b"8900000000000000010").hexdigest()
        self.assertEqual(self.client._sim_identity(), expected)

    def test_sim_change_prevents_cmgs_and_raw_send(self):
        for profile, responses in [
            ("at_pdu", []),
            ("qmi_ims_on", [b'+CSCA: "+12345",145\r\nOK\r\n']),
        ]:
            channel = self.setup_session(*responses, f"+CCID: {ICCID}\r\nOK\r\n".encode())
            wms = Mock()
            with (
                patch("modem.client.open_wms", return_value=nullcontext(wms)),
                self.assertRaises(OutboundSimChanged),
            ):
                self.client.send_sms(profile, PHONE, "hello", expected_sim="other")
            self.assertFalse(any(c.startswith("+CMGS") for c in channel.sent))
            wms.send.assert_not_called()

    def test_at_prompt_and_reference(self):
        channel = self.setup_session(
            f"+CCID: {ICCID}\r\nOK\r\n".encode(),
            b'AT+CMGS=18\r\n+CMTI: "SM",2\r\n> ',
        )
        channel.chunks.append(b"\r\n+CMGS: 17\r\nOK\r\n")
        self.assertEqual(self.client.send_sms("at_pdu", PHONE, "hello", expected_sim=SIM), "17")
        self.assertEqual(channel.sent, ["+CCID\n", "+CMGS=18\n", f"DATA:{PDU}\n"])

    def test_terminal_errors_and_lost_replies(self):
        for response, expected, submissions in [
            (b"ERROR\r\n", OutboundError, 0),
            (b"", OutboundError, 0),
            (b"> ", OutboundUnknown, 1),
            (b"> \r\nOK\r\n", OutboundUnknown, 1),
            (b"> \r\n+CMS ERROR: 332\r\n", OutboundUnknown, 1),
            (b"> \r\n+CMS ERROR: 350\r\n", OutboundError, 1),
        ]:
            with self.subTest(response=response):
                channel = self.setup_session(f"+CCID: {ICCID}\r\nOK\r\n".encode(), response)
                with self.assertRaises(expected):
                    self.client.send_sms("at_pdu", PHONE, "hello", expected_sim=SIM)
                self.assertEqual(sum(c.startswith("DATA:") for c in channel.sent), submissions)

    def test_discovery_never_submits(self):
        wms = Mock()
        wms.subscriber_number.return_value = PHONE
        channel = self.setup_session(
            f"+CCID: {ICCID}\r\nOK\r\n".encode(),
            b"OK\r\n",
            b"OK\r\n",
            b'+CSCA: "+12345",145\r\nOK\r\n',
        )
        with patch("modem.client.open_wms", return_value=nullcontext(wms)):
            self.assertEqual(self.client.outbound_capabilities(), (
                SIM, ["qmi_ims_off", "qmi_ims_on", "at_pdu"], PHONE
            ))
        self.assertIn("+CMGS=?\n", channel.sent)
        wms.send.assert_not_called()

    def test_qmi_absent_and_at_rejected_offer_no_profile(self):
        self.setup_session(
            f"+CCID: {ICCID}\r\nOK\r\n".encode(), b"ERROR\r\n", b"ERROR\r\n", b"ERROR\r\n"
        )
        self.assertEqual(self.client.outbound_capabilities(), (SIM, [], ""))

    def test_qmi_selects_explicit_ims_flag_once(self):
        for profile, ims in [("qmi_ims_off", False), ("qmi_ims_on", True)]:
            wms = Mock()
            wms.send.return_value = "19"
            self.setup_session(b'+CSCA: "+12345",145\r\nOK\r\n', f"+CCID: {ICCID}\r\nOK\r\n".encode())
            with patch("modem.client.open_wms", return_value=nullcontext(wms)):
                self.assertEqual(self.client.send_sms(profile, PHONE, "hello", expected_sim=SIM), "19")
            wms.send.assert_called_once_with(bytes.fromhex("04912143F5" + PDU[2:]), ims)

    def test_sim_read_errors_hide_device_diagnostics(self):
        with patch.object(self.client, "_request", side_effect=ATRejected("sensitive modem data")):
            with self.assertRaisesRegex(OutboundError, "Could not inspect"):
                self.client.outbound_capabilities()
