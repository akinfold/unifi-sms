import socket
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import paramiko

from modem.client import ModemClient
from modem.ssh import SSHCommandError, read_command


class CommandChannel:
    def __init__(self, stdout=b"", stderr=b"", *, eof=True, status=True, flood=False):
        self.stdout, self.stderr = stdout, stderr
        self.eof_received, self.status_ready = eof, status
        self.flood = flood
        self.closed = False
        self.status_calls = 0

    def settimeout(self, timeout):
        pass

    def exec_command(self, command):
        self.command = command

    def recv_ready(self):
        return bool(self.stdout) or self.flood

    def recv_stderr_ready(self):
        return bool(self.stderr)

    def recv(self, size):
        if self.flood:
            return b"x" * size
        result, self.stdout = self.stdout[:size], self.stdout[size:]
        return result

    def recv_stderr(self, size):
        result, self.stderr = self.stderr[:size], self.stderr[size:]
        return result

    def exit_status_ready(self):
        return self.status_ready

    def recv_exit_status(self):
        if not self.status_ready:
            raise AssertionError("Exit status must never be read before it is ready")
        self.status_calls += 1
        return 0

    def close(self):
        self.closed = True


class SSHDeadlineTests(unittest.TestCase):
    def run_command(self, channel, timeout=0.08, limit=256):
        ssh = MagicMock()
        ssh.get_transport.return_value.open_session.return_value = channel
        return read_command(ssh, "identity", timeout=timeout, limit=limit)

    def test_reads_bounded_stdout_and_stderr_before_exit(self):
        channel = CommandChannel(b"model\n", b"warning\n")
        self.assertEqual(self.run_command(channel), b"model\n")
        self.assertEqual(channel.status_calls, 1)
        self.assertTrue(channel.closed)

    def test_withheld_eof_or_exit_status_times_out_and_closes_channel(self):
        for eof, status in ((False, False), (False, True), (True, False)):
            with self.subTest(eof=eof, status=status):
                channel = CommandChannel(b"model\n", eof=eof, status=status)
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    self.run_command(channel)
                self.assertLess(time.monotonic() - started, 0.6)
                self.assertEqual(channel.status_calls, 0)
                self.assertTrue(channel.closed)

    def test_output_and_stderr_floods_are_rejected_and_closed(self):
        for channel in (CommandChannel(flood=True), CommandChannel(stderr=b"x" * 257)):
            with self.subTest(channel=channel), self.assertRaises(SSHCommandError):
                self.run_command(channel)
            self.assertTrue(channel.closed)
            self.assertEqual(channel.status_calls, 0)

    def test_real_paramiko_peer_withholds_completion_or_exec_acknowledgement(self):
        for behavior in ("no_eof", "no_status", "no_exec_ack"):
            with self.subTest(behavior=behavior):
                self._real_peer_deadline(behavior)

    def test_modem_bootstrap_bounds_exec_acknowledgement_and_ready_wait(self):
        for behavior in ("no_channel_ack", "no_exec_ack", "no_ready"):
            with self.subTest(behavior=behavior):
                self._real_peer_deadline(behavior, bootstrap=True)

    def _real_peer_deadline(self, behavior, bootstrap=False):
        client_socket, server_socket = socket.socketpair()
        release = threading.Event()
        command_received = threading.Event()
        server_transport = paramiko.Transport(server_socket)
        host_key = paramiko.RSAKey.generate(1024)
        server_transport.add_server_key(host_key)

        class Server(paramiko.ServerInterface):
            def check_auth_password(self, username, password):
                return paramiko.AUTH_SUCCESSFUL

            def check_channel_request(self, kind, channel_id):
                if behavior == "no_channel_ack":
                    release.wait(2)
                return paramiko.OPEN_SUCCEEDED

            def check_channel_exec_request(self, channel, command):
                command_received.set()
                if behavior == "no_exec_ack":
                    release.wait(2)
                return True

        def serve():
            try:
                server_transport.start_server(server=Server())
                channel = server_transport.accept(2)
                if channel is not None and command_received.wait(2):
                    channel.sendall(b"model\n")
                    if behavior == "no_status":
                        channel.shutdown_write()
                    elif behavior == "no_eof":
                        channel.send_exit_status(0)
                    release.wait(2)
            except (OSError, EOFError, paramiko.SSHException):
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        ssh = paramiko.SSHClient()
        ssh.get_host_keys().add("synthetic.invalid", "ssh-rsa", host_key)
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            ssh.connect("synthetic.invalid", sock=client_socket, username="test", password="test",
                        look_for_keys=False, allow_agent=False, timeout=2)
            transport = ssh.get_transport()
            started = time.monotonic()
            if bootstrap:
                # Reuse the authenticated, pinned transport to isolate command startup.
                modem = ModemClient(
                    "synthetic.invalid", 22, "test", "", "", timeout=0.15,
                    auth_method="password", password="test",
                    host_keys=f"synthetic.invalid {host_key.get_name()} {host_key.get_base64()}",
                )
                with patch("paramiko.SSHClient", return_value=ssh), patch.object(
                        ssh, "connect", return_value=None):
                    with self.assertRaises(TimeoutError):
                        modem.__enter__()
                self.assertIsNone(modem._ssh)
                self.assertIsNone(modem._channel)
            else:
                with self.assertRaises(TimeoutError):
                    read_command(ssh, "identity", timeout=0.15)
            self.assertLess(time.monotonic() - started, 0.7)
            self.assertFalse(transport.is_active())
        finally:
            release.set()
            ssh.close()
            server_transport.close()
            client_socket.close()
            server_socket.close()
            thread.join(2)
        self.assertFalse(thread.is_alive())
