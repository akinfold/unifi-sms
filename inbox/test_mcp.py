import json

from django.contrib.auth.hashers import make_password
from django.test import Client, TestCase, override_settings
from django.urls import path

from . import mcp_views
from .models import IntegrationSettings

urlpatterns = [path("mcp/", mcp_views.endpoint)]


@override_settings(ROOT_URLCONF=__name__, ALLOWED_HOSTS=["testserver"], CSRF_TRUSTED_ORIGINS=["https://trusted.example"])
class SmsMcpTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.key = "mcp-test-key-value"
        IntegrationSettings.objects.create(
            pk=1,
            rest_enabled=True,
            rest_key_hash=make_password("different-rest-key"),
            rest_key_suffix="key",
            mcp_enabled=True,
            mcp_key_hash=make_password(self.key),
            mcp_key_suffix=self.key[-4:],
        )
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {self.key}"}

    def rpc(self, method, params=None, request_id=1, **headers):
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        return self.client.post("/mcp/", json.dumps(payload), content_type="application/json", **self.headers, **headers)

    def test_disabled_auth_and_unsupported_methods(self):
        IntegrationSettings.objects.filter(pk=1).update(mcp_enabled=False)
        self.assertEqual(self.client.post("/mcp/").status_code, 404)
        IntegrationSettings.objects.filter(pk=1).update(mcp_enabled=True)
        unauthorized = self.client.post("/mcp/", "{}", content_type="application/json")
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized["WWW-Authenticate"], 'Bearer realm="sms-archive"')
        self.assertEqual(self.client.get("/mcp/", **self.headers).status_code, 405)
        self.assertEqual(self.client.delete("/mcp/", **self.headers).status_code, 405)

    def test_origin_and_protocol_header_validation(self):
        bad_origin = self.rpc("ping", HTTP_ORIGIN="https://attacker.example")
        self.assertEqual(bad_origin.status_code, 403)
        trusted = self.rpc("ping", HTTP_ORIGIN="https://trusted.example")
        self.assertEqual(trusted.status_code, 200)
        unsupported = self.rpc("ping", HTTP_MCP_PROTOCOL_VERSION="2024-11-05")
        self.assertEqual(unsupported.status_code, 400)

    @override_settings(CSRF_TRUSTED_ORIGINS=["https://*.trusted.example"])
    def test_wildcard_trusted_origin_requires_the_same_scheme_and_domain(self):
        self.assertEqual(self.rpc("ping", HTTP_ORIGIN="https://client.trusted.example").status_code, 200)
        self.assertEqual(self.rpc("ping", HTTP_ORIGIN="http://client.trusted.example").status_code, 403)
        self.assertEqual(self.rpc("ping", HTTP_ORIGIN="https://trusted.example.attacker.invalid").status_code, 403)

    def test_lifecycle_protocol_versions_ping_tools_and_notification(self):
        initialized = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}).json()
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(initialized["result"]["capabilities"], {"tools": {"listChanged": False}})
        fallback = self.rpc("initialize", {"protocolVersion": "unsupported"}).json()
        self.assertEqual(fallback["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(self.rpc("ping").json()["result"], {})
        tools = self.rpc("tools/list").json()["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"list_sms", "get_sms", "create_sms", "update_sms", "delete_sms"})
        self.assertTrue(next(tool for tool in tools if tool["name"] == "delete_sms")["annotations"]["destructiveHint"])
        notification = self.client.post("/mcp/", json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), content_type="application/json", **self.headers)
        self.assertEqual(notification.status_code, 202)
        self.assertEqual(notification.content, b"")
        response_message = self.client.post("/mcp/", json.dumps({"jsonrpc": "2.0", "id": 4, "result": {}}), content_type="application/json", **self.headers)
        self.assertEqual(response_message.status_code, 202)
        self.assertEqual(response_message.content, b"")

    def test_tool_crud_returns_text_and_structured_content_and_domain_errors(self):
        created = self.rpc("tools/call", {"name": "create_sms", "arguments": {
            "sender": "sender",
            "text": "archive body",
            "received_at": "2026-10-24T12:30:00Z",
            "read": False,
        }}).json()["result"]
        self.assertFalse(created["isError"])
        message = created["structuredContent"]
        self.assertEqual(message["source"], "api")
        self.assertEqual(json.loads(created["content"][0]["text"]), message)
        message_id = message["id"]
        self.assertEqual(self.rpc("tools/call", {"name": "get_sms", "arguments": {"id": message_id}}).json()["result"]["structuredContent"]["text"], "archive body")
        self.assertEqual(self.rpc("tools/call", {"name": "update_sms", "arguments": {"id": message_id, "read": True}}).json()["result"]["structuredContent"]["read"], True)
        listing = self.rpc("tools/call", {"name": "list_sms", "arguments": {"q": "archive"}}).json()["result"]["structuredContent"]
        self.assertEqual(listing["count"], 1)
        deleted = self.rpc("tools/call", {"name": "delete_sms", "arguments": {"id": message_id}}).json()["result"]
        self.assertFalse(deleted["isError"])
        missing = self.rpc("tools/call", {"name": "get_sms", "arguments": {"id": message_id}}).json()["result"]
        self.assertTrue(missing["isError"])
        self.assertEqual(missing["structuredContent"]["status"], 404)

    def test_tool_validation_and_rpc_errors(self):
        invalid = self.rpc("tools/call", {"name": "create_sms", "arguments": {"sender": "x", "text": "x", "received_at": "no offset", "read": False}}).json()["result"]
        self.assertTrue(invalid["isError"])
        self.assertEqual(self.rpc("missing/method").json()["error"]["code"], -32601)
        invalid_params = self.rpc("tools/call", {"name": 7, "arguments": {}}).json()
        self.assertEqual(invalid_params["error"]["code"], -32602)
        batch = self.client.post("/mcp/", json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]), content_type="application/json", **self.headers)
        self.assertEqual(batch.status_code, 400)
        self.assertEqual(batch.json()["error"]["code"], -32600)
        deeply_nested = b"{" * 2000 + b"0" + b"}" * 2000
        parse_error = self.client.post("/mcp/", deeply_nested, content_type="application/json", **self.headers)
        self.assertEqual(parse_error.status_code, 400)
        self.assertEqual(parse_error.json()["error"]["code"], -32700)
        unsupported_content = self.client.post("/mcp/", "{}", content_type="text/plain", **self.headers)
        self.assertEqual(unsupported_content.status_code, 415)
