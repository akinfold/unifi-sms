# UniFi SMS

Archive incoming SMS from a UniFi LTE Pro and free its message slots after saving them to SQLite. Browse, search, read, and delete messages in an administrator-only web inbox, with optional email forwarding.

The Docker image includes the web application and background importer. No external database or additional services are required.

**Installing on a NAS?** Follow the [Synology guide](docs/install-synology.md) or [UGREEN guide](docs/install-ugreen.md) for installation through the NAS web interface.

**Security update:** Release `0.1.1` verifies the SSH host key before sending credentials and upgrades Paramiko to 5.0.0. Upgrade from `0.1.0` to use this connection flow. Devices that only support SHA-1 SSH algorithms require a firmware or SSH configuration update; see the [security policy](SECURITY.md#supported-versions).

## Before you start

- Install Docker with the `docker compose` command on the destination host. The published image supports **Linux AMD64 and ARM64**; 32-bit ARM is not supported.
- Ensure the container can reach the LTE Pro over SSH. Obtain the device's SSH username and password, or access to UniFi Network to add an SSH public key. The device's SSH account may differ from your UniFi web login.
- Use one UniFi SMS instance per device. Imported messages are removed from the device, so keep backups of the archive.

## Quick start with Docker Compose

Release `0.1.1` is public in both registries: **no registry account, token, or source checkout is needed**. Both images support Linux AMD64 and ARM64. This example stores application data in a persistent Docker volume.

| Registry | Image |
| --- | --- |
| [GitHub Container Registry (GHCR)](https://github.com/users/akinfold/packages/container/package/unifi-sms) | `ghcr.io/akinfold/unifi-sms:0.1.1` |
| [Docker Hub](https://hub.docker.com/r/akinfold/unifi-sms/tags) | `docker.io/akinfold/unifi-sms:0.1.1` |

### 1. Create the Compose file

On the Docker host, create a deployment folder:

```sh
mkdir -p unifi-sms
cd unifi-sms
```

Save the following as **`compose.ghcr.yaml`** in that folder. It matches the [supplied Compose file](compose.ghcr.yaml).

```yaml
name: unifi-sms
services:
  app:
    image: ghcr.io/akinfold/unifi-sms:${IMAGE_TAG:-0.1.1}
    container_name: unifi-sms-app
    restart: unless-stopped
    ports:
      - "${BIND_ADDRESS:-127.0.0.1}:${PORT:-8080}:8000"
    environment:
      DATA_DIR: /data
      ALLOWED_HOSTS: "${ALLOWED_HOSTS:-localhost,127.0.0.1,[::1]}"
      CSRF_TRUSTED_ORIGINS: "${CSRF_TRUSTED_ORIGINS:-}"
      REQUIRE_HTTPS: "${REQUIRE_HTTPS:-0}"
      TRUST_PROXY: "${TRUST_PROXY:-0}"
    volumes:
      - sms-data:/data
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    stop_grace_period: 45s
volumes:
  sms-data:
```

The Compose example uses GHCR. To use Docker Hub, replace only the `image:` line:

```yaml
    image: docker.io/akinfold/unifi-sms:${IMAGE_TAG:-0.1.1}
```

Keep the `compose.ghcr.yaml` filename and use the same commands below with either registry. Keep the data volume and other settings unchanged when switching registries.

### 2. Start the service

Run these commands from the deployment folder:

```sh
docker compose -f compose.ghcr.yaml pull
docker compose -f compose.ghcr.yaml up -d
docker compose -f compose.ghcr.yaml ps
```

The container is named `unifi-sms-app`. Wait for it to report **healthy**. If it exits or remains unhealthy, inspect its logs:

```sh
docker compose -f compose.ghcr.yaml logs --tail=100 app
```

### 3. Open the application and create an administrator

If Docker runs on your computer, open [http://localhost:8080](http://localhost:8080).

If Docker runs on another server, run this command **on your computer**, replacing `USER` and `SERVER` with the server's SSH account and address. Keep the tunnel open while using the application:

```sh
ssh -N -L 8080:127.0.0.1:8080 USER@SERVER
```

Then open [http://localhost:8080](http://localhost:8080). The default configuration listens only on the Docker host's loopback interface.

On **Create administrator**, choose an application username and a password of at least **8 characters**. Common or entirely numeric passwords are rejected. There is no default account or setup token. Account creation closes after the first administrator is registered.

For direct access through a LAN address instead of a tunnel, see [LAN and HTTPS access](#lan-and-https-access).

## Connect the LTE Pro

The device wizard opens automatically when no device is configured. You can also open **Settings → Device**.

1. Enter the device's IP address or hostname, or select **Search local network**. If search finds nothing, enter the address manually; Docker networking can limit discovery. For a different subnet, use **LAN subnet** with a private IPv4 network of at most 256 addresses, such as `10.20.30.0/24`.
2. Enter the SSH port, usually `22`, and the **device's SSH username**. Choose password or private-key authentication. For a key, paste an unencrypted private key or select **Generate SSH key**. Add the generated **public** key in UniFi Network using the wizard's instructions before continuing. See the [first-launch guide](docs/nas-first-run.md#step-3--obtain-the-devices-ssh-account) for credential and key instructions.
3. Select **Check connection** to read the host key without authenticating. Verify its SSH fingerprint through a trusted device console or administrator, then choose **Confirm fingerprint and check access**. The application authenticates using that key and reads the model, name, and SIM phone number when available.
4. Select **Trust device and start imports**. The application saves the host key automatically and checks it on later connections; you do not need to prepare a `known_hosts` file.

The worker scans for SMS about once a minute. Check the importer status in the inbox to confirm it is working. The inbox shows received dates, the device address and model, and the SIM phone number when available. **Update from device** refreshes device information. Some SIM cards do not report their number; this does not prevent SMS import.

To change devices or credentials, select **Settings → Device → Change device connection**. The current connection stays active until the replacement is checked and trusted.

## Set up optional email forwarding

1. Open **Settings → Email forwarding**.
2. Enter your provider's SMTP host, port, encryption mode, login details, sender, and recipients. Match the provider's requirements; STARTTLS commonly uses port `587`, while implicit TLS uses `465`.
3. Select **Send test email** and check the recipient inbox. The test uses the current form values without saving them; a blank password uses the saved password.
4. Select **Enable email forwarding**, then **Save settings**.

Only new, complete SMS messages are queued while forwarding is enabled; existing messages are not forwarded retroactively. Failed deliveries are retried without blocking SMS archiving. See [email delivery behavior](docs/behavior.md#email-delivery) for retry and duplicate-delivery limits.

**Device access and SMTP are configured in the web interface.** Legacy `LTE_*` and `SMTP_*` environment variables are ignored.

## Keep data and update the service

The volume **`unifi-sms_sms-data`**, mounted at `/data`, contains the SMS database, administrator accounts, saved credentials, trusted host keys, and persistent application secret. Keep the entire volume when replacing the container. `docker compose -f compose.ghcr.yaml down` preserves it; adding **`-v` deletes it**.

Before an upgrade, make a [complete backup](docs/nas-maintenance.md#back-up-the-complete-data-volume). The backup and restore commands also apply to this Docker installation. Copying the Compose file or exporting the image does not back up the data. Protect backups as secrets and keep `secret-key` with the database so saved device credentials remain usable.

To upgrade, set `IMAGE_TAG` to the desired published version in a `.env` file beside `compose.ghcr.yaml`, then run:

```sh
docker compose -f compose.ghcr.yaml pull
docker compose -f compose.ghcr.yaml up -d
docker compose -f compose.ghcr.yaml ps
```

Available tags are listed on [GHCR](https://github.com/users/akinfold/packages/container/package/unifi-sms) and [Docker Hub](https://hub.docker.com/r/akinfold/unifi-sms/tags). Version tags keep upgrades explicit; `latest` can change between pulls.

To reset an existing administrator's password, replace `ADMIN_USERNAME` and run:

```sh
docker compose -f compose.ghcr.yaml exec app python manage.py changepassword ADMIN_USERNAME
```

## LAN and HTTPS access

Before an administrator exists, anyone who can reach setup can create that first account. Complete setup before sharing access.

For LAN access, create or edit `.env` beside `compose.ghcr.yaml`. Replace the example IP with an address assigned to the Docker host:

```dotenv
BIND_ADDRESS=10.20.30.10
PORT=8080
ALLOWED_HOSTS=10.20.30.10,localhost,127.0.0.1
```

Run `docker compose -f compose.ghcr.yaml up -d` again and open `http://10.20.30.10:8080` using your actual address. Allow the chosen port through the host firewall only for intended clients.

For HTTPS, follow the [reverse-proxy instructions](docs/nas-maintenance.md#optional-https-access). Set `REQUIRE_HTTPS=1`, `TRUST_PROXY=1`, and `CSRF_TRUSTED_ORIGINS` only after configuring a trusted HTTPS proxy and restricting direct backend access. `ALLOWED_HOSTS` takes hostnames or IP addresses without schemes or ports; `CSRF_TRUSTED_ORIGINS` takes full origins such as `https://sms.example.com`.

## Troubleshooting

| Symptom | First check |
| --- | --- |
| Image download fails | Check DNS and outbound HTTPS to the selected registry, or switch the `image:` line to the other registry. Both images are public. |
| Device search finds nothing | Enter the device IP manually; check SSH access and routing from the Docker host. |
| SSH authentication fails | Use the managed device's SSH credentials and confirm any public key has been applied. |
| SSH host key changed | Verify the replacement independently, then approve it through the device wizard. |
| HTTP 400 / DisallowedHost | Add the browser's hostname or IP to `ALLOWED_HOSTS` and recreate the container. |
| Healthy container, but no SMS | Check importer status in the inbox. Container health checks the web service and database, not device connectivity. |
| A SIM number is unavailable | Some SIM cards do not store a phone number. SMS import can still work. |

## More documentation

- [First launch and SSH-key setup](docs/nas-first-run.md)
- [Backups, migration, upgrades, HTTPS, and troubleshooting](docs/nas-maintenance.md)
- [Discovery, credentials, SMS cleanup, and email delivery behavior](docs/behavior.md)
- [Source builds, local development, and validation](docs/development.md)
- [Release publishing to GHCR and Docker Hub](docs/releases.md)

## Contributing and security

See [Contributing](CONTRIBUTING.md) for development and pull requests, and the [Security policy](SECURITY.md) for private vulnerability reports and deployment boundaries. This project is distributed under the [MIT License](LICENSE). It is not affiliated with or endorsed by Ubiquiti.
