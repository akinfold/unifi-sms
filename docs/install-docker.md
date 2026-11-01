# Install UniFi SMS with Docker Compose

Run the published image on a computer or server with Docker Compose. For a NAS installation through its web interface, use the [Synology guide](install-synology.md) or [UGREEN guide](install-ugreen.md).

You need Docker Compose and a UniFi LTE Pro reachable from the container over SSH. Have the LTE Pro's IP address and **device SSH credentials** ready, or access to UniFi Network to add a public key. The device credentials may differ from your UniFi web login. Use one UniFi SMS instance per device.

## 1. Start the container

Run on the Docker host:

```sh
mkdir -p unifi-sms
cd unifi-sms
curl -fsSL https://raw.githubusercontent.com/akinfold/unifi-sms/main/compose.ghcr.yaml -o compose.yaml
docker compose up -d
```

This downloads the Compose file and starts the public `latest` image from GHCR. The file uses [`pull_policy: always`](https://docs.docker.com/reference/compose-file/services/#pull_policy) to check for an updated image on each deployment. Prefer Docker Hub? [Switch the image below](#choose-your-registry).

The default host port is **8098**; the container listens on **8000**. If Docker reports that port 8098 is already allocated, [choose another host port](#choose-another-host-port), then repeat the startup command.

## 2. Create your administrator

Open **[http://localhost:8098](http://localhost:8098)** on the computer running Docker, using your chosen host port if you changed it. On a remote server, [open an SSH tunnel or enable LAN access](#access-from-another-computer) first.

Choose an application username and a password of at least **8 characters**. Common or entirely numeric passwords are rejected. There is no default login or setup token; registration closes after the first administrator is created.

## 3. Connect your LTE Pro

The connection wizard opens automatically:

1. Enter the LTE Pro's IP address or hostname. If you do not know it, use **Search local network**; Docker networking can limit discovery.
2. Enter the SSH port and device username, then choose a password or private key. You can paste your key or select **Generate SSH key** and follow the instructions to add its public part to UniFi Network.
3. Select **Check connection**. Verify the displayed fingerprint through a trusted device console or administrator, then select **Confirm fingerprint and check access**.
4. Review the device information. Starting imports saves SMS to the archive and then deletes their copies from the device to free its slots. Select **Trust device and start imports** when ready. The application saves the trusted host key and checks it on future connections — no manual `known_hosts` file needed.

**Verify your first import.** Send a test SMS to the LTE Pro's SIM. The importer checks for SMS about once a minute. Open the message and look for **Saved in your archive. Device slot cleared.** If cleanup is pending, check the importer status and the next scan. The inbox shows its status, received dates, device address and model, and the SIM phone number when available. Use **Update from device** to refresh device information.

Need help finding SSH credentials or adding a key? Follow the [step-by-step first-launch guide](nas-first-run.md). To change the connection later, open **Settings → Device → Change device connection**.

## Choose your registry

Both images are public and support the same architectures. No registry login is needed.

| Registry | Image |
| --- | --- |
| [GitHub Container Registry](https://github.com/users/akinfold/packages/container/package/unifi-sms) | `ghcr.io/akinfold/unifi-sms:latest` |
| [Docker Hub](https://hub.docker.com/r/akinfold/unifi-sms) | `docker.io/akinfold/unifi-sms:latest` |

To use Docker Hub, replace the `image:` line in `compose.yaml` with:

```yaml
    image: docker.io/akinfold/unifi-sms:${IMAGE_TAG:-latest}
```

Run `docker compose up -d` again. Keep the same Compose project and data volume when switching registries.

## Choose another host port

If Docker reports **Port is already allocated** or **Address already in use**, create or edit `.env` beside `compose.yaml`. Set `PORT` to a free host port, for example:

```dotenv
PORT=18098
```

**18098** is an example; choose another port if it is also busy. Keep any other existing settings in `.env`. From the same directory, apply the change:

```sh
docker compose up -d
docker compose ps
```

Compose recreates the container with the new mapping and keeps its data volume. `docker compose restart` does not apply the change. Keep container port **8000** unchanged.

Once the container is running, open `http://localhost:18098` on the Docker host. Update browser bookmarks, direct REST or MCP URLs, SSH tunnels, and reverse-proxy backend settings that use the old port. If you enabled LAN access and a firewall restricts it, allow your chosen TCP port from trusted clients.

## Access from another computer

The supplied Compose file listens only on the Docker host's loopback interface. For a remote server, the quickest way to complete setup is an SSH tunnel. Run this **on your computer**, replacing `USER` and `SERVER` with the server's SSH account and address:

```sh
ssh -N -L 8098:127.0.0.1:8098 USER@SERVER
```

Keep the tunnel open and visit [http://localhost:8098](http://localhost:8098). If you changed the server's host port, replace the final `8098` in the tunnel command with that port. If port 8098 is busy on your computer, change the first `8098` to a free local port and use that port in the browser URL.

For browser and integration access over a network, use the [HTTPS reverse-proxy guide](nas-maintenance.md#optional-https-access). HTTP sends passwords, API keys, and SMS without transport encryption. For temporary HTTP access on a private network you control, create or edit `.env` beside `compose.yaml`. Keep any existing settings, including a custom `PORT`. Replace the example IP with an address assigned to the **Docker host**:

```dotenv
BIND_ADDRESS=10.20.30.10
PORT=8098
ALLOWED_HOSTS=10.20.30.10,localhost,127.0.0.1
```

Run `docker compose up -d` again, then open `http://10.20.30.10:8098` using your actual address and configured host port. Restrict access to intended clients. **Complete administrator setup before sharing access:** anyone who can reach an unconfigured instance can create its first administrator.


## Back up and upgrade

Keep your existing host port when upgrading. If you replace an older Compose file that defaulted to **8080**, set `PORT=8080` in `.env` before redeploying to retain that address. Existing `PORT` settings continue to override the new default.

Imported SMS are removed from the LTE Pro, so [back up the complete data volume](nas-maintenance.md#back-up-the-complete-data-volume) before upgrading. Keep the same project and volume when recreating the container. `docker compose down` preserves the volume; **`docker compose down -v` deletes it**.

```sh
docker compose pull
docker compose up -d
```

`latest` tracks the latest stable release. Read the [release notes](https://github.com/akinfold/unifi-sms/releases) before upgrading. Existing containers update when you redeploy them, not in the background. If an older installation sets `IMAGE_TAG` in `.env`, remove that entry to use `latest`. See [maintenance and recovery](nas-maintenance.md) for migration, restoration, and password recovery.

Upgrading from `0.1.0`? Release `0.1.1` verifies the SSH host key before sending credentials. Devices limited to SHA-1 SSH algorithms need a firmware or SSH configuration update; see the [security policy](../SECURITY.md#supported-versions).

## Troubleshooting

Check the container with `docker compose ps`. For startup errors, run `docker compose logs --tail=100 app`.

| What you see | What to do |
| --- | --- |
| Port is already allocated / address already in use | [Set a free host port in `.env`](#choose-another-host-port), then run `docker compose up -d` again. |
| Image download fails | Check DNS and outbound HTTPS, or switch to the other public registry. |
| Device search finds nothing | Enter its address manually. Discovery can be limited by Docker networking; check SSH access from the Docker host. |
| SSH authentication fails | Use the device's SSH account and confirm that any public key has been applied. See the [SSH setup guide](nas-first-run.md#step-3-obtain-the-devices-ssh-account). |
| SSH host key changed | Verify the new fingerprint independently, then approve it in the device wizard. |
| HTTP 400 / DisallowedHost | Add the browser's hostname or IP to `ALLOWED_HOSTS`, then run `docker compose up -d`. |
| Container is healthy, but no SMS appear | Check importer status in the inbox. Container health checks the web service and database, not the device connection. |
| SIM phone number is missing | Some SIM cards do not store their number. SMS import still works. |

## Next steps

- [Forward new SMS to email](../README.md#forward-new-sms-to-email).
- [Connect REST API, MCP, or webhooks](integrations.md).
- [Read import and delivery behavior](behavior.md).
