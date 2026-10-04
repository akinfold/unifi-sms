# UniFi SMS

A small Django service that archives incoming UniFi LTE Pro SMS messages in SQLite and clears their modem slots after the database transaction commits. It provides an administrator-only inbox with search, unread filtering, a message reader, and bulk deletion.

The image runs Gunicorn and one supervised importer. No Redis, task queue, frontend build, or external database is required.

## Quick start

```sh
cp .env.example .env
mkdir -p secrets
docker compose up --build -d
```

Open <http://localhost:8080>. Choose an administrator username and set a password of at least 8 characters. The wizard closes after account creation. Importing starts only after setup is complete and an SSH connection is configured.

The default port binds to the Docker host's loopback interface. The Compose project and container are named `unifi-sms` and `unifi-sms-app`. The locally built image is `unifi-sms:0.1.0`.

To build both common Linux architectures locally with a Docker image store that supports multiple platforms:

```sh
docker buildx build --platform linux/amd64,linux/arm64 --load -t unifi-sms:0.1.0 .
```

To transfer an image without a registry, use `docker save -o unifi-sms.tar unifi-sms:0.1.0`, copy the archive to the destination, and run `docker load -i unifi-sms.tar`. Use `docker compose up -d --no-build` on that host. The destination also needs `compose.yaml`, `.env`, and its own SSH secret files.

## Connect an LTE Pro

The integration uses SSH to run AT commands against the device's modem serial port. It requires working SSH access with a private key and permission to access `/dev/ttyUSB2`, run `stty`, and create a lock directory in `/tmp`. SSH availability and configuration depend on the firmware. The service does not enable SSH or change controller configuration.

Provision a dedicated SSH key using the device's supported administration mechanism. Put its private key at `secrets/lte_key` and a verified SSH host key at `secrets/known_hosts`. Verify the host-key fingerprint through a trusted device console before accepting it; the application rejects unknown or changed host keys. It does not use SSH passwords or silently accept new host keys.

The container runs as UID 10001. Ensure that user can traverse the secrets directory and read both files. On Linux, for example:

```sh
chmod 755 secrets
sudo chown 10001:10001 secrets/lte_key
chmod 400 secrets/lte_key
chmod 644 secrets/known_hosts
```

Docker Compose reads `.env` beside `compose.yaml` and passes its values to the application as environment variables. The application does not discover the device or read SSH credentials from the controller. It mounts the local `secrets` directory at `/run/secrets` inside the container.

Set your device address and SSH username in `.env`. For example, replace these placeholders with your device's values:

```dotenv
LTE_HOST=lte-device.example.invalid
LTE_PORT=22
LTE_USERNAME=your-ssh-user
LTE_KEY_FILE=/run/secrets/lte_key
LTE_KNOWN_HOSTS=/run/secrets/known_hosts
```

The web administrator account is separate from the SSH account. The setup wizard creates only the web account. Apply changes to `.env` by recreating the container:

```sh
docker compose up -d --force-recreate
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `LTE_HOST` | Empty | Device hostname or address; empty disables import |
| `LTE_USERNAME` | Empty | SSH user with modem access |
| `LTE_PORT` | `22` | SSH port |
| `LTE_DEVICE` | `/dev/ttyUSB2` | Modem AT-command port |
| `LTE_STORAGE` | `SM` | SIM storage; `ME` is also accepted |
| `LTE_KEY_FILE` | `/run/secrets/lte_key` | Container path to the private key |
| `LTE_KNOWN_HOSTS` | `/run/secrets/known_hosts` | Container path to verified host keys |
| `POLL_INTERVAL` | `60` | Import interval in seconds, minimum 10 |
| `PORT` | `8080` | Host HTTP port in Compose |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Accepted HTTP hostnames |
| `CSRF_TRUSTED_ORIGINS` | Empty | Comma-separated origins, including scheme |
| `REQUIRE_HTTPS` | `0` | Set to `1` when serving through HTTPS |
| `TRUST_PROXY` | `0` | Trust `X-Forwarded-Proto` from a controlled proxy |
| `DATA_DIR` | `/data` in Docker | SQLite database and persistent secrets |

For remote browser access, use a TLS reverse proxy, add its public hostname to `ALLOWED_HOSTS`, set its HTTPS origin in `CSRF_TRUSTED_ORIGINS`, and set `REQUIRE_HTTPS=1`. Keep `127.0.0.1` in `ALLOWED_HOSTS` for the health check. Enable `TRUST_PROXY=1` only when the proxy overwrites the forwarded protocol header and clients cannot bypass it.

## SIM phone number

The inbox shows the phone number stored on the SIM and the time of the last successful check. The worker reads it with `AT+CNUM` after administrator setup, checks it automatically every hour, and saves the result in SQLite. Some SIM cards do not store a number; the inbox shows **Not available** when the device returns none.

Select **Update from device** to request another check, for example after replacing the SIM. The button shows **Updating…** until the worker finishes. A request waits if the worker is importing messages or forwarding email. Number checks use the same modem lock as SMS imports.

If a check fails, the inbox keeps the last saved number and labels it as outdated. A successful empty response clears the previous number. Changing the configured device address, SSH port, or serial port uses a separate cached result. Reading the number does not change the number stored on the SIM.

## Forward SMS by email

SMTP forwarding is optional and disabled by default. Sign in as an administrator, open **Settings** from the inbox, and enable forwarding. Set the SMTP server, port, transport security, sender address, and recipient addresses. Supply a username and password if the server requires authentication. The worker uses saved settings on its next pass; no container restart is needed for changes made in the interface.

To configure forwarding through the container environment instead, add these variables to `.env` and recreate the container with `docker compose up -d --force-recreate`:

```dotenv
SMTP_ENABLED=1
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=relay-user
SMTP_PASSWORD=replace-with-smtp-password
SMTP_FROM=sms@example.com
SMTP_TO=reader@example.com,second-reader@example.com
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `SMTP_ENABLED` | `0` | Enable forwarding with `1`; also accepts `true`, `yes`, or `on` and their false equivalents |
| `SMTP_HOST` | Empty | SMTP server hostname |
| `SMTP_PORT` | `587` | SMTP port, from 1 to 65535 |
| `SMTP_SECURITY` | `starttls` | `starttls`, `tls` for implicit TLS (usually port 465), or `none` |
| `SMTP_USERNAME` | Empty | Authentication username; leave both credentials empty for an unauthenticated relay |
| `SMTP_PASSWORD` | Empty | Authentication password |
| `SMTP_FROM` | Empty | Sender email address |
| `SMTP_TO` | Empty | Comma-separated recipient email addresses |

The presence of **any** listed SMTP variable makes ENV the source for the **entire** SMTP configuration, even when that variable is empty or `SMTP_ENABLED=0`. The Settings page displays the effective values, disables the SMTP fields, and rejects changes on the server. Unspecified fields use the defaults above, never values saved in the database. Invalid ENV settings stop forwarding without blocking SMS import; the Settings page shows validation errors.

To edit SMTP settings in the interface again, remove **all** listed SMTP variables from the container environment and recreate the container. Previously saved database settings become active again. The examples in `.env.example` are commented out so they do not lock the interface by default.

The form shows whether a password is saved without displaying it. Choose **Replace password** to enter a new one, or **Remove login details** to clear both the username and password. Changes apply only after you select **Save settings**; you can cancel a replacement or undo a removal before saving. Settings saved in the interface, including the SMTP password, are stored in the private SQLite volume. Protect that volume and its backups.

Only messages completed while forwarding is enabled enter the persistent queue. Existing complete messages are not sent retroactively. Multipart SMS wait until all parts arrive. Each email includes the SMS sender, received timestamp with its UTC offset, and full text. Recipients receive separate copies.

SMTP delivery runs after modem import. Delivery failures leave the SMS archive and device cleanup unaffected. Retries start after one minute and back off to one hour; actual timing also depends on `POLL_INTERVAL`. Disabling forwarding pauses queued messages. Pending messages use current server and authentication settings, while recipient addresses are fixed when the first delivery attempt starts. Deleting an archived SMS also cancels its queued delivery unless a send is already in progress.

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

Protect this backup: it contains SMS content, authentication secrets, and any SMTP password saved in Settings. Restore the entire directory into the data volume of a stopped instance, retaining UID 10001 ownership. Do not replace the persistent secret key during a routine upgrade.

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
docker build -t unifi-sms:0.1.0 .
```

Tests use synthetic messages and a simulated modem to cover decoding, durable save-before-delete behavior, slot reuse, retry and recovery, multipart assembly, first-run setup, authentication, CSRF, archive deletion, SMTP settings precedence, forwarding retries, and local SMTP transport. A Linux-only test also exercises the device-side shell through a virtual serial port, including mode restoration and lock cleanup. End-to-end compatibility must still be verified on the target LTE Pro firmware before relying on unattended import.

PDU fields follow [3GPP TS 23.040](https://www.etsi.org/deliver/etsi_ts/123000_123099/123040/16.00.00_60/ts_123040v160000p.pdf). No controller HTTP API is used for SMS access.
