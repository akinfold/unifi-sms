import os
import select
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from modem.client import ModemClient
from modem.tests.test_pdu import make_pdu, septets


class ShellChannel:
    def __init__(self):
        self.closed = False
        self.timeout = 3
        self.process = None

    def set_combine_stderr(self, value):
        pass

    def exec_command(self, script):
        self.process = subprocess.Popen(
            ["sh", "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, value):
        self.process.stdin.write(value.encode())

    def recv(self, size):
        if not select.select([self.process.stdout], [], [], self.timeout)[0]:
            raise socket.timeout()
        return os.read(self.process.stdout.fileno(), size)

    def shutdown_write(self):
        self.process.stdin.close()

    def close(self):
        self.closed = True
        if self.process is not None:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
            self.process.stdin.close()


@unittest.skipUnless(sys.platform == "linux", "The device-side shell uses Linux stty -F")
class SerialSessionTests(unittest.TestCase):
    def test_shell_reads_deletes_and_restores_serial_session(self):
        import pty

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        stop = threading.Event()
        commands = []
        pdu = make_pdu(septets([72, 105]), udl=2)
        slots = {0: pdu}

        def emulate():
            pending = b""
            while not stop.is_set():
                if not select.select([master], [], [], 0.1)[0]:
                    continue
                pending += os.read(master, 4096)
                while b"\r" in pending:
                    command, pending = pending.split(b"\r", 1)
                    command = command.decode().strip()
                    commands.append(command)
                    responses = {
                        "AT+CNUM": '+CNUM: "","+15550102001",145',
                        "AT+CMGF?": "+CMGF: 1",
                        "AT+CMGL=4": f"+CMGL: 0,1,,15\r\n{pdu}",
                        "AT+CMGR=0": f"+CMGR: 1,,15\r\n{pdu}",
                    }
                    if command == "AT+CMGD=0":
                        slots.pop(0)
                    response = responses.get(command, "")
                    os.write(master, ("\r\n" + response + "\r\nOK\r\n").encode())

        thread = threading.Thread(target=emulate, daemon=True)
        thread.start()
        channel = ShellChannel()
        ssh = MagicMock()
        ssh.get_transport.return_value.open_session.return_value = channel
        client = ModemClient(
            "example.invalid", 22, "sms", "key", "known_hosts", device=os.ttyname(slave), timeout=3
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                lock = Path(directory) / "lock"
                script = client._remote_script().replace("/tmp/unifi-sms-modem.lock", str(lock))
                with (
                    patch("paramiko.SSHClient", return_value=ssh),
                    patch.object(client, "_remote_script", return_value=script),
                ):
                    with client:
                        self.assertEqual(client.phone_numbers(), ["+15550102001"])
                        self.assertEqual(client.list_pdus(), {0: pdu})
                        self.assertEqual(client.read_pdu(0), pdu)
                        client.delete(0)
                        self.assertTrue(lock.is_dir())
                self.assertFalse(lock.exists())
                self.assertFalse(slots)
                self.assertEqual(commands[-1], "AT+CMGF=1")
                self.assertEqual(channel.process.returncode, 0)
        finally:
            channel.close()
            stop.set()
            thread.join(timeout=1)
