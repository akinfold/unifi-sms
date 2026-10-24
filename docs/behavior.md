# UniFi SMS behavior and limits

Use the [README quick start](../README.md#quick-start-with-docker-compose) to install the application. This reference describes discovery, saved credentials, and message processing.

## Device discovery

Search combines UniFi discovery on UDP port `10001` with unauthenticated SSH banner checks on TCP port `22`. Reverse DNS (PTR) adds hostnames and places names matching LTE Pro first. **Likely LTE Pro** means the hostname suggests the model; a DNS name does not establish device identity. The authenticated connection check confirms the LTE model before trust or import.

Search can take just over a minute. The interface shows a countdown and lists the subnets it checked. The search limits concurrent SSH connections and waits for delayed connection and banner responses. Candidates still appear without hostnames if DNS is unavailable or slow.

Docker bridge networking does not expose the host LAN directly. Automatic search also tries four common subnets: `192.168.0.0/24`, `192.168.1.0/24`, `10.0.0.0/24`, and `10.0.1.0/24`. For another network, enter its private IPv4 subnet under **LAN subnet**, with at most 256 addresses. Enter the address manually if SSH uses another port or the network blocks discovery.

## Credentials and device trust

Only an explicitly trusted configuration is used for import. The application writes the approved host key to `/data/known_hosts` with owner-only permissions and checks it on later connections. A changed or missing key stops connections.

Device passwords and private keys are encrypted in SQLite using the persistent application secret. They are not shown again in the form. Preserve `/data/secret-key` with the database when restoring. The backup includes the decryption key and must remain private. SMTP passwords are stored in the private SQLite volume; do not assume the entire database is encrypted.

A connection check expires after ten minutes. Changing credentials requires another check. The active connection stays in use until a replacement is checked and trusted. Canceling the wizard discards its draft; drafts expire after 24 hours.

Device and SMTP configuration comes from the web interface. Legacy `LTE_*` and `SMTP_*` environment variables are ignored. Modem serial-device and SMS-storage choices are fixed internally rather than exposed as settings.

## Device information

The inbox shows the configured SSH address, the hardware model read from `board.name` in `/etc/board.info`, the phone number stored on the SIM, and the time of the last successful SIM check. Device details are visible only to signed-in administrators. The worker reads the SIM number with `AT+CNUM` after administrator setup, checks it automatically every hour, and saves the result in SQLite. Some SIM cards do not store a number; the inbox shows **Not available** when the device returns none.

Select **Update from device** to refresh the model and SIM number, for example after replacing the SIM. The button shows **Updating…** until the worker finishes. A request waits if the worker is importing messages or forwarding email. Number checks use the same modem lock as SMS imports.

If a check fails, the inbox keeps the last saved number and labels it as outdated. A successful empty response clears the previous number. Changing the configured device address or SSH port uses a separate cached result. Reading the number does not change the number stored on the SIM. Model checks run independently of SIM number checks. If the model cannot be read, the inbox keeps its saved value and shows that the update failed.

## Email delivery

Configure forwarding under **Settings → Email forwarding**. **Replace password** enters a new password; **Remove login details** clears both username and password when saved. Leave both credentials empty only for a server that accepts mail without authentication.

**Send test email** uses the current form without saving it and works while forwarding is disabled. A blank password uses the saved password. The test sends a separate message to every configured recipient. A successful test means the server accepted it; check the recipient inbox and spam folder for delivery.

Only messages completed while forwarding is enabled enter the persistent queue. Existing complete messages are not sent retroactively. Multipart SMS wait until all parts arrive. Each email includes the SMS sender, received timestamp with its UTC offset, and full text. Recipients receive separate copies.

When forwarding is configured and enabled, the inbox and message reader show **Email pending**, **Email error**, or **Email sent**. Sent messages include the sending time. **Email not queued** means the SMS has no forwarding job, for example because it was imported before forwarding was enabled. Statuses are hidden while forwarding is disabled or its settings are incomplete.

SMTP delivery runs after modem import. Delivery failures leave the SMS archive and device cleanup unaffected. Retries start after one minute and back off to one hour; the worker checks once a minute. Disabling forwarding pauses queued messages. Pending messages use current server and authentication settings, while recipient addresses are fixed when the first delivery attempt starts. Deleting an archived SMS also cancels its queued delivery unless a send is already in progress.

Successful recipients are recorded separately, so a retry after a partial failure does not resend to them. SMTP cannot guarantee exactly-once delivery: a crash after server acceptance but before recording success may cause a duplicate. Retries retain the same Message-ID for each recipient. The Settings page shows pending delivery counts and the latest failure. A successful send means the SMTP server accepted the message, not that it reached the recipient's inbox.

## SMS import and deletion

Each scan selects storage with `AT+CPMS`, reads PDUs with `AT+CMGL=4`, and supports zero-based slot numbers. Text decoding covers GSM-7, its standard extension alphabet, UCS-2, numeric and alphanumeric senders, and multipart headers with 8-bit or 16-bit references.

Each segment is committed to SQLite with its original PDU before any deletion. The importer rereads the slot using `AT+CMGR` and compares the complete PDU, then deletes only that slot with `AT+CMGD`. SQLite uses WAL and synchronous `FULL`. A local file lock and a device-side lock prevent concurrent instances of this importer from sharing the same modem.

If reading, decoding, verification, or saving fails, the affected slot is retained. A deletion failure leaves the archive intact and is retried on the next scan. A scan also reconciles archived segments that have already disappeared from the device. Unsupported binary messages, compressed messages, language-shift headers, and malformed PDUs remain on the device and produce a warning in the inbox.

Multipart segments are grouped by device, sender, reference, part count, and a 24-hour timestamp window. Missing parts are shown explicitly. Reference reuse within that window can be ambiguous; original PDUs are retained with the archive. Exact repeated PDUs are deduplicated.

Deleting messages in the web interface removes their archive content. This action is blocked while any segment still awaits confirmed device cleanup. Minimal fingerprint receipts remain to prevent deleted messages from reappearing after a replay; they contain no message text after deletion. SQLite deletion is not forensic erasure of database pages or backups.

Only one service instance should own the database volume. Avoid running another SMS reader or deletion tool against the modem at the same time: external tools and firmware do not honor the service's lock, and a serial modem offers no atomic compare-and-delete operation. The modem's original text/PDU mode and serial settings are restored when the session closes. The selected SMS storage remains configured on the device.

## Health and recovery

The inbox shows importer state, pending device cleanup, and the last successful scan. A stopped importer becomes stale in the interface. `/healthz/` checks the web process and SQLite connectivity; modem failures appear in the inbox without making the HTTP service unhealthy. The container exits if either its web server or importer exits, allowing the restart policy to recover it.

A lost SSH session normally releases the device lock. After a device crash, `/tmp/unifi-sms-modem.lock` may need removal through a trusted administrative console. First confirm that no importer session is active. The importer does not remove another session's lock automatically.
