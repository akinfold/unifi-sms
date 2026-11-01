# Connect the SMS archive to other tools

Use REST API, MCP, or outgoing webhooks to work with SMS outside the inbox:

| Goal | Integration | First result |
| --- | --- | --- |
| Search messages from a script, export records, or update their read state. | [REST API](#use-the-rest-api) | A JSON response containing archived SMS. |
| Give an MCP client or assistant tools to search and manage the archive. | [MCP](#connect-an-mcp-client) | Five SMS tools and a search result from `list_sms`. |
| Start an external workflow when a new SMS arrives. | [Outgoing webhook](#forward-new-sms-to-a-webhook) | An `sms.received` event at your receiver and **Webhook sent** in the inbox. |

These integrations are available starting with version 0.1.5. Each is disabled by default and can be enabled independently in the web interface after creating an administrator. REST and MCP manage archive records; they do not send cellular SMS.

## Enable REST API or MCP

Each REST or MCP key grants read, write, and delete access to the entire SMS archive. There are no read-only keys or per-message scopes. Give a key only to a client you trust, and use HTTPS when credentials travel over a network. An administrator's browser session does not authorize these endpoints.

1. Open **Settings → API and MCP**.
2. Select **Enable REST API**, **Enable MCP**, or both.
3. Enter a separate key for each enabled integration, or select **Generate key**. Keys must contain 32–256 printable ASCII characters without spaces.
4. Copy each key to a secure location, then select **Save settings**. Generated keys are drafts until saved; the saved key cannot be displayed again.
5. Follow the [REST](#use-the-rest-api) or [MCP](#connect-an-mcp-client) check below, using the endpoint shown on the settings page and the matching saved key.

Use different REST and MCP keys so you can grant and revoke access separately. Each integration accepts only its own saved key. Both use `Authorization: Bearer YOUR_KEY` on every request; neither provides an OAuth sign-in flow. See [key management and authentication](#key-management-and-authentication) for rotation, revocation, and error responses.

## Use the REST API

Use REST to search the archive from scripts, copy records into another system, or mark processed messages as read.

After [enabling REST and saving its key](#enable-rest-api-or-mcp), open Bash and set your base URL and REST key. Replace the URL with your deployment's address, without a trailing slash:

```sh
SMS_URL='https://sms.example.com'
read -r -s -p 'REST API key: ' SMS_API_KEY
printf '\n'
export SMS_URL SMS_API_KEY
```

### Get your first result

List messages, newest received first:

```sh
curl --fail-with-body "$SMS_URL/api/v1/messages/?page=1&page_size=30" \
  -H "Authorization: Bearer $SMS_API_KEY"
```

Expect HTTP 200 and a JSON object with `messages`, `page`, `page_size`, `pages`, and `count`. An empty archive returns:

```json
{"messages": [], "page": 1, "page_size": 30, "pages": 1, "count": 0}
```

To check that you can find a real incoming SMS, send an SMS containing `integration-check-123` to the phone number of the SIM in the LTE device and wait until it appears in the inbox. Then search for that text:

```sh
curl --fail-with-body --get "$SMS_URL/api/v1/messages/" \
  -H "Authorization: Bearer $SMS_API_KEY" \
  --data-urlencode 'q=integration-check-123'
```

The `messages` array should contain the SMS with its sender and text, and `count` should be at least 1. Reading or listing messages does not mark them as read.

### Search and manage records

The collection endpoint is `/api/v1/messages/`. A record's endpoint is `/api/v1/messages/ID/`. Keep the trailing slash, including on write requests. In the examples below, replace `42` with an ID returned by your search.

Optional query parameters are `page` (default 1), `page_size` (default 30, maximum 100), `q` (sender or text search, maximum 200 characters), and `read=true` or `read=false`. The `q` search matches sender or message text without case sensitivity.

Read one record without changing its read state:

```sh
curl --fail-with-body "$SMS_URL/api/v1/messages/42/" \
  -H "Authorization: Bearer $SMS_API_KEY"
```

Create an archive record:

```sh
curl --fail-with-body "$SMS_URL/api/v1/messages/" \
  -H "Authorization: Bearer $SMS_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"sender":"Example sender","text":"Example message","received_at":"2026-01-01T12:00:00Z","read":false}'
```

Creation returns HTTP 201 and the new record. It does not send a cellular SMS, create a modem slot, or queue email or webhook forwarding. New records have `source: "api"`.

Update selected fields, for example to mark a message as read:

```sh
curl --fail-with-body -X PATCH "$SMS_URL/api/v1/messages/42/" \
  -H "Authorization: Bearer $SMS_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"read":true}'
```

`PUT` replaces all editable fields and requires `sender`, `text`, `received_at`, and `read`. `PATCH` requires at least one of them. `sender` accepts up to 128 characters and `text` up to 65,536 characters. Timestamps require an explicit UTC offset or `Z`. JSON bodies are limited to 128 KiB. Fields such as `source`, device credentials, and delivery state cannot be set.

Delete an archive record:

```sh
curl --fail-with-body -X DELETE "$SMS_URL/api/v1/messages/42/" \
  -H "Authorization: Bearer $SMS_API_KEY"
```

Deletion returns HTTP 204. It also removes queued deliveries; an HTTP or SMTP request already in progress may still complete. Receipt fingerprints remain for deduplication, but their saved PDU text is cleared. Deletion does not send a command to the device.

Content edits return HTTP 409 while a multipart SMS is incomplete or device cleanup is pending. Read-state changes remain available. Deletion returns HTTP 409 while device cleanup is pending. Missing records return HTTP 404, and invalid fields or pagination return HTTP 400. The API does not resend notifications when you edit an archived message. Webhook jobs keep the content captured at import.

## Connect an MCP client

Use MCP to make the archive available as tools in a compatible client or assistant. For example, a client can search unread messages with `list_sms`, read a result with `get_sms`, and mark it as read with `update_sms`.

The client must support **Streamable HTTP** and a custom `Authorization` header. This integration uses a static Bearer key, without OAuth discovery or sign-in. A client that requires OAuth or cannot send custom headers cannot connect directly. The MCP key also permits editing and deletion across the archive; configure tool approval in your client as appropriate.

### Verify with the Python MCP SDK

This recipe uses the [official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0), pinned to `mcp==1.30.0`, as in the project's [integration smoke test](../scripts/smoke_integrations.py). The commands below use Python 3.12 and Bash. The check lists tools and searches messages without changing the archive.

1. [Enable MCP, copy its key, and save settings](#enable-rest-api-or-mcp).
2. Send an SMS containing `integration-check-123` to the phone number of the SIM in the LTE device and wait until it appears in the inbox. You can reuse the SMS from the REST check.
3. Create an isolated Python environment and install the SDK:

   ```sh
   python3.12 -m venv .venv-sms-mcp
   .venv-sms-mcp/bin/python -m pip install 'mcp==1.30.0'
   ```

4. Set the service URL and the saved MCP key in Bash. Use the MCP key, which is separate from the REST key:

   ```sh
   SMS_URL='https://sms.example.com'
   read -r -s -p 'MCP key: ' SMS_MCP_KEY
   printf '\n'
   export SMS_URL SMS_MCP_KEY
   ```

5. Save this as `sms_mcp_check.py`:

   ```python
   import asyncio
   import json
   import os

   import httpx
   from mcp import ClientSession
   from mcp.client.streamable_http import streamable_http_client


   async def main():
       url = os.environ["SMS_URL"].rstrip("/") + "/mcp/"
       headers = {"Authorization": f"Bearer {os.environ['SMS_MCP_KEY']}"}
       async with httpx.AsyncClient(
           headers=headers, trust_env=False, timeout=30
       ) as client:
           async with streamable_http_client(url, http_client=client) as (read, write, _):
               async with ClientSession(read, write) as session:
                   await session.initialize()
                   tools = await session.list_tools()
                   print("Tools:", ", ".join(tool.name for tool in tools.tools))
                   result = await session.call_tool(
                       "list_sms", {"q": "integration-check-123"}
                   )
                   if result.isError:
                       raise RuntimeError(result.structuredContent)
                   print(json.dumps(result.structuredContent, indent=2, ensure_ascii=False))


   asyncio.run(main())
   ```

6. Run the check:

   ```sh
   .venv-sms-mcp/bin/python sms_mcp_check.py
   ```

Expect the five tools below, followed by a JSON search result containing your SMS. If `count` is 0, check that the SMS appears in the inbox and that its text matches the search. The example disables environment proxy settings with `trust_env=False`, matching the smoke test.

### Configure another MCP client

Use the `/mcp/` URL shown in **Settings → API and MCP**. If your client supports a URL and custom headers in JSON configuration, supply its equivalent of:

```json
{
  "mcpServers": {
    "sms-archive": {
      "url": "https://sms.example.com/mcp/",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_KEY"
      }
    }
  }
}
```

This is a configuration shape, not a universal client format. Check your client's support for Streamable HTTP, Bearer keys, and custom headers; field names vary. Store the key using the client's secret storage when available. After connecting, list tools and call `list_sms` with `{"q":"integration-check-123"}` to verify access.

### Available tools and transport

The server exposes these tools:

| Tool | Purpose |
| --- | --- |
| `list_sms` | Search, filter by read state, and page through the archive. |
| `get_sms` | Read one archive record by ID. |
| `create_sms` | Create an archive record without sending or forwarding it. |
| `update_sms` | Change selected archive fields, subject to cleanup and multipart restrictions. |
| `delete_sms` | Delete an archive record after device cleanup. |

MCP uses the same record fields and create, read, update, and delete restrictions as REST. The same key authorizes every tool.

The endpoint accepts JSON-RPC POST requests and returns JSON responses. It supports protocol versions `2025-11-25`, `2025-06-18`, and `2025-03-26`, without persistent sessions or an SSE stream. GET and DELETE return HTTP 405. Requests with an `Origin` header must match the service origin or `CSRF_TRUSTED_ORIGINS`.

## Forward new SMS to a webhook

Use an outgoing webhook to start an external workflow as new SMS arrive, such as creating a ticket or passing a message to your own notification service. Prepare a receiver that accepts a JSON POST and returns a 2xx status. Forwarding is disabled by default.

1. Open **Settings → Webhooks**.
2. Enter an HTTP or HTTPS destination URL that accepts JSON POST requests. Use HTTPS when SMS or secrets travel over a network; HTTP does not encrypt them. Credentials in the URL and URL fragments are rejected.
3. Optionally enter a secret and its header name. The default header is `X-Webhook-Secret`. To use Bearer authorization, set the header to `Authorization` and the secret to `Bearer YOUR_TOKEN`.
4. Select **Enable webhook forwarding** and **Save settings**.
5. Send a new SMS to the phone number of the SIM in the LTE device after saving, then wait for it to appear in the inbox. Use a distinct message such as `webhook-check-123`.
6. Check that the receiver got an event with `"event": "sms.received"` and your SMS in `sms.text`. After the receiver returns a 2xx status, the inbox and message reader show **Webhook sent** with the sending time.

If the status stays **Webhook pending** or changes to **Webhook error**, check the destination, secret header, and receiver response. See [delivery and status behavior](#delivery-and-status-behavior) for retries. Creating an archive record through REST or MCP does not test incoming webhook forwarding.

Only new, complete SMS imported while the webhook is enabled and valid enter the queue. Existing messages and records created through REST or MCP are not forwarded. Multipart messages wait until all parts arrive. Webhook and email forwarding can run independently or together.

To keep a saved secret when editing settings, leave its field blank. To remove it, choose **Remove secret** and save. Secrets are encrypted with the application secret stored in the data volume. Back up the whole volume, including that application secret.

### Request payload

Each request has `Content-Type: application/json; charset=utf-8`, an `Idempotency-Key` header equal to `event_id`, and your optional secret header. Example:

```json
{
  "event": "sms.received",
  "event_id": "f890785e-e31b-4b53-88a9-29cd4265c0cc",
  "sms": {
    "id": 42,
    "source": "lte-device",
    "sender": "Example sender",
    "text": "Example message",
    "received_at": "2026-01-01T12:00:00+00:00",
    "imported_at": "2026-01-01T12:00:05+00:00"
  }
}
```

Return any 2xx status to acknowledge receipt. The service does not inspect the response body. The receiver should deduplicate by `event_id`: a request can be retried after the receiver accepts it if the response is lost.

### Delivery and status behavior

The SMS and its webhook job are saved before the device slot is cleared. Webhook delivery runs after import and email forwarding. Delivery failures leave the archive and slot cleanup unaffected.

Each request has a total 10-second deadline, including DNS lookup, connection, TLS, upload, and response headers. Timed-out requests are terminated and retried. Failures retry after one minute, with exponential backoff up to one hour; the worker checks about once a minute. HTTP redirects are rejected, so the secret is not forwarded to another URL. HTTP error response bodies and credentials are not shown in the inbox.

The inbox and message reader show **Webhook pending**, **Webhook error**, or **Webhook sent** while forwarding is enabled and valid. Sent messages include the sending time. **Webhook not queued** means no delivery job exists for that SMS. Disabling forwarding hides these statuses and pauses pending jobs; enabling it resumes them without losing their state.

Pending jobs use the current URL and secret with the original payload and event ID. Changing the destination sends pending messages to the new destination. Deleting an archived SMS also cancels its queued webhook, unless a request is already in progress.

## Key management and authentication

To rotate a key, enter or generate a new value and save. The old key stops working immediately. To revoke it, turn off the integration, choose **Revoke key**, and save. Turning off an integration without revoking its key pauses access and preserves the key for later use.

Disabled endpoints return HTTP 404. Enabled endpoints with missing or invalid credentials return HTTP 401. Failed authentication is limited to 30 attempts per client per minute; further failures return HTTP 429 with a `Retry-After` header. A valid key stored with a keyed hash remains usable when this limit is reached.

Existing keys remain valid after an upgrade. Their older password hashes are replaced with fast keyed hashes after successful authentication. Until then, verification is limited to five attempts per client and twenty across the service per minute. Exceeding either limit returns HTTP 429, even for a valid key that has not yet been upgraded.

Saved integration keys use HMAC-SHA256 with a domain-separated key derived from the application's persistent `SECRET_KEY`. Verification is fast and uses a constant-time comparison. A database copy alone cannot verify guesses without the application secret. A copy of the entire data volume includes both, so protect the volume and its backups. Changing the application secret, including through `DJANGO_SECRET_KEY`, invalidates these hashes and the encrypted credentials that depend on it. Restore the original secret or replace the affected credentials and integration keys.
