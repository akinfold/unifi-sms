"""Exercise REST, MCP, and webhook integrations in an isolated Docker image."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
from uuid import uuid4

from smoke_install import docker, require, wait_healthy


FEATURE_PROBE = r"""
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from django.db import connection
from django.urls import resolve
from inbox import api_views, mcp_views, webhooks
from inbox.models import IntegrationSettings, WebhookDelivery, WebhookSettings
assert callable(api_views.messages) and callable(api_views.message)
assert callable(mcp_views.endpoint)
assert callable(webhooks.queue_webhook) and callable(webhooks.deliver_webhooks)
assert resolve('/api/v1/messages/').func.__module__ == 'inbox.api_views'
assert resolve('/api/v1/messages/1/').func.__module__ == 'inbox.api_views'
assert resolve('/mcp/').func.__module__ == 'inbox.mcp_views'
tables = set(connection.introspection.table_names())
assert {IntegrationSettings._meta.db_table, WebhookSettings._meta.db_table,
        WebhookDelivery._meta.db_table} <= tables
print('supported')
"""

SEED = r"""
import json, os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from django.contrib.auth.models import User
from django.utils import timezone
from inbox.models import (DeviceSettings, Installation, IntegrationSettings,
                          Message, WebhookDelivery, WebhookSettings)
fixture = json.load(sys.stdin)
assert Message.objects.count() == 0
assert WebhookDelivery.objects.count() == 0
assert not IntegrationSettings.objects.exists()
assert not WebhookSettings.objects.exists()
User.objects.create_superuser(fixture['username'], fixture['email'], fixture['password'])
Installation.objects.update_or_create(pk=1, defaults={'completed': True})
DeviceSettings.objects.update_or_create(pk=1, defaults={
    'host': 'synthetic.invalid', 'username': 'acceptance', 'trusted_at': timezone.now(),
})
print('seeded')
"""

CREATE_WEBHOOK_SMS = r"""
import json, os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from django.utils import timezone
from datetime import datetime
from inbox.models import Message
from inbox.webhooks import queue_webhook
fixture = json.load(sys.stdin)
message = Message.objects.create(
    source='synthetic-acceptance', sender=fixture['sender'],
    text=fixture['text'], received_at=timezone.make_aware(
        datetime(2020, 1, 2, 3, 4, 5), timezone.get_current_timezone()
    ),
)
delivery = queue_webhook(message)
assert delivery is not None
print(json.dumps({'message_id': message.pk, 'delivery_id': delivery.pk}))
"""

DELIVER_WEBHOOK = r"""
import json, os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from django.utils import timezone
from inbox.models import Message, WebhookDelivery
from inbox.webhooks import deliver_webhooks
fixture = json.load(sys.stdin)
delivery = WebhookDelivery.objects.get(message__sender=fixture['sender'])
if fixture['retry']:
    delivery.next_attempt_at = timezone.now()
    delivery.save(update_fields=['next_attempt_at'])
pending = WebhookDelivery.objects.filter(sent_at__isnull=True).count()
assert pending == 1, 'Expected only the one synthetic delivery to remain pending'
deliver_webhooks()
delivery.refresh_from_db()
print(json.dumps({
    'attempts': delivery.attempts,
    'sent_at': delivery.sent_at.isoformat() if delivery.sent_at else None,
    'last_error': delivery.last_error,
}))
"""

RECEIVER = r"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

requests = []
response_status = 503
lock = threading.Lock()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, status, body=b''):
        self.send_response(status)
        if body:
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path != '/requests':
            self._send(404)
            return
        with lock:
            body = json.dumps(requests).encode()
        self._send(200, body)

    def do_POST(self):
        global response_status
        length = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(length)
        if self.path == '/control':
            try:
                status = json.loads(body)['status']
            except (KeyError, TypeError, ValueError):
                self._send(400)
                return
            if status not in (204, 503):
                self._send(400)
                return
            response_status = status
            self._send(204)
            return
        if self.path != '/hook':
            self._send(404)
            return
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            self._send(400)
            return
        with lock:
            requests.append({'payload': payload, 'headers': dict(self.headers.items())})
        self._send(response_status)

ThreadingHTTPServer(('0.0.0.0', 8765), Handler).serve_forever()
"""


def _run_python(container, source, payload=None):
    if payload is None:
        return docker("exec", container, "python", "-c", source)
    return docker(
        "exec", "-i", container, "python", "-c", source, input=json.dumps(payload)
    )


def _require_status(response, expected, label):
    require(response.status == expected, f"{label}: expected HTTP {expected}, received HTTP {response.status}")


def _json_request(page, method, url, payload=None, headers=None):
    request = getattr(page.request, method)
    merged = {"Content-Type": "application/json", **(headers or {})}
    return request(
        url,
        data=None if payload is None else json.dumps(payload),
        headers=merged,
        timeout=15000,
    )


async def check_mcp(url, key):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {key}"}, trust_env=False, timeout=30
    ) as client:
        async with streamable_http_client(url + "/mcp/", http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                require(
                    initialized.protocolVersion
                    in ("2025-11-25", "2025-06-18", "2025-03-26"),
                    "MCP negotiated an unsupported protocol version",
                )
                listed_tools = await session.list_tools()
                expected = {
                    "list_sms", "get_sms", "create_sms", "update_sms", "delete_sms"
                }
                require(
                    {tool.name for tool in listed_tools.tools} == expected,
                    "MCP returned an unexpected SMS tool set",
                )
                created = await session.call_tool(
                    "create_sms",
                    {
                        "sender": "synthetic-mcp-client",
                        "text": "Synthetic MCP acceptance message",
                        "received_at": datetime.now(timezone.utc).isoformat(),
                        "read": False,
                    },
                )
                require(not created.isError, "MCP could not create a synthetic SMS")
                record = created.structuredContent
                require(isinstance(record, dict) and record.get("id"), "MCP create omitted the SMS")
                message_id = record["id"]
                fetched = await session.call_tool("get_sms", {"id": message_id})
                require(
                    not fetched.isError and fetched.structuredContent["text"]
                    == "Synthetic MCP acceptance message",
                    "MCP could not read the created SMS",
                )
                updated = await session.call_tool(
                    "update_sms", {"id": message_id, "text": "Synthetic MCP updated", "read": True}
                )
                require(
                    not updated.isError and updated.structuredContent["read"] is True
                    and updated.structuredContent["text"] == "Synthetic MCP updated",
                    "MCP could not update the synthetic SMS",
                )
                filtered = await session.call_tool("list_sms", {"q": "synthetic-mcp-client"})
                require(
                    not filtered.isError and filtered.structuredContent["count"] == 1,
                    "MCP could not find the updated SMS",
                )
                deleted = await session.call_tool("delete_sms", {"id": message_id})
                require(
                    not deleted.isError and deleted.structuredContent["deleted"] is True,
                    "MCP could not delete the synthetic SMS",
                )
                missing = await session.call_tool("get_sms", {"id": message_id})
                require(missing.isError, "MCP returned a deleted SMS")


def _save_settings(page):
    page.get_by_role("button", name="Save settings", exact=True).click()
    from playwright.sync_api import expect

    expect(page.locator(".settings-alert.success")).to_be_visible()


def _expect_auth_error(response):
    _require_status(response, 401, "Bearer authentication")
    require(
        "bearer" in response.headers.get("www-authenticate", "").lower(),
        "Unauthorized response did not advertise Bearer authentication",
    )


def _verify_rest(api, base_url, key):
    collection = base_url + "/api/v1/messages/"
    auth = {"Authorization": f"Bearer {key}"}
    listed = api.get(collection, headers=auth, timeout=15000)
    _require_status(listed, 200, "REST list")

    fields = {
        "sender": "synthetic-rest-client",
        "text": "Synthetic REST acceptance message",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "read": False,
    }
    created = api.post(collection, data=json.dumps(fields), headers={"Content-Type": "application/json", **auth}, timeout=15000)
    _require_status(created, 201, "REST create")
    record = created.json()
    detail = collection + str(record["id"]) + "/"
    fetched = api.get(detail, headers=auth, timeout=15000)
    _require_status(fetched, 200, "REST detail")
    require(fetched.json()["text"] == fields["text"], "REST detail returned the wrong SMS")

    patched = api.patch(detail, data=json.dumps({"text": "Synthetic REST patched", "read": True}), headers={"Content-Type": "application/json", **auth}, timeout=15000)
    _require_status(patched, 200, "REST patch")
    require(
        patched.json()["read"] is True and patched.json()["text"] == "Synthetic REST patched",
        "REST patch did not update the SMS text and read state",
    )

    fields.update(text="Synthetic REST replaced", read=False)
    replaced = api.put(detail, data=json.dumps(fields), headers={"Content-Type": "application/json", **auth}, timeout=15000)
    _require_status(replaced, 200, "REST put")
    require(replaced.json()["text"] == fields["text"], "REST put did not replace the SMS")

    query = api.get(
        collection, params={"q": "synthetic-rest-client", "page_size": "2"},
        headers=auth, timeout=15000,
    )
    _require_status(query, 200, "REST filtered list")
    require(query.json()["count"] == 1, "REST search did not find the synthetic SMS")

    invalid = api.post(collection, data=json.dumps({"sender": "incomplete"}), headers={"Content-Type": "application/json", **auth}, timeout=15000)
    _require_status(invalid, 400, "REST input validation")
    deleted = api.delete(detail, headers=auth, timeout=15000)
    _require_status(deleted, 204, "REST delete")
    missing = api.get(detail, headers=auth, timeout=15000)
    _require_status(missing, 404, "REST deleted detail")


def _check_webhook_delivery(container, sender, retry):
    result = json.loads(_run_python(container, DELIVER_WEBHOOK, {"sender": sender, "retry": retry}))
    return result


def _webhook_requests(client, control_url):
    response = client.get(control_url + "/requests")
    require(response.status_code == 200, "Webhook receiver did not return its request list")
    return response.json()


def _expect_status(page, base_url, sender, label, sent_at=None):
    from playwright.sync_api import expect

    page.goto(base_url + "/", wait_until="networkidle")
    page.locator("#search").fill(sender)
    row = page.locator(".message-row").filter(has_text=sender)
    expect(row).to_have_count(1)
    expect(row.locator(".webhook-label")).to_have_text(label)
    expect(row.locator(".webhook-label")).to_be_visible()
    row_time = row.locator("time.webhook-sent-at")
    expect(row_time).to_have_count(1 if sent_at else 0)
    row.locator("button.message-open").click()
    expect(page.locator("#reader-webhook .webhook-label")).to_have_text(label)
    expect(page.locator("#reader-webhook .webhook-label")).to_be_visible()
    reader_time = page.locator("#reader-webhook time.webhook-sent-at")
    expect(reader_time).to_have_count(1 if sent_at else 0)
    if sent_at:
        expect(row_time).to_be_visible()
        expect(reader_time).to_be_visible()
        actual = datetime.fromisoformat(row_time.get_attribute("datetime").replace("Z", "+00:00"))
        expected = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        require(actual == expected, "Inbox webhook time did not match the saved delivery time")
        expected_text = page.evaluate(
            "value => `Sent ${new Date(value).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'})}`",
            row_time.get_attribute("datetime"),
        )
        expect(row_time).to_have_text(expected_text)
        expect(reader_time).to_have_text(expected_text)
        require(
            reader_time.get_attribute("datetime") == row_time.get_attribute("datetime"),
            "Inbox and reader webhook times differ",
        )


def _set_receiver_status(client, control_url, status):
    response = client.post(control_url + "/control", json={"status": status})
    require(response.status_code == 204, "Unable to configure the synthetic webhook receiver")


def _verify_integrations(base_url, container, receiver_name, page, playwright, redact, page_errors):
    from playwright.sync_api import expect

    keys = {}
    page.goto(base_url + "/settings/integrations/", wait_until="networkidle")
    require(page.url.endswith("/settings/integrations/"), "Administrator cannot open integration settings")
    expect(page.locator("#id_rest_enabled")).not_to_be_checked()
    expect(page.locator("#id_mcp_enabled")).not_to_be_checked()

    for kind in ("rest", "mcp"):
        page.locator(f"[data-generate-key='{kind}']").click()
        key_input = page.locator(f"#id_{kind}_key")
        expect(key_input).to_have_value(re.compile(r".{32,}"))
        key = key_input.input_value()
        require(len(key) >= 32, "Generated integration key is too short")
        keys[kind] = key
        redact.append(key)
        page.locator(f"#id_{kind}_enabled").check()
    disabled_rest = page.request.get(base_url + "/api/v1/messages/", timeout=15000)
    _require_status(disabled_rest, 404, "Disabled REST endpoint")
    disabled_mcp = _json_request(
        page, "post", base_url + "/mcp/", {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    )
    _require_status(disabled_mcp, 404, "Disabled MCP endpoint")
    _save_settings(page)
    for kind in ("rest", "mcp"):
        expect(page.locator(f"#id_{kind}_key")).to_have_value("")
        require(keys[kind] not in page.content(), "A saved key appeared in the settings page")

    session_only = page.context.request
    # page.request carries the authenticated session; a separate context proves anonymous denial.
    unauthenticated = playwright.request.new_context()
    try:
        _expect_auth_error(unauthenticated.get(base_url + "/api/v1/messages/", timeout=15000))
        _expect_auth_error(session_only.get(base_url + "/api/v1/messages/", timeout=15000))
        _expect_auth_error(page.request.get(base_url + "/api/v1/messages/?key=ignored", timeout=15000))
        _expect_auth_error(
            unauthenticated.get(
                base_url + "/api/v1/messages/",
                headers={"Authorization": "Bearer invalid-synthetic-key"}, timeout=15000,
            )
        )
        _expect_auth_error(
            unauthenticated.get(
                base_url + "/api/v1/messages/",
                headers={"Authorization": f"Bearer {keys['mcp']}"}, timeout=15000,
            )
        )
        for headers in (
            {},
            {"Authorization": "Bearer invalid-synthetic-key"},
            {"Authorization": f"Bearer {keys['rest']}"},
        ):
            request = unauthenticated.post(
                base_url + "/mcp/",
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                headers={"Content-Type": "application/json", **headers}, timeout=15000,
            )
            _expect_auth_error(request)
        _expect_auth_error(
            page.request.post(
                base_url + "/mcp/",
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                headers={"Content-Type": "application/json"}, timeout=15000,
            )
        )
        print("PASS: generated keys, disabled endpoints, and anonymous/session/wrong/cross-key rejection", flush=True)
    finally:
        unauthenticated.dispose()

    authenticated_api = playwright.request.new_context()
    try:
        _verify_rest(authenticated_api, base_url, keys["rest"])
    finally:
        authenticated_api.dispose()
    print("PASS: REST HTTP list, detail, create, patch, put, search, validation, and delete", flush=True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, check_mcp(base_url, keys["mcp"])).result(timeout=60)
    print("PASS: official MCP SDK initialize, tool discovery, and SMS CRUD", flush=True)

    for kind, endpoint in (("rest", "/api/v1/messages/"), ("mcp", "/mcp/")):
        replacement = "manual-" + kind + "-" + secrets.token_urlsafe(32)
        redact.append(replacement)
        page.locator(f"#id_{kind}_key").fill(replacement)
        _save_settings(page)
        if kind == "rest":
            _expect_auth_error(
                page.request.get(endpoint_url(base_url, endpoint), headers={"Authorization": f"Bearer {keys[kind]}"}, timeout=15000)
            )
            _require_status(
                page.request.get(endpoint_url(base_url, endpoint), headers={"Authorization": f"Bearer {replacement}"}, timeout=15000),
                200,
                "Manually rotated REST key",
            )
        else:
            _expect_auth_error(
                _json_request(page, "post", base_url + endpoint,
                              {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                              {"Authorization": f"Bearer {keys[kind]}"})
            )
            _require_status(
                _json_request(page, "post", base_url + endpoint,
                              {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                              {"Authorization": f"Bearer {replacement}"}),
                200,
                "Manually rotated MCP key",
            )
        page.locator(f"#id_{kind}_key_action").select_option("revoke")
        page.locator(f"#id_{kind}_enabled").uncheck()
        _save_settings(page)
        hash_check = r"""
import json, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from inbox.models import IntegrationSettings
settings = IntegrationSettings.objects.get(pk=1)
kind = os.environ['CHECK_KIND']
print(json.dumps({'revoked': not bool(getattr(settings, kind + '_key_hash'))}))
"""
        check_url = endpoint_url(base_url, endpoint)
        request = page.request.get if kind == "rest" else page.request.post
        kwargs = {"headers": {"Authorization": f"Bearer {replacement}"}, "timeout": 15000}
        if kind == "mcp":
            kwargs["data"] = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            kwargs["headers"]["Content-Type"] = "application/json"
        _require_status(request(check_url, **kwargs), 404, f"Revoked and disabled {kind.upper()} endpoint")
        hash_check = hash_check.replace("kind = os.environ['CHECK_KIND']", f"kind = {kind!r}")
        revoked = json.loads(_run_python(container, hash_check))
        require(revoked["revoked"], f"Revoking the {kind.upper()} key left its hash stored")
    print("PASS: manual key rotation, old-key invalidation, revocation, and disabled endpoints", flush=True)

    webhook_secret = secrets.token_urlsafe(32)
    redact.append(webhook_secret)
    page.goto(base_url + "/settings/webhooks/", wait_until="networkidle")
    require(page.url.endswith("/settings/webhooks/"), "Administrator cannot open webhook settings")
    page.locator("#id_url").fill(f"http://{receiver_name}:8765/hook")
    page.locator("#id_secret_header").fill("X-Integration-Test-Secret")
    page.locator("#id_secret").fill(webhook_secret)
    page.locator("#id_enabled").check()
    _save_settings(page)
    expect(page.locator("#id_secret")).to_have_value("")
    require(webhook_secret not in page.content(), "Webhook secret appeared in the settings page")

    sender = "synthetic-webhook-" + uuid4().hex[:10]
    redacted_text = "Synthetic webhook retry acceptance message"
    webhook_fixture = {"sender": sender, "text": redacted_text}
    first_message = json.loads(_run_python(container, CREATE_WEBHOOK_SMS, webhook_fixture))
    require(first_message["message_id"] > 0, "Could not queue the synthetic webhook SMS")

    import httpx

    receiver_host_port = json.loads(docker("inspect", receiver_name))[0]["NetworkSettings"]["Ports"]["8765/tcp"][0]["HostPort"]
    with httpx.Client(trust_env=False, timeout=10) as receiver_client:
        control_url = f"http://127.0.0.1:{receiver_host_port}"
        deadline = time.monotonic() + 20
        while True:
            try:
                require(
                    receiver_client.get(control_url + "/requests").status_code == 200,
                    "Webhook receiver health check failed",
                )
                break
            except httpx.TransportError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Webhook receiver did not become ready") from None
                time.sleep(0.2)

        _expect_status(page, base_url, sender, "Webhook pending")
        failed = _check_webhook_delivery(container, sender, retry=False)
        require(failed["attempts"] == 1 and failed["sent_at"] is None, "503 did not leave the webhook pending")
        require("503" in failed["last_error"], "503 response was not recorded as a delivery error")
        _expect_status(page, base_url, sender, "Webhook error")
        still_waiting = _check_webhook_delivery(container, sender, retry=False)
        require(still_waiting["attempts"] == 1, "Webhook retried before its scheduled time")
        require(
            len(_webhook_requests(receiver_client, control_url)) == 1,
            "Webhook made another HTTP request before its scheduled retry",
        )

        _set_receiver_status(receiver_client, control_url, 204)
        sent = _check_webhook_delivery(container, sender, retry=True)
        require(sent["attempts"] == 2 and sent["sent_at"], "204 did not mark the webhook sent")
        require(not sent["last_error"], "Successful delivery retained an error")
        _expect_status(page, base_url, sender, "Webhook sent", sent["sent_at"])

        records = _webhook_requests(receiver_client, control_url)
        require(len(records) == 2, "Expected exactly one failed and one successful webhook request")
        first, second = records
        require(first["payload"] == second["payload"], "Webhook retry changed the payload")
        event_id = first["payload"].get("event_id")
        require(event_id and first["payload"].get("event") == "sms.received", "Webhook event payload is invalid")
        for record in records:
            headers = {name.lower(): value for name, value in record["headers"].items()}
            require(
                headers.get("x-integration-test-secret") == webhook_secret,
                "Webhook did not send the custom secret header",
            )
            require(headers.get("idempotency-key") == event_id, "Webhook idempotency key did not match the event")
            sms = record["payload"]["sms"]
            require(sms["id"] == first_message["message_id"], "Webhook sent the wrong SMS")
            require(
                sms["sender"] == sender and sms["text"] == webhook_fixture["text"],
                "Webhook payload changed the sender or SMS text",
            )

        _run_python(container, "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); import django; django.setup(); from inbox.webhooks import deliver_webhooks; deliver_webhooks()")
        require(len(_webhook_requests(receiver_client, control_url)) == 2, "Sent webhook was delivered more than once")

        page.goto(base_url + "/settings/webhooks/", wait_until="networkidle")
        page.locator("#id_enabled").uncheck()
        _save_settings(page)
        page.goto(base_url + "/", wait_until="networkidle")
        page.locator("#search").fill(sender)
        row = page.locator(".message-row").filter(has_text=sender)
        expect(row).to_have_count(1)
        expect(row.locator(".webhook-label, time.webhook-sent-at")).to_have_count(0)
        row.locator("button.message-open").click()
        expect(page.locator("#reader-webhook")).to_be_hidden()
        page.goto(base_url + "/settings/webhooks/", wait_until="networkidle")
        page.locator("#id_enabled").check()
        _save_settings(page)
        _expect_status(page, base_url, sender, "Webhook sent", sent["sent_at"])
        require(len(_webhook_requests(receiver_client, control_url)) == 2, "Re-enabling redelivered the sent webhook")
        print("PASS: real webhook 503 retry, 204 success, secret header, idempotency, no duplicate, UI states/times/hiding", flush=True)

        page.goto(base_url + "/settings/webhooks/", wait_until="networkidle")
        page.locator("#id_secret_action").select_option("remove")
        _save_settings(page)
        second_sender = "synthetic-webhook-no-secret-" + uuid4().hex[:8]
        second = json.loads(_run_python(container, CREATE_WEBHOOK_SMS, {
            "sender": second_sender, "text": "Synthetic delivery without optional secret",
        }))
        optional_delivery = _check_webhook_delivery(container, second_sender, retry=False)
        require(optional_delivery["attempts"] == 1 and optional_delivery["sent_at"], "Delivery without a secret failed")
        records = _webhook_requests(receiver_client, control_url)
        require(len(records) == 3, "Expected one optional-secret webhook delivery")
        headers = {name.lower(): value for name, value in records[-1]["headers"].items()}
        require("x-integration-test-secret" not in headers, "Removed webhook secret was still sent")
        require(records[-1]["payload"]["sms"]["id"] == second["message_id"], "Optional-secret delivery sent the wrong SMS")
        _run_python(container, "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); import django; django.setup(); from inbox.webhooks import deliver_webhooks; deliver_webhooks()")
        require(len(_webhook_requests(receiver_client, control_url)) == 3, "Optional-secret webhook was delivered more than once")
        print("PASS: removing the optional webhook secret sends future events without the header", flush=True)
    require(not page_errors, f"Browser JavaScript errors: {page_errors}")


def endpoint_url(base_url, path):
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _capture_failure(page, artifacts_dir, page_errors):
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "browser-errors.log").write_text("\n".join(page_errors), encoding="utf-8")
        if page is not None:
            page.locator("input, textarea").evaluate_all(
                "fields => fields.forEach(field => { field.value = ''; field.setAttribute('value', ''); })"
            )
            page.screenshot(path=str(artifacts_dir / "integration-ui-failure.png"), full_page=True)
    except Exception:
        pass


def _sanitize(value, redact):
    for secret in sorted((entry for entry in redact if entry), key=len, reverse=True):
        value = value.replace(secret, "[redacted]")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Locally available candidate image; never pulled")
    parser.add_argument("--timeout", type=int, default=120, help="Health timeout in seconds")
    parser.add_argument("--artifacts-dir", type=Path, help="Directory for redacted failure diagnostics")
    args = parser.parse_args()
    require(args.timeout > 0, "The health timeout must be positive")

    suffix = uuid4().hex[:12]
    name = f"unifi-sms-integrations-{suffix}"
    app = name + "-app"
    receiver = name + "-receiver"
    volume = name + "-data"
    network = name + "-network"
    artifacts_dir = args.artifacts_dir or Path(tempfile.gettempdir()) / f"{name}-artifacts"
    username = "integration-smoke-" + suffix
    password = secrets.token_urlsafe(24)
    redact = [password]
    page_errors = []
    browser = None
    page = None
    base_url = "http://127.0.0.1"
    app_created = receiver_created = volume_created = network_created = False
    try:
        docker("image", "inspect", args.image)
        docker("network", "create", network)
        network_created = True
        docker("volume", "create", volume)
        volume_created = True
        web_only = (
            "import os,subprocess,sys; os.umask(0o077); "
            "subprocess.run([sys.executable,'manage.py','migrate','--noinput'],check=True); "
            "os.execvp('gunicorn',['gunicorn','config.wsgi:application','--bind','0.0.0.0:8000',"
            "'--workers','1','--threads','4','--access-logfile','/dev/null'])"
        )
        docker(
            "create", "--pull=never", "--name", app,
            "--network", network,
            "--publish", "127.0.0.1::8000",
            "--mount", f"type=volume,source={volume},target=/data",
            "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL",
            "--stop-timeout", "20", "--entrypoint", "python", args.image, "-c", web_only,
        )
        app_created = True
        docker(
            "create", "--pull=never", "--name", receiver,
            "--network", network, "--publish", "127.0.0.1::8765",
            "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL",
            "--read-only", "--tmpfs", "/tmp", "--tmpfs", "/data", "--entrypoint", "python", args.image,
            "-u", "-c", RECEIVER,
        )
        receiver_created = True
        docker("start", app)
        base_url = wait_healthy(app, args.timeout)
        try:
            probe = _run_python(app, FEATURE_PROBE)
        except RuntimeError:
            raise RuntimeError(
                "Candidate image is unsupported: it must include migrated REST, MCP, and webhook integrations"
            ) from None
        require(probe.strip() == "supported", "Candidate image is unsupported: integration routes or tables are missing")

        fixture = {
            "username": username,
            "password": password,
            "email": "integration-smoke@example.invalid",
        }
        _run_python(app, SEED, fixture)
        docker("start", receiver)

        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(locale="en-US", timezone_id="UTC")
            page = context.new_page()
            page.set_default_timeout(min(args.timeout * 1000, 30000))
            page.on("pageerror", lambda error: page_errors.append(type(error).__name__))
            try:
                page.goto(base_url + "/login/", wait_until="networkidle")
                page.get_by_label("Username").fill(username)
                page.get_by_label("Password").fill(password)
                page.get_by_role("button", name="Sign in").click()
                expect(page.get_by_role("heading", name="Inbox")).to_be_visible()
                _verify_integrations(base_url, app, receiver, page, playwright, redact, page_errors)
            except Exception:
                _capture_failure(page, artifacts_dir, page_errors)
                raise
            context.close()
            browser.close()
            browser = None
        print("PASS: isolated REST, MCP, webhook, and browser acceptance", flush=True)
    except Exception as error:
        if app_created:
            try:
                logs = _sanitize(docker("logs", "--tail", "100", app), redact)
                artifacts_dir.mkdir(parents=True, exist_ok=True)
                (artifacts_dir / "container.log").write_text(logs, encoding="utf-8")
            except Exception:
                pass
        if artifacts_dir.exists():
            print(f"Redacted failure diagnostics: {artifacts_dir}", file=sys.stderr)
        raise RuntimeError(_sanitize(str(error), redact)) from None
    finally:
        cleanup_errors = []
        for created, command in (
            (receiver_created, ("rm", "--force", receiver)),
            (app_created, ("rm", "--force", app)),
            (volume_created, ("volume", "rm", volume)),
            (network_created, ("network", "rm", network)),
        ):
            if created:
                try:
                    docker(*command)
                except Exception:
                    cleanup_errors.append(command[0])
        if cleanup_errors:
            print("Unable to remove one or more temporary Docker resources.", file=sys.stderr)
        else:
            print("Removed temporary integration containers, volume, and network.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Integration acceptance failed: {error}", file=sys.stderr)
        sys.exit(1)
