from io import StringIO
from unittest import TestCase
from unittest.mock import MagicMock, patch

import paramiko

from .probe import InspectHostKey, ProbeError, probe_connection
from .test_helpers import TEST_HOST_KEY


class DeviceProbeTests(TestCase):
    def setUp(self):
        self.values = {"host": "device.example.invalid", "port": 22, "username": "sms", "auth_method": "password", "password": "synthetic-password", "private_key": ""}
        self.ssh = MagicMock()
        transport = self.ssh.get_transport.return_value
        transport.get_remote_server_key.return_value = TEST_HOST_KEY
        transport.sock.getpeername.return_value = ("10.20.30.40", 22)
        self.modem = MagicMock()
        self.modem.__enter__.return_value = self.modem
        self.modem.phone_numbers.return_value = ["+15550102001"]
        self.client_patch = patch("inbox.probe.paramiko.SSHClient", return_value=self.ssh)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.modem_patch = patch("inbox.probe.ModemClient", return_value=self.modem)
        self.factory = self.modem_patch.start()
        self.addCleanup(self.modem_patch.stop)
        self.command_patch = patch("inbox.probe._read_command", side_effect=["U-LTE-Pro-EU", "LTE device"])
        self.command = self.command_patch.start()
        self.addCleanup(self.command_patch.stop)

    def test_probe_reports_regional_model_and_pins_optional_phone_connection(self):
        result = probe_connection(self.values)
        self.assertEqual(result["model"], "U-LTE-Pro-EU")
        self.assertEqual(result["address"], "10.20.30.40")
        self.assertEqual(result["numbers"], ["+15550102001"])
        self.assertIn(result["host_key"], self.factory.call_args.kwargs["host_keys"])
        self.assertIsInstance(self.ssh.set_missing_host_key_policy.call_args.args[0], InspectHostKey)
        self.ssh.load_system_host_keys.assert_not_called()
        self.ssh.save_host_keys.assert_not_called()
        self.modem.list_pdus.assert_not_called()
        self.modem.delete.assert_not_called()
        self.assertFalse(self.ssh.connect.call_args.kwargs["allow_agent"])
        self.assertFalse(self.ssh.connect.call_args.kwargs["look_for_keys"])
        self.ssh.close.assert_called_once()

    def test_pasted_private_key_is_used_without_password_or_agent(self):
        stream = StringIO()
        TEST_HOST_KEY.write_private_key(stream)
        self.values.update(auth_method="private_key", password="", private_key=stream.getvalue())
        probe_connection(self.values)
        self.assertNotIn("password", self.ssh.connect.call_args.kwargs)
        self.assertEqual(self.ssh.connect.call_args.kwargs["pkey"].asbytes(), TEST_HOST_KEY.asbytes())

    def test_phone_failure_does_not_block_identity_or_leak_output(self):
        self.modem.phone_numbers.side_effect = RuntimeError("private serial output")
        result = probe_connection(self.values)
        self.assertTrue(result["phone_unavailable"])
        self.assertEqual(result["numbers"], [])
        self.assertNotIn("private serial output", str(result))

    def test_unsupported_model_is_rejected_before_modem_commands(self):
        self.command.side_effect = ["Generic Router"]
        with self.assertRaises(ProbeError):
            probe_connection(self.values)
        self.factory.assert_not_called()
        self.ssh.close.assert_called_once()

    def test_authentication_failure_does_not_reveal_transport_message(self):
        self.ssh.connect.side_effect = paramiko.AuthenticationException("private output")
        with self.assertRaises(ProbeError) as error:
            probe_connection(self.values)
        self.assertNotIn("private output", str(error.exception))
        self.factory.assert_not_called()
        self.ssh.close.assert_called_once()
