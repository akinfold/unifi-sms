# Connect the SMS archive to other tools

REST API, MCP, and webhook forwarding are available starting with version 0.1.5. They are independent options, disabled by default. Configure them in the web interface after creating an administrator.

## Enable REST API or MCP

1. Open **Settings → API and MCP**.
2. Select **Enable REST API**, **Enable MCP**, or both.
3. Enter a separate key for each enabled integration, or select **Generate key**. Keys must contain 32–256 printable ASCII characters without spaces.
4. Copy each key before selecting **Save settings**. Generated keys are drafts until saved. The application stores a hash and cannot display the saved key again.
5. Configure your client to send `Authorization: Bearer YOUR_KEY` on every request. Use the endpoint shown on the settings page.

Both keys grant read and write access to the entire SMS archive. Each integration validates its own saved key. Use different values so you can grant and revoke access to each integration separately. Use HTTPS when credentials travel over a network. An administrator's browser session does not authorize these endpoints.

To rotate a key, enter or generate a new value and save. The old key stops working immediately. To revoke it, turn off the integration, choose **Revoke key**, and save. Turning off an integration without revoking its key pauses access and preserves the key for later use. Disabled endpoints return HTTP 404; enabled endpoints with missing or invalid credentials return HTTP 401.

## Use the REST API

The collection endpoint is `/api/v1/messages/`. A record's endpoint is `/api/v1/messages/ID/`. Keep the trailing slash, including on write requests.

In Bash, set your base URL and key. Replace the example values with your deployment's settings:

```sh
SMS_URL='https://sms.example.com'
read -r -s -p 'REST API key: ' SMS_API_KEY
printf '\n'
export SMS_URL SMS_API_KEY
```

List messages, newest received first:

```sh
curl --fail-with-body "$SMS_URL/api/v1/messages/?page=1&page_size=30" \
  -H "Authorization: Bearer $SMS_API_KEY"
```

Optional query parameters are `page` (default 1), `page_size` (default 30, maximum 100), `q` (sender or text search, maximum 200 characters), and `read=true` or `read=false`. The response contains `messages`, `page`, `page_size`, `pages`, and `count`.

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

Enable MCP and copy the `/mcp/` URL from **Settings → API and MCP**. Your client must support **Streamable HTTP** with a custom `Authorization` header. For clients that use a JSON configuration, supply the equivalent of:

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

Configuration field names vary by client. This is a static Bearer-key integration; clients that require OAuth discovery without custom headers cannot connect directly. Store the key using your client's secret storage when available.

The server exposes these tools:

| Tool | Purpose |
| --- | --- |
| `list_sms` | Search, filter by read state, and page through the archive. |
| `get_sms` | Read one archive record by ID. |
| `create_sms` | Create an archive record without sending or forwarding it. |
| `update_sms` | Change selected archive fields, subject to cleanup and multipart restrictions. |
| `delete_sms` | Delete an archive record after device cleanup. |

MCP uses the same record fields and CRUD restrictions as REST. An MCP key grants access to all these tools, including deletion. Configure tool approval in your client as appropriate.

The endpoint accepts JSON-RPC POST requests and returns JSON responses. It supports protocol versions `2025-11-25`, `2025-06-18`, and `2025-03-26`, without persistent sessions or an SSE stream. GET and DELETE return HTTP 405. Requests with an `Origin` header must match the service origin or `CSRF_TRUSTED_ORIGINS`.

## Forward new SMS to a webhook

1. Open **Settings → Webhooks**.
2. Enter an HTTP or HTTPS destination URL that accepts JSON POST requests. Credentials in the URL and URL fragments are rejected.
3. Optionally enter a secret and its header name. The default header is `X-Webhook-Secret`. To use Bearer authorization, set the header to `Authorization` and the secret to `Bearer YOUR_TOKEN`.
4. Select **Enable webhook forwarding** and **Save settings**.
5. Import a new SMS and check its webhook status in the inbox and message reader.

Leave the secret blank to preserve a saved value. To remove it, choose **Remove secret** and save. Secrets are encrypted with the application secret stored in the data volume. Back up the whole volume, including that application secret.

Only new, complete SMS imported while the webhook is enabled and valid enter the queue. Existing messages and records created through REST or MCP are not forwarded. Multipart messages wait until all parts arrive. Webhook and email forwarding can run independently or together.

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

Requests time out after 10 seconds. Failures retry after one minute, with exponential backoff up to one hour; the worker checks about once a minute. HTTP redirects are rejected, so the secret is not forwarded to another URL. HTTP error response bodies and credentials are not shown in the inbox.

The inbox and message reader show **Webhook pending**, **Webhook error**, or **Webhook sent** while forwarding is enabled and valid. Sent messages include the sending time. **Webhook not queued** means no delivery job exists for that SMS. Disabling forwarding hides these statuses and pauses pending jobs; enabling it resumes them without losing their state.

Pending jobs use the current URL and secret with the original payload and event ID. Changing the destination sends pending messages to the new destination. Deleting an archived SMS also cancels its queued webhook, unless a request is already in progress.
