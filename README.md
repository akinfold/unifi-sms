# UniFi SMS

A small Django service that archives incoming UniFi LTE Pro SMS messages in SQLite and clears their modem slots after the database transaction commits. It provides an administrator-only inbox with search, unread filtering, a message reader, and bulk deletion.

The image runs Gunicorn and one supervised importer. No Redis, task queue, frontend build, or external database is required.

## Quick start

```sh
cp .env.example .env
docker compose up --build -d
```

Open <http://localhost:8080>. Choose an administrator username and set a password of at least 8 characters. The wizard closes after account creation. Importing starts only after setup is complete and an SSH connection is configured.

The default port binds to the Docker host's loopback interface. The Compose project and container are named `unifi-sms` and `unifi-sms-app`. The locally built image is `unifi-sms:0.1.0`.

To build both common Linux architectures locally with a Docker image store that supports multiple platforms:

```sh
docker buildx build --platform linux/amd64,linux/arm64 --load -t unifi-sms:0.1.0 .
```

To transfer an image without a registry, use `docker save -o unifi-sms.tar unifi-sms:0.1.0`, copy the archive to the destination, and run `docker load -i unifi-sms.tar`. Use `docker compose up -d --no-build` on that host. The destination also needs `compose.yaml` and a copy of `.env.example` saved as `.env`. Configure device access in the interface.

## Connect an LTE Pro

Sign in as an administrator. If no device is configured, the connection wizard opens automatically.

1. Select a discovered device, or enter its address manually. Search combines UniFi discovery on UDP port 10001 with unauthenticated SSH banner checks on TCP port 22. Search also uses reverse DNS (PTR) to show device hostnames and places names matching LTE Pro first. **Likely LTE Pro** means the hostname suggests the model; DNS names can change and do not establish device identity. SSH results are candidates: the authenticated check confirms the LTE model before trust or SMS import. Search can take just over a minute and lists the subnets it checked. It limits simultaneous SSH connections and waits for delayed connection and banner responses to avoid missing devices behind container networking. If DNS is unavailable or slow, candidates still appear without hostnames. In Docker, automatic search also tries four common LAN subnets because bridge networking does not expose the host LAN. For another network, enter its private IPv4 subnet under **LAN subnet** (at most 256 addresses). Enable SSH on the device. Enter its address manually if SSH uses a different port or the network blocks discovery.
2. Enter the SSH port and username. Choose a password, paste an unencrypted private key, or generate an RSA key in the application.
3. For a generated key, copy the public key and install it in UniFi Network under **Settings → Devices → Device Updates and Settings → Device SSH Settings**. Older versions use **Settings → System → Advanced → Device Authentication → SSH Keys**. Add an SSH key, paste the public key, and apply the changes. The wizard includes these instructions and a link to [Ubiquiti's SSH key guide](https://help.ui.com/hc/en-us/articles/235247068-Adding-SSH-Keys-to-UniFi-Devices).
4. Select **Check connection**. Review the SSH fingerprint, connected IP address, model, device name, and SIM phone number when available. Select **Trust device and start imports** to activate it. A check expires after ten minutes; changing credentials requires another check.

Only an explicitly trusted configuration is used for SMS import. The application writes the confirmed host key to `/data/known_hosts` with owner-only permissions and checks it on later connections. A changed or missing host key stops connections. Passwords and private keys are encrypted in SQLite using the application's persistent secret key; they are never shown again in the form. Keep the data volume and its backups private, and preserve `/data/secret-key` when restoring the database.

To replace a device or change its access details, open **Settings → Device → Change device connection**. The current connection stays active until the replacement is checked and trusted. Canceling the wizard discards its draft; drafts expire after 24 hours. Device access and SMTP are configured in the interface. Legacy `LTE_*` and `SMTP_*` environment variables are ignored.

## Device information

The inbox shows the configured SSH address, the hardware model read from `board.name` in `/etc/board.info`, the phone number stored on the SIM, and the time of the last successful SIM check. Device details are visible only to signed-in administrators. The worker reads the SIM number with `AT+CNUM` after administrator setup, checks it automatically every hour, and saves the result in SQLite. Some SIM cards do not store a number; the inbox shows **Not available** when the device returns none.

Select **Update from device** to refresh the model and SIM number, for example after replacing the SIM. The button shows **Updating…** until the worker finishes. A request waits if the worker is importing messages or forwarding email. Number checks use the same modem lock as SMS imports.

If a check fails, the inbox keeps the last saved number and labels it as outdated. A successful empty response clears the previous number. Changing the configured device address or SSH port uses a separate cached result. Reading the number does not change the number stored on the SIM. Model checks run independently of SIM number checks. If the model cannot be read, the inbox keeps its saved value and shows that the update failed.

## Forward SMS by email

Open **Settings → Email forwarding** to configure the optional SMTP server, port, encryption, sender, and recipients. Settings are stored in SQLite and take effect after saving. Use STARTTLS on port 587 or implicit TLS on port 465 as required by your provider. Leave both credentials empty only for a server that accepts mail without authentication.

The form shows whether a password is saved without displaying it. Choose **Replace password** to enter a new one, or **Remove login details** to clear both the username and password. Changes apply only after **Save settings**. The SMTP password is stored in the private SQLite volume; protect that volume and its backups.

Select **Send test email** to check the current form values without saving them. The test sends a separate message to each configured recipient, even while forwarding is disabled. A blank password uses the saved password. Success means the server accepted the message; check the recipient inbox and spam folder for delivery.

Only messages completed while forwarding is enabled enter the persistent queue. Existing complete messages are not sent retroactively. Multipart SMS wait until all parts arrive. Each email includes the SMS sender, received timestamp with its UTC offset, and full text. Recipients receive separate copies.

SMTP delivery runs after modem import. Delivery failures leave the SMS archive and device cleanup unaffected. Retries start after one minute and back off to one hour; the worker checks once a minute. Disabling forwarding pauses queued messages. Pending messages use current server and authentication settings, while recipient addresses are fixed when the first delivery attempt starts. Deleting an archived SMS also cancels its queued delivery unless a send is already in progress.

Successful recipients are recorded separately, so a retry after a partial failure does not resend to them. SMTP cannot guarantee exactly-once delivery: a crash after server acceptance but before recording success may cause a duplicate. Retries retain the same Message-ID for each recipient. The Settings page shows pending delivery counts and the latest failure. A successful send means the SMTP server accepted the message, not that it reached the recipient's inbox.

## Import and deletion behavior

Each scan selects storage with `AT+CPMS`, reads PDUs with `AT+CMGL=4`, and supports zero-based slot numbers. Text decoding covers GSM-7, its standard extension alphabet, UCS-2, numeric and alphanumeric senders, and multipart headers with 8-bit or 16-bit references.

Each segment is committed to SQLite with its original PDU before any deletion. The importer rereads the slot using `AT+CMGR` and compares the complete PDU, then deletes only that slot with `AT+CMGD`. SQLite uses WAL and synchronous `FULL`. A local file lock and a device-side lock prevent concurrent instances of this importer from sharing the same modem.

If reading, decoding, verification, or saving fails, the affected slot is retained. A deletion failure leaves the archive intact and is retried on the next scan. A scan also reconciles archived segments that have already disappeared from the device. Unsupported binary messages, compressed messages, language-shift headers, and malformed PDUs remain on the device and produce a warning in the inbox.

Multipart segments are grouped by device, sender, reference, part count, and a 24-hour timestamp window. Missing parts are shown explicitly. Reference reuse within that window can be ambiguous; original PDUs are retained with the archive. Exact repeated PDUs are deduplicated.

Deleting messages in the web interface removes their archive content. This action is blocked while any segment still awaits confirmed device cleanup. Minimal fingerprint receipts remain to prevent deleted messages from reappearing after a replay; they contain no message text after deletion. SQLite deletion is not forensic erasure of database pages or backups.

Only one service instance should own the database volume. Avoid running another SMS reader or deletion tool against the modem at the same time: external tools and firmware do not honor the service's lock, and a serial modem offers no atomic compare-and-delete operation. The modem's original text/PDU mode and serial settings are restored when the session closes. The selected SMS storage remains configured on the device.

## Operations

The inbox shows importer state, pending device cleanup, and the last successful scan. A stopped importer becomes stale in the UI. `/healthz/` checks the web process and SQLite connectivity; modem failures appear in the inbox without making the HTTP service unhealthy. The container exits if either its web server or importer exits, allowing the restart policy to recover it.

The named volume `unifi-sms_sms-data` holds the database and Django secret key. Keep it when replacing the container. `docker compose down` preserves it; `docker compose down -v` destroys it.

To back up a consistent, complete data directory:

```sh
docker compose stop
docker cp unifi-sms-app:/data ./unifi-sms-backup
docker compose start
```

Protect this backup: it contains SMS content, authentication secrets, saved SSH credentials, and any SMTP password saved in Settings. Restore the entire directory into the data volume of a stopped instance, retaining UID 10001 ownership. Do not replace the persistent secret key during a routine upgrade.

To change a forgotten administrator password:

```sh
docker compose exec app python manage.py changepassword ADMIN_USERNAME
```

A lost SSH session normally releases the device lock. After a device crash, `/tmp/unifi-sms-modem.lock` may need removal through an administrative console; first confirm no importer session is active. The importer never removes another session's lock automatically.

## Development and validation

Use Python 3.12. The image uses Python 3.12 and pinned dependency versions from `requirements.txt`.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py bootstrap
.venv/bin/python manage.py runserver --insecure
# In a second terminal:
.venv/bin/python manage.py runworker
```

`--insecure` serves the collected local development assets with Django's development server. Use the Docker image for deployment.

```sh
.venv/bin/python manage.py test inbox modem
.venv/bin/python manage.py makemigrations --check --dry-run
node --test tests/device_settings.test.cjs
docker build -t unifi-sms:0.1.0 .
```

Tests use synthetic messages and a simulated modem to cover decoding, durable save-before-delete behavior, slot reuse, retry and recovery, multipart assembly, first-run setup, authentication, CSRF, archive deletion, device and SMTP settings precedence, SSH credential encryption, forwarding retries, and local SMTP transport. A Linux-only test also exercises the device-side shell through a virtual serial port, including mode restoration and lock cleanup. End-to-end compatibility must still be verified on the target LTE Pro firmware before relying on unattended import.

PDU fields follow [3GPP TS 23.040](https://www.etsi.org/deliver/etsi_ts/123000_123099/123040/16.00.00_60/ts_123040v160000p.pdf). No controller HTTP API is used for SMS access.
