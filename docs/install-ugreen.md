# Install UniFi SMS on a UGREEN NAS

Run UniFi SMS on a UGREEN NAS with the Docker app. Use a model that offers Docker in App Center and supports Linux AMD64 or ARM64 containers; 32-bit ARM is not supported. UGOS Pro labels can vary by version.

Before you start, have a UGOS Pro administrator account, a stable NAS LAN IP, internet access from the NAS, the LTE Pro's IP address, and its device SSH credentials from the UniFi Network application that manages it. Your UniFi web login may differ from the device's SSH account. Confirm the NAS can reach the LTE Pro over SSH (usually TCP 22). UniFi SMS and NAS accounts are separate.

The initial setup uses HTTP. Use it only on a trusted private LAN, create the administrator before sharing access, and do not forward the app port from the internet. For remote access, use a VPN or [optional HTTPS access](nas-maintenance.md#optional-https-access).

## 1. Install Docker

In **App Center**, find and install **Docker**, then open it. If Docker is unavailable, confirm that your NAS model supports the app.

## 2. Create a project and paste the Compose file

Open **Docker → Project → Create**, enter `unifi-sms` as the project name, and choose the Compose editor. UGOS Pro creates the project folder under its Docker shared directory; no manual folder or database setup is needed.

Paste this configuration. Replace both instances of `10.20.30.10` with the NAS LAN IP:

```yaml
name: unifi-sms
services:
  app:
    image: docker.io/akinfold/unifi-sms:latest
    pull_policy: always
    container_name: unifi-sms-app
    restart: unless-stopped
    ports:
      - "10.20.30.10:8098:8000"
    environment:
      DATA_DIR: /data
      ALLOWED_HOSTS: "10.20.30.10,localhost,127.0.0.1"
    volumes:
      - sms-data:/data
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    stop_grace_period: 45s
volumes:
  sms-data:
    name: unifi-sms_sms-data
```

The project installs `latest` from Docker Hub. `pull_policy: always` checks the registry for an updated image on each deployment. If you prefer GHCR, change only the image line to `ghcr.io/akinfold/unifi-sms:latest`. No `.env` file is needed. See [UGREEN's Project guide](https://support.ugnas.com/detail/article/en-US/411).

If port **8098** is busy, change only the middle number under `ports`, for example to `10.20.30.10:18098:8000`. Keep your NAS IP and container port **8000**. Open `http://NAS_IP:18098` after deployment. If the project already failed to start, follow [port-conflict recovery](#if-the-port-is-already-in-use).

## 3. Deploy and open the app

Select **Deploy** and wait for the image download and container startup. Open `http://NAS_IP:8098` from a computer on the same LAN, substituting your NAS IP and chosen host port.

## 4. Create the administrator and connect the device

1. Choose an application username and a password of at least **8 characters**. Common or entirely numeric passwords are rejected. There is no default account or setup token.
2. Enter the LTE Pro's IP address in the device wizard. **Search local network** is optional; Docker networking can limit discovery. Enter the device's SSH credentials, or generate a key and add its public key to UniFi Network.
3. Verify the host fingerprint through a trusted console or administrator and check access. Starting imports saves SMS to the archive and deletes their device copies to free slots. Select **Trust device and start imports** when ready.
4. Send a test SMS to the SIM and wait for the next scan; imports run about once a minute. Open the message and look for **Saved in your archive. Device slot cleared.** If cleanup is pending, check the importer status and the next scan.

See [First launch](nas-first-run.md) for SSH credentials, fingerprint verification, and the setup screens.

Optional: [forward SMS to email](../README.md#forward-new-sms-to-email) or [connect REST API, MCP, or webhooks](integrations.md). All are configured in the browser and disabled by default.

## If the port is already in use

For **Port is already allocated** or **Address already in use**:

1. Open **Docker → Project**, select `unifi-sms`, and open **Edit Configuration**.
2. Under `ports`, change `"10.20.30.10:8098:8000"` to `"10.20.30.10:18098:8000"`, using your NAS IP. **18098** is an example; choose another free host port if it is also busy. Keep the final `:8000`.
3. Save the configuration and deploy the updated project so Docker recreates the container. Restarting the container alone does not apply a new port mapping. Keep the same project and data volume; do not delete the volume or use **Clean**.
4. Once `unifi-sms-app` is running, open **`http://NAS_IP:18098`** with your NAS IP and chosen port. If a firewall restricts access, allow that TCP port from your trusted LAN clients. Update any bookmarks, direct REST or MCP URLs, and reverse-proxy backend settings that still use the old port.

## Data, upgrades, and troubleshooting

The named volume **`unifi-sms_sms-data`** holds the database, settings, administrator account, and application secret. The project folder alone is not a backup. Follow [Back up the complete data volume](nas-maintenance.md#back-up-the-complete-data-volume). For upgrades, follow [Upgrade the image](nas-maintenance.md#upgrade-the-image); do not delete the volume or use **Clean**. For migration, follow [Restore or migrate to another NAS](nas-maintenance.md#restore-or-migrate-to-another-nas). Run only one importer per LTE Pro.

- **Project does not start:** check the project log and confirm the NAS can pull the image.
- **Port is already allocated / address already in use:** follow [port-conflict recovery](#if-the-port-is-already-in-use) and deploy the updated project.
- **Page does not open:** verify the NAS IP, host port, and LAN firewall rules.
- **Device is not found:** enter its IP manually and verify NAS-to-device SSH access.
