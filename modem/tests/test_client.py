import unittest
from io import BytesIO, StringIO
from unittest.mock import MagicMock, patch

import paramiko

from modem.client import ModemClient, ModemError, parse_host_keys, parse_private_key


class FakeChannel:
    def __init__(self, response):
        self.chunks = [response[i : i + 3] for i in range(0, len(response), 3)]
        self.sent = []
        self.closed = False

    def sendall(self, value):
        self.sent.append(value)

    def settimeout(self, value):
        pass

    def recv(self, size):
        return self.chunks.pop(0) if self.chunks else b""


class ClientTests(unittest.TestCase):
    def client(self):
        return ModemClient("example.invalid", 22, "user", "key", "known_hosts")

    def test_fragmented_cmgl_with_echo_and_urc(self):
        client = self.client()
        client._channel = FakeChannel(
            b'__UNIFI_SMS_BEGIN__\nAT+CMGL=4\r\n+CMTI: "SM",8\r\n'
            b"+CMGL: 0,1,,23\r\n001122\r\n+CMGL: 2,1,,23\r\nAABBCC\r\nOK\r\n"
        )
        self.assertEqual(client.list_pdus(), {0: "001122", 2: "AABBCC"})
        self.assertEqual(client._channel.sent, ["+CMGL=4\n"])

    def test_cnum_reads_normalizes_and_deduplicates_numbers(self):
        client = self.client()
        client._channel = FakeChannel(
            b'__UNIFI_SMS_BEGIN__\nAT+CNUM\r\n+CMTI: "SM",2\r\n'
            b'+CNUM: "Voice, primary", "15550102001", 145,,4\r\n'
            b'+CNUM: "", "+15550102001",145\r\n'
            b'+CNUM: "Local","020123456",129\r\nOK\r\n'
        )
        self.assertEqual(client.phone_numbers(), ["+15550102001", "020123456"])
        self.assertEqual(client._channel.sent, ["+CNUM\n"])

    def test_cnum_empty_number_is_supported(self):
        for response in (b"", b'+CNUM: "","",129\r\n'):
            client = self.client()
            client._channel = FakeChannel(b"__UNIFI_SMS_BEGIN__\n" + response + b"OK\r\n")
            self.assertEqual(client.phone_numbers(), [])

    def test_cnum_rejects_invalid_responses(self):
        for response in (
            b'+CNUM: "missing number"',
            b'+CNUM: "","123",invalid',
            b'+CNUM: "","<script>",145',
            b'+CNUM: "","unterminated',
            b"unexpected",
            b"ERROR",
        ):
            with self.subTest(response=response):
                client = self.client()
                client._channel = FakeChannel(b"__UNIFI_SMS_BEGIN__\n" + response + b"\r\nOK\r\n")
                with self.assertRaises(ModemError):
                    client.phone_numbers()

    def test_device_model_uses_board_name(self):
        client = self.client()
        stdout = BytesIO(b"U-LTE-Pro-EU\n")
        stdout.channel = MagicMock()
        stdout.channel.recv_exit_status.return_value = 0
        client._ssh = MagicMock()
        client._ssh.exec_command.return_value = (None, stdout, None)
        self.assertEqual(client.device_model(), "U-LTE-Pro-EU")
        command = client._ssh.exec_command.call_args.args[0]
        self.assertIn("board.name", command)
        self.assertIn("/etc/board.info", command)

    def test_device_model_rejects_missing_or_invalid_board_name(self):
        for output in (b"", b"<script>\n", b"model\nother\n"):
            with self.subTest(output=output):
                client = self.client()
                stdout = BytesIO(output)
                stdout.channel = MagicMock()
                stdout.channel.recv_exit_status.return_value = 0
                client._ssh = MagicMock()
                client._ssh.exec_command.return_value = (None, stdout, None)
                with self.assertRaises(ModemError):
                    client.device_model()

    def test_missing_read_and_error(self):
        client = self.client()
        client._channel = FakeChannel(b"__UNIFI_SMS_BEGIN__\n+CMS ERROR: 321\r\n")
        self.assertIsNone(client.read_pdu(0))
        client._channel = FakeChannel(b"__UNIFI_SMS_BEGIN__\nERROR\r\n")
        with self.assertRaises(ModemError):
            client.read_pdu(0)

    def test_malformed_listing_does_not_look_like_empty_storage(self):
        for response in (b"+CMGL: invalid", b"001122", b"unexpected"):
            client = self.client()
            client._channel = FakeChannel(b"__UNIFI_SMS_BEGIN__\n" + response + b"\r\nOK\r\n")
            with self.assertRaises(ModemError):
                client.list_pdus()

    def test_memory_full_is_not_a_missing_slot(self):
        client = self.client()
        client._channel = FakeChannel(b"__UNIFI_SMS_BEGIN__\n+CMS ERROR: 322\r\n")
        with self.assertRaises(ModemError):
            client.read_pdu(0)

    def test_rejects_invalid_slots_before_sending(self):
        client = self.client()
        client._channel = FakeChannel(b"")
        for slot in (-1, True, 1.0, "1"):
            with self.subTest(slot=slot), self.assertRaises(ValueError):
                client.delete(slot)
        self.assertEqual(client._channel.sent, [])

    def test_password_auth_uses_only_password_and_inline_host_key(self):
        key = paramiko.RSAKey.generate(1024)
        host_keys = f"example.invalid {key.get_name()} {key.get_base64()}"
        client = ModemClient(
            "example.invalid", 22, "user", "unused-key", "unused-file",
            auth_method="password", password="test-secret", host_keys=host_keys,
        )
        ssh = MagicMock()
        ssh.connect.side_effect = RuntimeError("stop before modem session")
        with patch("paramiko.SSHClient", return_value=ssh), self.assertRaises(RuntimeError):
            client.__enter__()
        ssh.load_host_keys.assert_not_called()
        self.assertIsInstance(ssh.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)
        self.assertEqual(ssh.get_host_keys().add.call_args.args[0], "example.invalid")
        kwargs = ssh.connect.call_args.kwargs
        self.assertEqual(kwargs["password"], "test-secret")
        self.assertNotIn("key_filename", kwargs)
        self.assertNotIn("pkey", kwargs)
        self.assertFalse(kwargs["look_for_keys"])
        self.assertFalse(kwargs["allow_agent"])

    def test_inline_key_auth_does_not_offer_file_or_password(self):
        key = paramiko.RSAKey.generate(1024)
        stream = StringIO()
        key.write_private_key(stream)
        client = ModemClient(
            "example.invalid", 22, "user", "unused-key", "known_hosts",
            auth_method="private_key", private_key=stream.getvalue(),
        )
        ssh = MagicMock()
        ssh.connect.side_effect = RuntimeError("stop before modem session")
        with patch("paramiko.SSHClient", return_value=ssh), self.assertRaises(RuntimeError):
            client.__enter__()
        ssh.load_host_keys.assert_called_once_with("known_hosts")
        kwargs = ssh.connect.call_args.kwargs
        self.assertIsInstance(kwargs["pkey"], paramiko.RSAKey)
        self.assertNotIn("password", kwargs)
        self.assertNotIn("key_filename", kwargs)
        self.assertFalse(kwargs["look_for_keys"])
        self.assertFalse(kwargs["allow_agent"])

    def test_invalid_inline_material_has_generic_errors(self):
        for parser, value in ((parse_private_key, "secret key text"), (parse_host_keys, "bad host key")):
            with self.subTest(parser=parser.__name__), self.assertRaises(ValueError) as error:
                parser(value)
            self.assertNotIn(value, str(error.exception))


if __name__ == "__main__":
    unittest.main()
