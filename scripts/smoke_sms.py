"""Exercise the complete SMS import and email-forwarding cycle in a local image."""

import argparse
import math
import sys
import time
from uuid import uuid4

from smoke_install import docker, require


RUNTIME = r"""import base64
from email import policy
from email.parser import BytesParser
import io
import os
import re
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timezone

import django
import paramiko

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
from django.conf import settings
from django.db import connection
from django.utils import timezone as django_timezone
from inbox.models import (
    DeviceSettings,
    ForwardDelivery,
    Installation,
    Message,
    Receipt,
    SMTPSettings,
    SyncStatus,
)
from inbox.trust import save_known_host


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


# Fixed synthetic SMS-DELIVER PDU; expected fields are asserted independently below.
PDU = "00000B915155214365F70008624051102030002E0053004D005300200073006D006F006B006500200074006500730074003A002000630061006600E90020D83DDE80"
EXPECTED_TEXT = "SMS smoke test: café 🚀"
EXPECTED_SENDER = "+15551234567"
EXPECTED_RECEIVED = datetime(2026, 4, 15, 1, 2, 3, tzinfo=timezone.utc)


class SmtpHandler(socketserver.StreamRequestHandler):
    def reply(self, value):
        self.wfile.write((value + "\r\n").encode("ascii"))
        self.wfile.flush()

    def handle(self):
        self.request.settimeout(15)
        if self.server.greeting_gate is not None:
            ready, release = self.server.greeting_gate
            ready.set()
            for _ in range(30):
                if release.wait(2):
                    break
                self.reply("220-localhost waiting for test delivery")
            else:
                check(False, "Timed out waiting to resume SMTP delivery")
        self.reply("220 localhost ESMTP smoke")
        envelope_from = ""
        recipients = []
        authenticated = self.server.credentials is None
        while True:
            raw = self.rfile.readline(65536)
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            command, _, argument = line.partition(" ")
            command = command.upper()
            if command in ("EHLO", "HELO"):
                self.wfile.write(b"250-localhost\r\n250-AUTH PLAIN\r\n250 SIZE 1048576\r\n")
                self.wfile.flush()
            elif command == "AUTH":
                mechanism, _, value = argument.partition(" ")
                try:
                    supplied = tuple(base64.b64decode(value, validate=True).decode().split("\x00")[1:])
                except (ValueError, UnicodeError):
                    supplied = ()
                authenticated = mechanism.upper() == "PLAIN" and supplied == self.server.credentials
                if authenticated:
                    self.server.authenticated += 1
                self.reply("235 authenticated" if authenticated else "535 invalid credentials")
            elif command == "MAIL" and not authenticated:
                self.reply("530 authentication required")
            elif command == "MAIL":
                match = re.match(r"FROM:\s*<([^>]*)>", argument, re.IGNORECASE)
                check(match is not None, "SMTP MAIL command had no valid address")
                envelope_from = match.group(1)
                recipients = []
                self.reply("250 sender accepted")
            elif command == "RCPT":
                match = re.match(r"TO:\s*<([^>]*)>", argument, re.IGNORECASE)
                check(match is not None, "SMTP RCPT command had no valid address")
                recipients.append(match.group(1))
                self.reply("250 recipient accepted")
            elif command == "DATA":
                self.reply("354 end with dot")
                rows = []
                while True:
                    part = self.rfile.readline(65536)
                    if not part or part in (b".\r\n", b".\n"):
                        break
                    if part.startswith(b".."):
                        part = part[1:]
                    rows.append(part)
                with self.server.lock:
                    if self.server.fail_data:
                        self.server.fail_data = False
                        reject = True
                    else:
                        reject = False
                        self.server.messages.append(
                            (envelope_from, tuple(recipients), b"".join(rows))
                        )
                if reject:
                    self.reply("451 temporary synthetic failure")
                else:
                    self.reply("250 queued")
                envelope_from, recipients = "", []
            elif command == "RSET":
                envelope_from, recipients = "", []
                self.reply("250 reset")
            elif command == "NOOP":
                self.reply("250 ok")
            elif command == "QUIT":
                self.reply("221 bye")
                return
            else:
                self.reply("500 unsupported")


class SmtpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port=0, credentials=None, greeting_gate=None):
        super().__init__(("127.0.0.1", port), SmtpHandler)
        self.credentials = credentials
        self.greeting_gate = greeting_gate
        self.authenticated = 0
        self.lock = threading.Lock()
        self.fail_data = True
        self.messages = []


class SshServer(paramiko.ServerInterface):
    def __init__(self, allowed_key, username="smoke-device"):
        self.allowed_key = allowed_key
        self.username = username
        self.channels = {}
        self.lock = threading.Lock()

    def check_auth_publickey(self, username, key):
        if username == self.username and key.asbytes() == self.allowed_key.asbytes():
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "publickey"

    def check_channel_request(self, kind, channel_id):
        return (
            paramiko.OPEN_SUCCEEDED
            if kind == "session"
            else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        )

    def check_channel_exec_request(self, channel, command):
        with self.lock:
            state = self.channels.setdefault(channel.get_id(), {})
            state["command"] = command.decode("utf-8", "replace")
            state.setdefault("ready", threading.Event()).set()
        return True

    def wait_exec(self, channel, timeout):
        with self.lock:
            state = self.channels.setdefault(channel.get_id(), {"ready": threading.Event()})
            ready = state["ready"]
        if not ready.wait(timeout):
            return ""
        with self.lock:
            return self.channels[channel.get_id()].get("command", "")


class FakeModem:
    def __init__(self, private_key, *, host_key=None, port=0, username="smoke-device"):
        self.private_key = private_key
        self.username = username
        self.host_key = host_key or paramiko.RSAKey.generate(2048)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", port))
        self.listener.listen(8)
        self.listener.settimeout(0.5)
        self.port = self.listener.getsockname()[1]
        self.slots = {7: PDU}
        self.commands = []
        self.errors = []
        self.delete_callbacks = 0
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stop_event.is_set():
            try:
                sock, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.handle_connection, args=(sock,), daemon=True).start()

    def handle_connection(self, sock):
        transport = paramiko.Transport(sock)
        transport.add_server_key(self.host_key)
        interface = SshServer(self.private_key, self.username)
        try:
            transport.start_server(server=interface)
            while transport.is_active() and not self.stop_event.is_set():
                channel = transport.accept(0.25)
                if channel is None:
                    continue
                threading.Thread(
                    target=self.handle_channel,
                    args=(channel, interface),
                    daemon=True,
                ).start()
        except Exception as error:
            if not self.stop_event.is_set():
                self.errors.append(f"{type(error).__name__}: {error}")
        finally:
            transport.close()

    def handle_channel(self, channel, interface):
        try:
            command = interface.wait_exec(channel, 5)
            if command.startswith("sed -n '"):  # The production model probe.
                channel.sendall(b"LTE Pro\n")
                channel.send_exit_status(0)
                channel.close()
            else:
                self.serve_modem_channel(channel)
        except Exception as error:
            if not self.stop_event.is_set():
                self.errors.append(f"{type(error).__name__}: {error}")
        finally:
            channel.close()

    def serve_modem_channel(self, channel):
        channel.settimeout(15)
        channel.sendall(b"__UNIFI_SMS_READY__\n")
        pending = b""
        try:
            while True:
                data = channel.recv(4096)
                if not data:
                    return
                pending += data
                while b"\n" in pending:
                    raw, pending = pending.split(b"\n", 1)
                    command = raw.decode("ascii", "replace").strip()
                    if command == "EXIT":
                        return
                    if not command:
                        continue
                    self.commands.append(command)
                    if command == "+CMGF?":
                        response = ["+CMGF: 1"]
                    elif command in ("+CMGF=0", "+CMGF=1", '+CPMS="SM"'):
                        response = []
                    elif command == "+CNUM":
                        response = ['+CNUM: 1,"+15550001111",145']
                    elif command == "+CMGL=4":
                        response = [
                            line
                            for slot, pdu in sorted(self.slots.items())
                            for line in (f"+CMGL: {slot},1,,{len(pdu) // 2 - 1}", pdu)
                        ]
                    elif command.startswith("+CMGR="):
                        slot = int(command.split("=", 1)[1])
                        response = (
                            [f"+CMGR: 1,,{len(self.slots[slot]) // 2 - 1}", self.slots[slot]]
                            if slot in self.slots
                            else ["+CMS ERROR: 321"]
                        )
                    elif command.startswith("+CMGD="):
                        slot = int(command.split("=", 1)[1])
                        self.before_delete(slot)
                        del self.slots[slot]
                        self.delete_callbacks += 1
                        response = []
                    else:
                        self.errors.append("unexpected modem command")
                        response = ["ERROR"]
                    channel.sendall(
                        (
                            "__UNIFI_SMS_BEGIN__\nAT"
                            + command
                            + "\n"
                            + "\n".join(response)
                            + "\nOK\n"
                        ).encode("ascii")
                    )
        except Exception as error:
            if not self.stop_event.is_set():
                self.errors.append(f"{type(error).__name__}: {error}")

    def before_delete(self, slot):
        check(slot == 7, "Unexpected synthetic modem slot")
        try:
            # A separate SQLite connection proves the archive transaction committed before CMGD.
            db = sqlite3.connect(str(settings.DATABASES["default"]["NAME"]), timeout=5)
            try:
                message = db.execute(
                    "SELECT id, sender, received_at, text, total_parts, saved_parts FROM inbox_message"
                ).fetchall()
                receipt = db.execute(
                    "SELECT message_id, pdu, slot, cleared_at FROM inbox_receipt"
                ).fetchall()
                delivery = db.execute(
                    "SELECT message_id, sent_at, attempts FROM inbox_forwarddelivery"
                ).fetchall()
            finally:
                db.close()
            check(len(message) == 1, "Message was not durably committed before modem deletion")
            row = message[0]
            check(
                row[1] == EXPECTED_SENDER
                and row[2] == "2026-04-15 01:02:03"
                and row[3] == EXPECTED_TEXT
                and row[4:] == (1, 1),
                "Committed SMS fields did not match the synthetic PDU",
            )
            check(
                len(receipt) == 1
                and receipt[0][0] == row[0]
                and receipt[0][1] == PDU
                and receipt[0][2] == slot
                and receipt[0][3] is None,
                "Receipt and original PDU were not durable before deletion",
            )
            check(
                len(delivery) == 1
                and delivery[0][0] == row[0]
                and delivery[0][1] is None
                and delivery[0][2] == 0,
                "Forward delivery was not queued before modem deletion",
            )
        except Exception as error:
            self.errors.append(str(error))
            raise

    def close(self):
        self.stop_event.set()
        self.listener.close()
        self.thread.join(timeout=3)


def worker(timeout=35):
    result = subprocess.run(
        [sys.executable, "manage.py", "runworker", "--once"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    check(result.returncode == 0, "runworker --once failed")


def delivery_state():
    item = ForwardDelivery.objects.get()
    return (
        item.sent_at,
        item.attempts,
        item.last_error,
        list(item.delivered_to),
        list(item.recipients),
    )


def main():
    subprocess.run(
        [sys.executable, "manage.py", "migrate", "--noinput"],
        check=True,
        timeout=45,
        stdout=subprocess.DEVNULL,
    )
    Installation.objects.update_or_create(pk=1, defaults={"completed": True})
    smtp = SmtpServer()
    smtp_thread = threading.Thread(target=smtp.serve_forever, daemon=True)
    smtp_thread.start()
    fake = None
    try:
        client_key = paramiko.RSAKey.generate(2048)
        private_buffer = io.StringIO()
        client_key.write_private_key(private_buffer)
        fake = FakeModem(client_key)
        host_key_line = f"{fake.host_key.get_name()} {fake.host_key.get_base64()}"
        fingerprint = save_known_host("127.0.0.1", fake.port, host_key_line)
        DeviceSettings.objects.update_or_create(
            pk=1,
            defaults={
                "host": "127.0.0.1",
                "port": fake.port,
                "username": "smoke-device",
                "auth_method": "private_key",
                "private_key": private_buffer.getvalue(),
                "public_key": f"{client_key.get_name()} {client_key.get_base64()}",
                "known_hosts": str(settings.DATA_DIR / "known_hosts"),
                "host_keys": "",
                "trusted_at": django_timezone.now(),
                "fingerprint": fingerprint,
                "model": "LTE Pro",
                "name": "Synthetic LTE Pro",
                "address": "127.0.0.1",
            },
        )
        SMTPSettings.objects.update_or_create(
            pk=1,
            defaults={
                "enabled": True,
                "host": "127.0.0.1",
                "port": smtp.server_address[1],
                "username": "",
                "password": "",
                "security": "none",
                "from_email": "sms@example.invalid",
                "recipients": "ops@example.invalid",
            },
        )
        worker()
        check(
            not fake.errors, f"Synthetic SSH/modem server reported a protocol error: {fake.errors}"
        )
        check(
            fake.delete_callbacks == 1 and not fake.slots,
            "The validated modem slot was not deleted exactly once",
        )
        check(
            fake.commands.index("+CMGR=7") < fake.commands.index("+CMGD=7"),
            "Modem slot was deleted before the verification read",
        )
        sync = SyncStatus.objects.get(pk=1)
        check(sync.state == "ok" and sync.succeeded_at is not None, "Sync did not report success")
        check(
            Message.objects.count()
            == Receipt.objects.count()
            == ForwardDelivery.objects.count()
            == 1,
            "The first worker run did not archive exactly one SMS and delivery",
        )
        receipt = Receipt.objects.get()
        delivery = ForwardDelivery.objects.get()
        check(
            receipt.cleared_at is not None and receipt.pdu == PDU,
            "Receipt was not marked cleared with its original PDU",
        )
        check(
            delivery.sent_at is None and delivery.attempts == 1 and delivery.last_error,
            "Synthetic SMTP outage did not persist a retry",
        )
        check(not smtp.messages, "SMTP accepted a message during the deliberate temporary failure")
        print(
            "PASS: migration, pinned Paramiko SSH, real modem command flow, committed-before-delete archive, and SMTP retry",
            flush=True,
        )

        ForwardDelivery.objects.update(next_attempt_at=django_timezone.now())
        worker()
        check(
            not fake.errors and fake.delete_callbacks == 1 and not fake.slots,
            "Retry repeated modem deletion or broke the SSH protocol",
        )
        check(
            Message.objects.count() == 1
            and Receipt.objects.count() == 1
            and ForwardDelivery.objects.count() == 1,
            "Retry created duplicate records",
        )
        sent_at, attempts, last_error, delivered_to, recipients = delivery_state()
        check(
            sent_at is not None and attempts == 2 and not last_error,
            "SMTP retry did not complete delivery",
        )
        check(
            recipients == ["ops@example.invalid"] and delivered_to == recipients,
            "Delivery recipient tracking is incorrect",
        )
        with smtp.lock:
            captured = list(smtp.messages)
        check(len(captured) == 1, "Expected exactly one accepted SMTP message after retry")
        envelope_from, envelope_to, raw_message = captured[0]
        parsed = BytesParser(policy=policy.default).parsebytes(raw_message)
        body = parsed.get_body(preferencelist=("plain",)).get_content().replace("\r\n", "\n")
        check(
            envelope_from == "sms@example.invalid" and envelope_to == ("ops@example.invalid",),
            "SMTP envelope sender or recipient is incorrect",
        )
        check(
            parsed["From"] == "sms@example.invalid" and parsed["To"] == "ops@example.invalid",
            "MIME sender or recipient is incorrect",
        )
        check(parsed["Subject"] == f"SMS from {EXPECTED_SENDER}", "SMS email subject is incorrect")
        check(
            body.rstrip("\n")
            == f"From: {EXPECTED_SENDER}\nReceived: {EXPECTED_RECEIVED.isoformat()}\n\n{EXPECTED_TEXT}",
            "Email body omitted the sender, received timestamp, or full Unicode SMS",
        )
        check(
            receipt.cleared_at <= sent_at,
            "SMS was forwarded before the modem slot was durably cleared",
        )
        print(
            "PASS: SMTP retry delivered one complete Unicode email with correct envelope, headers, and SMS metadata",
            flush=True,
        )

        deletes_before = fake.commands.count("+CMGD=7")
        worker()
        check(
            fake.commands.count("+CMGD=7") == deletes_before == 1 and fake.delete_callbacks == 1,
            "No-op worker deleted a modem slot again",
        )
        check(
            Message.objects.count() == 1
            and Receipt.objects.count() == 1
            and ForwardDelivery.objects.count() == 1,
            "No-op worker created duplicate database rows",
        )
        check(
            ForwardDelivery.objects.get().attempts == 2 and len(smtp.messages) == 1,
            "No-op worker resent the completed email",
        )
        with connection.cursor() as cursor:
            check(
                cursor.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
                "SQLite integrity check failed",
            )
        check(not fake.errors and not fake.slots, "Fake modem retained a slot or recorded an error")
        print(
            "PASS: repeat worker is idempotent; database integrity and all final state checks passed",
            flush=True,
        )
    finally:
        if fake is not None:
            fake.close()
        smtp.shutdown()
        smtp.server_close()
        smtp_thread.join(timeout=3)


if __name__ == "__main__":
    main()
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", required=True, help="Locally available image to test; never pulled"
    )
    parser.add_argument(
        "--timeout", type=float, default=240,
        help="Maximum test runtime in seconds, excluding cleanup",
    )
    args = parser.parse_args()
    require(
        math.isfinite(args.timeout) and args.timeout > 0,
        "The timeout must be a finite positive number",
    )
    work_deadline = time.monotonic() + args.timeout

    def docker_with_deadline(*docker_args, input=None, until=work_deadline):
        remaining = until - time.monotonic()
        require(remaining > 0, "The total smoke-test timeout expired")
        return docker(*docker_args, timeout=max(0.01, remaining), input=input)

    name = f"unifi-sms-sms-smoke-{uuid4().hex[:12]}"
    volume = f"{name}-data"
    volume_created = False
    try:
        docker_with_deadline("image", "inspect", args.image)
        docker_with_deadline("volume", "create", volume)
        volume_created = True
        output = docker_with_deadline(
            "run",
            "--interactive",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--mount",
            f"type=volume,source={volume},target=/data",
            "--stop-timeout",
            "2",
            "--entrypoint",
            "python",
            args.image,
            "-",
            input=RUNTIME,
        )
        if output:
            print(output)
        for marker in (
            "PASS: migration, pinned Paramiko SSH, real modem command flow, committed-before-delete archive, and SMTP retry",
            "PASS: SMTP retry delivered one complete Unicode email with correct envelope, headers, and SMS metadata",
            "PASS: repeat worker is idempotent; database integrity and all final state checks passed",
        ):
            require(marker in output, f"Container omitted required success marker: {marker}")
    finally:
        cleanup_errors = []
        if volume_created:
            try:
                containers = docker(
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"name=^{name}$",
                    timeout=10,
                )
                if containers:
                    docker("rm", "--force", name, timeout=10)
            except Exception as error:
                cleanup_errors.append(str(error))
            try:
                docker("volume", "rm", volume, timeout=15)
            except Exception as error:
                cleanup_errors.append(str(error))
        if cleanup_errors:
            for error in cleanup_errors:
                print(f"Cleanup failed: {error}", file=sys.stderr)
            if sys.exc_info()[0] is None:
                raise RuntimeError("Could not remove the disposable smoke-test resources")
        else:
            print("Removed temporary SMS smoke-test resources.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"SMS cycle smoke test failed: {error}", file=sys.stderr)
        sys.exit(1)
