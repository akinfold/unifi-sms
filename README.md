# UniFi SMS

A private SMS inbox for your UniFi LTE Pro. Keep a searchable archive, forward new messages to email, and free the device's SMS slots automatically — from one Docker container.

[![CI](https://github.com/akinfold/unifi-sms/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/akinfold/unifi-sms/actions/workflows/ci.yml)
[![GitHub release](https://img.shields.io/github/v/release/akinfold/unifi-sms?logo=github)](https://github.com/akinfold/unifi-sms/releases/latest)
[![Docker Hub pulls](https://img.shields.io/docker/pulls/akinfold/unifi-sms?logo=docker&logoColor=white)](https://hub.docker.com/r/akinfold/unifi-sms)
[![Platforms: AMD64 and ARM64](https://img.shields.io/badge/platforms-linux%2Famd64%20%7C%20linux%2Farm64-blue?logo=docker&logoColor=white)](https://hub.docker.com/r/akinfold/unifi-sms/tags)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Install the image, create your administrator, and connect your LTE Pro in the guided web setup.** No external database, source checkout, or registry account required.

- **Keep the messages, free the slots.** SMS are saved to a local SQLite archive before their copies are removed from the device.
- **Find what you need.** Search your inbox, check received dates, read a message, or delete selected messages from the archive.
- **Get new SMS by email.** Optional SMTP forwarding brings new messages to your existing inbox, with a test-email button to check your settings.
- **Set up SSH in the browser.** Find the device or enter its address, use a password or SSH key, and approve its fingerprint. The wizard can generate a key and explain how to add it to UniFi Network.
- **Run it on your server or NAS.** The web application and background importer share one container and one persistent data volume.

## Choose your installation

| Where you want to run it | Start here |
| --- | --- |
| A computer or server with Docker Compose | [Quick start below](#quick-start-with-docker-compose) |
| Synology NAS with Container Manager | [Synology installation guide](docs/install-synology.md) |
| UGREEN NAS with the Docker app | [UGREEN installation guide](docs/install-ugreen.md) |

The images support **Linux AMD64 and ARM64**. NAS support depends on the model and available container apps; see the guides for requirements. 32-bit ARM is not supported.

## Quick start with Docker Compose

You need Docker Compose and a UniFi LTE Pro reachable from the container over SSH. Have the **device's SSH credentials** ready, or access to UniFi Network to add a public key. These may differ from your UniFi web login. Use one UniFi SMS instance per device.

### 1. Start the container

Run on the Docker host:

```sh
mkdir -p unifi-sms
cd unifi-sms
curl -fsSL https://raw.githubusercontent.com/akinfold/unifi-sms/v0.1.3/compose.ghcr.yaml -o compose.yaml
docker compose up -d
```

This downloads the release Compose file and starts the public `0.1.3` image from GHCR. Docker downloads the image automatically. Prefer Docker Hub? [Switch the image below](#choose-your-registry).

### 2. Create your administrator

Open **[http://localhost:8080](http://localhost:8080)** on the computer running Docker. On a remote server, [open an SSH tunnel or enable LAN access](#access-from-another-computer) first.

Choose an application username and a password of at least **8 characters**. Common or entirely numeric passwords are rejected. There is no default login or setup token; registration closes after the first administrator is created.

### 3. Connect your LTE Pro

The connection wizard opens automatically:

1. Select **Search local network** or enter the device's IP address or hostname. If discovery finds nothing, enter the address manually.
2. Enter the SSH port and device username, then choose a password or private key. You can paste your key or select **Generate SSH key** and follow the instructions to add its public part to UniFi Network.
3. Select **Check connection**. Verify the displayed fingerprint through a trusted device console or administrator, then select **Confirm fingerprint and check access**.
4. Review the device information and select **Trust device and start imports**. The application saves the trusted host key and checks it on future connections — no manual `known_hosts` file needed.

**Your inbox is ready.** The importer checks for SMS about once a minute. The inbox shows its status, received dates, device address and model, and the SIM phone number when available. Use **Update from device** to refresh device information.

Need help finding SSH credentials or adding a key? Follow the [step-by-step first-launch guide](docs/nas-first-run.md). To change the connection later, open **Settings → Device → Change device connection**.

## Choose your registry

Both images are public and support the same architectures. No registry login is needed.

| Registry | Image |
| --- | --- |
| [GitHub Container Registry](https://github.com/users/akinfold/packages/container/package/unifi-sms) | `ghcr.io/akinfold/unifi-sms:0.1.3` |
| [Docker Hub](https://hub.docker.com/r/akinfold/unifi-sms) | `docker.io/akinfold/unifi-sms:0.1.3` |

To use Docker Hub, replace the `image:` line in `compose.yaml` with:

```yaml
    image: docker.io/akinfold/unifi-sms:${IMAGE_TAG:-0.1.3}
```

Run `docker compose up -d` again. Keep the same Compose project and data volume when switching registries.

## Forward new SMS to email

Open **Settings → Email forwarding**, enter your SMTP settings, sender, and recipients, then select **Send test email**. Once the test arrives, select **Enable email forwarding** and **Save settings**.

The test uses the current form values without saving them; leave the password blank to use the saved password. New, complete messages are queued while forwarding is enabled. Existing messages are not sent retroactively. Failed deliveries are retried without blocking archiving; see [email delivery behavior](docs/behavior.md#email-delivery) for details.

Device access and SMTP are configured in the web interface. No connection settings are needed in `.env`; legacy `LTE_*` and `SMTP_*` variables are ignored.

## Access from another computer

The supplied Compose file listens only on the Docker host's loopback interface. For a remote server, the quickest way to complete setup is an SSH tunnel. Run this **on your computer**, replacing `USER` and `SERVER` with the server's SSH account and address:

```sh
ssh -N -L 8080:127.0.0.1:8080 USER@SERVER
```

Keep the tunnel open and visit [http://localhost:8080](http://localhost:8080).

For direct LAN access, create `.env` beside `compose.yaml`. Replace the example IP with an address assigned to the **Docker host**:

```dotenv
BIND_ADDRESS=10.20.30.10
PORT=8080
ALLOWED_HOSTS=10.20.30.10,localhost,127.0.0.1
```

Run `docker compose up -d` again, then open `http://10.20.30.10:8080` using your actual address. Restrict access to intended clients. **Complete administrator setup before sharing access:** anyone who can reach an unconfigured instance can create its first administrator.

For HTTPS, follow the [reverse-proxy guide](docs/nas-maintenance.md#optional-https-access). It covers the required proxy and application settings.

## Keep your archive and stay up to date

**Imported SMS are removed from the LTE Pro, so back up the archive.** The `unifi-sms_sms-data` volume contains the database, accounts, credentials, trusted host keys, and application secret. Back up the **entire volume** and protect backups as secrets; see [backup and restore](docs/nas-maintenance.md#back-up-the-complete-data-volume).

Replacing the container preserves this volume. `docker compose down` preserves it too; **`docker compose down -v` deletes it**.

To upgrade, back up first, then set `IMAGE_TAG` to the desired published version in `.env` beside `compose.yaml` and run:

```sh
docker compose pull
docker compose up -d
```

Choose a version from [GitHub releases](https://github.com/akinfold/unifi-sms/releases) or [Docker Hub tags](https://hub.docker.com/r/akinfold/unifi-sms/tags). Version tags make upgrades explicit; `latest` can change between pulls.

Upgrading from `0.1.0`? Release `0.1.1` verifies the SSH host key before sending credentials. Devices limited to SHA-1 SSH algorithms need a firmware or SSH configuration update; see the [security policy](SECURITY.md#supported-versions).

## Troubleshooting

Check the container with `docker compose ps`. For startup errors, run `docker compose logs --tail=100 app`.

| What you see | What to do |
| --- | --- |
| Image download fails | Check DNS and outbound HTTPS, or switch to the other public registry. |
| Device search finds nothing | Enter its address manually. Discovery can be limited by Docker networking; check SSH access from the Docker host. |
| SSH authentication fails | Use the device's SSH account and confirm that any public key has been applied. See the [SSH setup guide](docs/nas-first-run.md#step-3--obtain-the-devices-ssh-account). |
| SSH host key changed | Verify the new fingerprint independently, then approve it in the device wizard. |
| HTTP 400 / DisallowedHost | Add the browser's hostname or IP to `ALLOWED_HOSTS`, then run `docker compose up -d`. |
| Container is healthy, but no SMS appear | Check importer status in the inbox. Container health checks the web service and database, not the device connection. |
| SIM phone number is missing | Some SIM cards do not store their number. SMS import still works. |

## Documentation and support

- [First launch, SSH credentials, and key setup](docs/nas-first-run.md)
- [Backups, migration, upgrades, HTTPS, and password recovery](docs/nas-maintenance.md)
- [Discovery, SMS cleanup, and email delivery: behavior and limits](docs/behavior.md)
- [Source builds, development, and validation](docs/development.md)
- [Release publishing and registry descriptions](docs/releases.md)
- [Report a bug or suggest a feature](https://github.com/akinfold/unifi-sms/issues)

Contributions are welcome — see [Contributing](CONTRIBUTING.md). Report vulnerabilities privately using the [Security policy](SECURITY.md).

Released under the [MIT License](LICENSE). UniFi SMS is an independent project, not affiliated with or endorsed by Ubiquiti.
