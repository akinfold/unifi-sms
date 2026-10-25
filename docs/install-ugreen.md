# Install UniFi SMS on a UGREEN NAS

Run UniFi SMS on a UGREEN NAS with the Docker app. Use a model that offers Docker in App Center and supports Linux AMD64 or ARM64 containers; 32-bit ARM is not supported. UGOS Pro labels can vary by version.

Before you start, have a UGOS Pro administrator account, a stable NAS LAN IP, internet access from the NAS, and the LTE Pro's SSH credentials from the UniFi Network application that manages it. Confirm the NAS can reach the LTE Pro over SSH (usually TCP 22). UniFi SMS and NAS accounts are separate.

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
    image: docker.io/akinfold/unifi-sms:0.1.6
    container_name: unifi-sms-app
    restart: unless-stopped
    ports:
      - "10.20.30.10:8080:8000"
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

Docker Hub is the default public registry. If you prefer GHCR, change only the image line to `ghcr.io/akinfold/unifi-sms:0.1.6`. If port 8080 is busy, change only the host port (`8080`) and use that port in the browser URL; keep container port `8000`. No `.env` file is needed. See [UGREEN's Project guide](https://support.ugnas.com/detail/article/en-US/411).

## 3. Deploy and open the app

Select **Deploy** and wait for the image download and container startup. Open `http://NAS_IP:8080` from a computer on the same LAN, substituting your NAS IP and chosen host port.

## 4. Create the administrator and connect the device

Choose an application username and a password of at least 8 characters. Select the LTE Pro, or enter its IP manually if discovery finds nothing. Enter its SSH password or use a generated key. Verify the host fingerprint through a trusted console or administrator, approve the device, and start importing messages. See [First launch](nas-first-run.md) for the screens and details.

Optional: [forward SMS to email](../README.md#forward-new-sms-to-email) or [enable REST API, MCP, and webhooks](../README.md#connect-scripts-mcp-clients-and-webhooks). All are configured in the browser and disabled by default.

## Data, upgrades, and troubleshooting

The named volume **`unifi-sms_sms-data`** holds the database, settings, administrator account, and application secret. The project folder alone is not a backup. Follow [Back up the complete data volume](nas-maintenance.md#back-up-the-complete-data-volume). For upgrades, follow [Upgrade the image](nas-maintenance.md#upgrade-the-image); do not delete the volume or use **Clean**. For migration, follow [Restore or migrate to another NAS](nas-maintenance.md#restore-or-migrate-to-another-nas). Run only one importer per LTE Pro.

- **Project does not start:** check the project log and confirm the NAS can pull the image.
- **Page does not open:** verify the NAS IP, host port, and LAN firewall rules.
- **Device is not found:** enter its IP manually and verify NAS-to-device SSH access.

**Validation:** Compose was checked for release `0.1.6`, and the project steps follow [UGREEN's official Project guide](https://support.ugnas.com/detail/article/en-US/411). This workflow has not been tested on physical UGREEN hardware.
