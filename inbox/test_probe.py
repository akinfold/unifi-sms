import socket
import threading
from io import StringIO
from unittest import TestCase
from unittest.mock import MagicMock, patch

import paramiko

from .probe import ProbeError, inspect_host_key, probe_connection
from .test_helpers import TEST_HOST_KEY


class DeviceProbeTests(TestCase):
    def setUp(self):
        self.values = {"host": "device.example.invalid", "port": 22, "username": "sms", "auth_method": "password", "password": "synthetic-password", "private_key": ""}
        self.host_key = f"{TEST_HOST_KEY.get_name()} {TEST_HOST_KEY.get_base64()}"
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
        result = probe_connection(self.values, host_key=self.host_key)
        self.assertEqual(result["model"], "U-LTE-Pro-EU")
        self.assertEqual(result["address"], "10.20.30.40")
        self.assertEqual(result["numbers"], ["+15550102001"])
        self.assertIn(result["host_key"], self.factory.call_args.kwargs["host_keys"])
        self.assertIsInstance(self.ssh.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)
        self.ssh.get_host_keys.return_value.add.assert_called_once_with(
            "device.example.invalid", TEST_HOST_KEY.get_name(), TEST_HOST_KEY)
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
        probe_connection(self.values, host_key=self.host_key)
        self.assertNotIn("password", self.ssh.connect.call_args.kwargs)
        self.assertEqual(self.ssh.connect.call_args.kwargs["pkey"].asbytes(), TEST_HOST_KEY.asbytes())

    def test_phone_failure_does_not_block_identity_or_leak_output(self):
        self.modem.phone_numbers.side_effect = RuntimeError("private serial output")
        result = probe_connection(self.values, host_key=self.host_key)
        self.assertTrue(result["phone_unavailable"])
        self.assertEqual(result["numbers"], [])
        self.assertNotIn("private serial output", str(result))

    def test_unsupported_model_is_rejected_before_modem_commands(self):
        self.command.side_effect = ["Generic Router"]
        with self.assertRaises(ProbeError):
            probe_connection(self.values, host_key=self.host_key)
        self.factory.assert_not_called()
        self.ssh.close.assert_called_once()

    def test_authentication_failure_does_not_reveal_transport_message(self):
        self.ssh.connect.side_effect = paramiko.AuthenticationException("private output")
        with self.assertRaises(ProbeError) as error:
            probe_connection(self.values, host_key=self.host_key)
        self.assertNotIn("private output", str(error.exception))
        self.factory.assert_not_called()
        self.ssh.close.assert_called_once()

    def test_host_key_inspection_never_authenticates(self):
        sock = MagicMock()
        sock.getpeername.return_value = ("10.20.30.40", 22)
        transport = MagicMock()
        transport.get_remote_server_key.return_value = TEST_HOST_KEY
        with patch("inbox.probe.socket.create_connection", return_value=sock), patch(
                "inbox.probe.paramiko.Transport", return_value=transport):
            result = inspect_host_key(self.values)
        self.assertEqual(result["host_key"], self.host_key)
        self.assertFalse(result["identity_verified"])
        transport.start_client.assert_called_once_with(timeout=6)
        transport.auth_password.assert_not_called()
        transport.auth_publickey.assert_not_called()
        self.ssh.connect.assert_not_called()
        self.command.assert_not_called()
        self.factory.assert_not_called()
        transport.close.assert_called_once()
        sock.close.assert_called_once()

    def test_changed_key_is_rejected_before_password_authentication(self):
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(client_sock.close)
        self.addCleanup(server_sock.close)
        server = paramiko.Transport(server_sock)
        server.add_server_key(paramiko.RSAKey.generate(2048))
        self.addCleanup(server.close)
        passwords = []

        class Server(paramiko.ServerInterface):
            def check_auth_password(self, username, password):
                passwords.append(password)
                return paramiko.AUTH_SUCCESSFUL

        rejected_keys = []

        def serve():
            try:
                server.start_server(server=Server())
            except (paramiko.SSHException, EOFError, OSError):
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.client_patch.stop()
        real_client = paramiko.SSHClient()
        connect = real_client.connect
        def connect_with_socket(*args, **kwargs):
            try:
                return connect(*args, sock=client_sock, **kwargs)
            except paramiko.BadHostKeyException as error:
                rejected_keys.append(error)
                raise

        with patch.object(real_client, "connect", side_effect=connect_with_socket), patch(
                "inbox.probe.paramiko.SSHClient", return_value=real_client):
            with self.assertRaises(ProbeError):
                probe_connection(self.values, host_key=self.host_key)
        server.close()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(rejected_keys), 1)
        self.assertEqual(passwords, [])
        self.factory.assert_not_called()
