"""Exercise preserved integrations against local simulators after an image upgrade."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from smoke_install import docker, require
from smoke_integrations import _capture_failure, _expect_auth_error, _verify_rest, check_mcp
from smoke_sms import RUNTIME


REST_KEY = "synthetic-rest-upgrade-key-" + "a" * 32
MCP_KEY = "synthetic-mcp-upgrade-key-" + "b" * 32

# Reuse the wire-protocol fixtures; never replace the upgraded connection settings.
CYCLE_RUNTIME = r'''
import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from django.db import close_old_connections
from inbox.connection import decrypt_secret
from inbox.models import WebhookDelivery, WebhookSettings


class UpgradeModem(FakeModem):
    def before_delete(self, slot):
        try:
            check(slot == 7, "Unexpected upgrade modem slot")
            with sqlite3.connect(str(settings.DATABASES["default"]["NAME"]), timeout=5) as db:
                rows = db.execute(
                    "SELECT id, sender, received_at, text, total_parts, saved_parts "
                    "FROM inbox_message WHERE sender = ?", (EXPECTED_SENDER,)
                ).fetchall()
                check(len(rows) == 1, "Imported SMS was not committed before slot deletion")
                message_id, sender, received, text, total, saved = rows[0]
                check((sender, received, text, total, saved) == (
                    EXPECTED_SENDER, "2026-04-15 01:02:03", EXPECTED_TEXT, 1, 1
                ), "Committed upgraded SMS fields differ from the PDU")
                receipt = db.execute(
                    "SELECT pdu, slot, cleared_at FROM inbox_receipt WHERE message_id = ?",
                    (message_id,),
                ).fetchall()
                check(receipt == [(PDU, slot, None)], "Original PDU was not durable before deletion")
                for table in ("inbox_forwarddelivery", "inbox_webhookdelivery"):
                    delivery = db.execute(
                        f"SELECT sent_at, attempts FROM {table} WHERE message_id = ?", (message_id,)
                    ).fetchall()
                    check(delivery == [(None, 0)], "Delivery queue was not committed before deletion")
        except Exception as error:
            self.errors.append(str(error))
            raise


device = DeviceSettings.objects.get(pk=1)
key = paramiko.RSAKey.from_private_key(io.StringIO(decrypt_secret(device.private_key)))
fake = UpgradeModem(key, host_key=key, port=65534, username="upgrade-device")
smtp_ready, smtp_release = threading.Event(), threading.Event()
smtp = SmtpServer(port=65533, credentials=("upgrade-smtp", "synthetic-smtp-password"),
                  greeting_gate=(smtp_ready, smtp_release))
executor = ThreadPoolExecutor(max_workers=1)
worker_future = None
threading.Thread(target=smtp.serve_forever, daemon=True).start()
hook_requests = []
reject_hook = True


def cycle_state():
    message = Message.objects.get(sender=EXPECTED_SENDER)
    receipt = Receipt.objects.get(message=message)
    email = ForwardDelivery.objects.get(message=message)
    hook = WebhookDelivery.objects.get(message=message)
    check(Message.objects.count() == 3 and Receipt.objects.count() == 4,
          "Upgrade cycle changed historical rows or created duplicate SMS")
    check(ForwardDelivery.objects.count() == WebhookDelivery.objects.count() == 1,
          "Upgrade cycle created unexpected delivery rows")
    check(receipt.pdu == PDU and receipt.cleared_at is not None,
          "Imported PDU or cleared receipt is missing")
    check(SyncStatus.objects.get(pk=1).state == "ok", "Upgraded importer did not report success")
    check(not fake.errors and not fake.slots and fake.delete_callbacks == 1,
          "Upgraded modem protocol or slot cleanup failed")
    check(fake.commands.index("+CMGR=7") < fake.commands.index("+CMGD=7"),
          "Upgraded importer deleted a slot before verifying it")
    return {
        "id": message.pk,
        "email": {"sent_at": email.sent_at.isoformat() if email.sent_at else None,
                  "attempts": email.attempts, "error": bool(email.last_error)},
        "webhook": {"sent_at": hook.sent_at.isoformat() if hook.sent_at else None,
                    "attempts": hook.attempts, "error": bool(hook.last_error)},
    }


def verify_delivery():
    message = Message.objects.get(sender=EXPECTED_SENDER)
    email = ForwardDelivery.objects.get(message=message)
    hook = WebhookDelivery.objects.get(message=message)
    expected_recipients = ["first@example.invalid", "second@example.invalid"]
    check(email.recipients == email.delivered_to == expected_recipients,
          "Preserved SMTP recipients were not delivered exactly once")
    check(len(smtp.messages) == 2 and smtp.authenticated >= 2,
          "Preserved SMTP credentials were not used for authenticated delivery")
    for recipient, (sender, recipients, raw) in zip(expected_recipients, smtp.messages):
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        body = parsed.get_body(preferencelist=("plain",)).get_content().replace("\r\n", "\n")
        check(sender == "sms@example.invalid" and recipients == (recipient,),
              "Upgraded SMTP envelope differs from preserved settings")
        check(parsed["From"] == sender and parsed["To"] == recipient
              and parsed["Subject"] == f"SMS from {EXPECTED_SENDER}",
              "Upgraded email headers are incorrect")
        check(body.rstrip("\n") == (
            f"From: {EXPECTED_SENDER}\nReceived: {EXPECTED_RECEIVED.isoformat()}\n\n{EXPECTED_TEXT}"
        ), "Upgraded email omitted Unicode text or SMS metadata")
    check(len(hook_requests) == 2, "Webhook retry did not make exactly two requests")
    check(hook_requests[0] == hook_requests[1], "Webhook retry changed its payload or idempotency key")
    payload, token, secret, content_type = hook_requests[0]
    check(payload == hook.payload and token == str(hook.token)
          and payload["event_id"] == token and payload["event"] == "sms.received",
          "Webhook event or persisted idempotency token is incorrect")
    check(secret == "synthetic-webhook-upgrade-secret" and content_type.startswith("application/json"),
          "Preserved webhook secret or content type was not sent")
    check(payload["sms"] == {
        "id": message.pk, "source": message.source, "sender": EXPECTED_SENDER,
        "text": EXPECTED_TEXT, "received_at": EXPECTED_RECEIVED.isoformat(),
        "imported_at": message.imported_at.isoformat(),
    }, "Webhook payload omitted SMS metadata or Unicode text")
    receipt = Receipt.objects.get(message=message)
    check(receipt.cleared_at <= email.sent_at and receipt.cleared_at <= hook.sent_at,
          "Delivery completed before durable slot cleanup")


class Control(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        global reject_hook, worker_future
        close_old_connections()
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/upgrade-fixture":
                hook_requests.append((data, self.headers.get("Idempotency-Key"),
                                      self.headers.get("X-Upgrade-Secret"),
                                      self.headers.get("Content-Type", "")))
                self.send_response(503 if reject_hook else 204)
                self.end_headers()
                return
            check(self.path == "/control", "Unexpected fixture request")
            action = data["action"]
            if action == "ready":
                result = {"ready": True}
            elif action == "visibility":
                SMTPSettings.objects.filter(pk=1).update(enabled=data["enabled"])
                WebhookSettings.objects.filter(pk=1).update(enabled=data["enabled"])
                result = {"enabled": data["enabled"]}
            else:
                if action == "import":
                    worker_future = executor.submit(worker, timeout=90)
                    if not smtp_ready.wait(20):
                        if worker_future.done():
                            worker_future.result()
                        check(False, "Upgraded worker did not import and queue the SMS")
                elif action == "fail":
                    smtp_release.set()
                    worker_future.result(timeout=60)
                elif action == "retry":
                    ForwardDelivery.objects.filter(message__sender=EXPECTED_SENDER).update(
                        next_attempt_at=django_timezone.now())
                    WebhookDelivery.objects.filter(message__sender=EXPECTED_SENDER).update(
                        next_attempt_at=django_timezone.now())
                    reject_hook = False
                    worker()
                    verify_delivery()
                elif action == "noop":
                    before = cycle_state()
                    worker()
                    check(before == cycle_state(), "No-op worker changed completed deliveries")
                    verify_delivery()
                else:
                    check(False, "Unknown fixture action")
                result = cycle_state()
                for kind in ("email", "webhook"):
                    item = result[kind]
                    attempts = {"import": int(kind == "email"), "fail": 1, "retry": 2, "noop": 2}[action]
                    check(item["attempts"] == attempts, "Unexpected post-upgrade delivery attempts")
                    check(item["error"] == (action == "fail"), "Delivery retry error state is incorrect")
                    check(bool(item["sent_at"]) == (action in ("retry", "noop")),
                          "Delivery sending time is incorrect")
                if action == "fail":
                    check(not smtp.messages and len(hook_requests) == 1,
                          "Deliberate SMTP or webhook outage did not fail")
            encoded = json.dumps(result).encode()
            self.send_response(200)
        except Exception as error:
            encoded = json.dumps({"error": str(error)}).encode()
            self.send_response(500)
        finally:
            close_old_connections()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


ThreadingHTTPServer(("127.0.0.1", 65532), Control).serve_forever()
'''

CONTROL = r'''
import json, sys, urllib.request, urllib.error
request = urllib.request.Request('http://127.0.0.1:65532/control',
    data=sys.argv[1].encode(), headers={'Content-Type': 'application/json'})
try:
    response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=45)
except urllib.error.HTTPError as error:
    raise RuntimeError(error.read().decode()) from None
print(response.read().decode())
'''


def control(container, action, **values):
    return json.loads(docker(
        "exec", container, "python", "-c", CONTROL,
        json.dumps({"action": action, **values}), timeout=55,
    ))


def start_fixture(container, timeout):
    program = (
        "import traceback\n"
        "try:\n"
        "    scope = {'__name__': 'upgrade_fixture'}\n"
        f"    exec({RUNTIME!r}, scope)\n"
        f"    exec({CYCLE_RUNTIME!r}, scope)\n"
        "except Exception:\n"
        "    traceback.print_exc(file=open('/tmp/upgrade-cycle-error.log', 'w'))\n"
        "    raise\n"
    )
    docker("exec", "--detach", container, "python", "-c", program)
    deadline = time.monotonic() + min(timeout, 30)
    while time.monotonic() < deadline:
        try:
            require(control(container, "ready") == {"ready": True}, "Simulator did not become ready")
            return
        except RuntimeError:
            time.sleep(0.25)
    error = docker("exec", container, "python", "-c",
                   "from pathlib import Path; p=Path('/tmp/upgrade-cycle-error.log'); "
                   "print(p.read_text() if p.exists() else 'No fixture diagnostics')")
    raise RuntimeError("Upgrade simulators did not start: " + error)


def verify_authentication(api, base_url, keys):
    for kind, endpoint in (("rest", "/api/v1/messages/"), ("mcp", "/mcp/")):
        other = keys["mcp" if kind == "rest" else "rest"]
        for key in (None, "invalid-synthetic-upgrade-key", other):
            headers = {} if key is None else {"Authorization": f"Bearer {key}"}
            if kind == "rest":
                response = api.get(base_url + endpoint, headers=headers, timeout=15000)
            else:
                response = api.post(base_url + endpoint, headers=headers,
                                    data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                                    timeout=15000)
            _expect_auth_error(response)


def expect_delivery(page, base_url, state, label):
    from playwright.sync_api import expect

    page.goto(base_url + "/", wait_until="networkidle")
    page.locator("#search").fill("+15551234567")
    row = page.locator(".message-row").filter(has=page.locator(".sender", has_text="+15551234567"))
    expect(row).to_have_count(1)
    row.locator("button.message-open").click()
    for kind, prefix, css_class, reader_id in (
        ("email", "Email", "forwarding", "forwarding"),
        ("webhook", "Webhook", "webhook", "webhook"),
    ):
        for scope in (row, page.locator(f"#reader-{reader_id}")):
            expect(scope.locator(f".{css_class}-label")).to_have_text(f"{prefix} {label}")
            expect(scope.locator(f".{css_class}-label")).to_be_visible()
            timestamp = scope.locator("time.forwarded-at" if kind == "email" else "time.webhook-sent-at")
            sent_at = state[kind]["sent_at"]
            expect(timestamp).to_have_count(1 if sent_at else 0)
            if sent_at:
                expect(timestamp).to_be_visible()
                require(datetime.fromisoformat(timestamp.get_attribute("datetime").replace("Z", "+00:00"))
                        == datetime.fromisoformat(sent_at), "UI sending time differs from the saved delivery")
                expected = page.evaluate(
                    "value => `Sent ${new Date(value).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'})}`",
                    sent_at,
                )
                expect(timestamp).to_have_text(expected)


def verify_cycle(container, base_url, credentials, timeout, artifacts_dir):
    from playwright.sync_api import expect, sync_playwright

    start_fixture(container, timeout)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(locale="en-US", timezone_id="UTC")
        page = context.new_page()
        page.set_default_timeout(15000)
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(type(error).__name__))
        try:
            page.goto(base_url + "/login/", wait_until="networkidle")
            page.get_by_label("Username").fill(credentials["username"])
            page.get_by_label("Password").fill(credentials["password"])
            page.get_by_role("button", name="Sign in").click()
            expect(page.get_by_role("heading", name="Inbox")).to_be_visible()
            api = playwright.request.new_context()
            try:
                _verify_rest(api, base_url, REST_KEY)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(asyncio.run, check_mcp(base_url, MCP_KEY)).result(timeout=60)
                verify_authentication(api, base_url, {"rest": REST_KEY, "mcp": MCP_KEY})
                # Administrator cookies must never substitute for the integration keys.
                verify_authentication(page.request, base_url, {"rest": REST_KEY, "mcp": MCP_KEY})
            finally:
                api.dispose()
            print("PASS: preserved REST and MCP keys authorize SMS CRUD; missing, wrong, cross-key, and session-only requests are rejected", flush=True)
            pending = control(container, "import")
            expect_delivery(page, base_url, pending, "pending")
            failed = control(container, "fail")
            expect_delivery(page, base_url, failed, "error")
            sent = control(container, "retry")
            expect_delivery(page, base_url, sent, "sent")
            print("PASS: upgraded pinned SSH import commits SMS, PDU, and queues before slot cleanup; preserved SMTP and webhook credentials deliver after retries", flush=True)
            require(control(container, "noop") == sent, "Repeated worker changed completed deliveries")
            control(container, "visibility", enabled=False)
            try:
                page.goto(base_url + "/", wait_until="networkidle")
                row = page.locator(".message-row").filter(has=page.locator(".sender", has_text="+15551234567"))
                row.locator("button.message-open").click()
                expect(page.locator(".forwarding-label, .webhook-label, time.forwarded-at, time.webhook-sent-at")).to_have_count(0)
            finally:
                control(container, "visibility", enabled=True)
            expect_delivery(page, base_url, sent, "sent")
            require(not page_errors, "Upgraded UI raised a JavaScript error")
            print("PASS: upgraded inbox and reader show pending, error, sent, and exact sending times; disabled statuses hide; repeat worker is idempotent", flush=True)
        except Exception:
            _capture_failure(page, artifacts_dir, page_errors)
            raise
        finally:
            context.close()
            browser.close()
