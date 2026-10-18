# How to install UniFi SMS on a UGREEN NAS

Run a private SMS inbox on your NAS using the published UniFi SMS image. The service archives messages from a UniFi LTE Pro in SQLite and can forward new messages through your SMTP provider. This guide uses the Docker application included with supported UGOS Pro models.

**Applies to:** a UGREEN NAS with **Docker available in App Center** and a Linux AMD64 or ARM64 processor. Do not assume every UGREEN model supports Docker. The image does not support 32-bit ARM. UGOS Pro updates can change button labels.

**Example throughout:** NAS `10.20.30.10`, browser port `8080`. Replace the example IP with your NAS IP.

## STEP 1 — Prepare the NAS and device access

Have these ready:

- A UGOS Pro administrator account and an initialized storage volume with free space.
- The NAS's LAN IP address, preferably kept stable with a DHCP reservation.
- Internet access from the NAS to GitHub Container Registry.
- A route from the NAS to the LTE Pro's SSH port, usually TCP 22.
- Access to the device's UniFi Network application for its SSH username, password, or public-key configuration.

You can check the NAS IP under **Control Panel → Network Settings → Network Connection**, as described in [UGREEN's login guide](https://support.ugnas.com/detail/article/en-US/60).

Complete the initial HTTP setup on a trusted LAN. Do not expose TCP 8080 through router port forwarding. Use a VPN or an HTTPS reverse proxy for access outside the trusted network; see [NAS maintenance](nas-maintenance.md#optional-https-access).

If another UniFi SMS installation already contains your messages, follow [Restore or migrate](nas-maintenance.md#restore-or-migrate-to-another-nas). Do not keep two importers active against the same device.

## STEP 2 — Install the Docker application

Open **App Center**, find **Docker**, and select **Install**. Choose a storage location if prompted, then open Docker.

If Docker is not offered, confirm model support with UGREEN before proceeding. The [official UGREEN Docker guide](https://ai.ugreen.com/blogs/knowledge/docker-docker-compose-ugreen-nas) covers installation and model compatibility. A GitHub account, registry token, and Portainer are unnecessary for this service.

## STEP 3 — Start a Compose project

Open **Docker → Project → Create**. Enter **`unifi-sms`** as the project name. Choose the Compose editor or the option to import a `.yaml`/`.yml` file.

UGOS Pro creates a project folder under the Docker shared directory. You do not need to create a database directory or change folder ownership for this procedure.

The project folder stores the Compose configuration. Application data lives in the Docker volume **`unifi-sms_sms-data`**, mounted at `/data`; it is not stored inside the project folder. Docker initializes the volume for the application's UID/GID `10001:10001`. Do not add a bind mount, `PUID`, `PGID`, or a `user:` override for this procedure.

The volume holds SMS, administrator accounts, saved connection settings, trusted host keys, and the persistent application secret. Recreating the container with the same volume keeps that data. Copying the project folder alone does **not** back it up. [Docker explains how named volumes persist](https://docs.docker.com/engine/storage/volumes/).

## STEP 4 — Paste and customize the Compose configuration

Paste the following YAML into the editor, or import [compose.nas.yaml](compose.nas.yaml).

```yaml
services:
  app:
    image: ghcr.io/akinfold/unifi-sms:0.1.1
    container_name: unifi-sms-app
    restart: unless-stopped
    ports:
      - "10.20.30.10:8080:8000"
    environment:
      DATA_DIR: /data
      ALLOWED_HOSTS: "10.20.30.10,localhost,127.0.0.1"
      CSRF_TRUSTED_ORIGINS: ""
      REQUIRE_HTTPS: "0"
      TRUST_PROXY: "0"
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
name: unifi-sms
```

Before deploying, replace **both occurrences of `10.20.30.10`** with your NAS's LAN IPv4 address. It is an example, not an address to assign to the NAS.

| Setting | What to enter |
| --- | --- |
| `10.20.30.10:8080:8000` | Your NAS IP, the browser port, and the container port. Keep the final `8000`. |
| `ALLOWED_HOSTS` | Your NAS IP. Keep `localhost,127.0.0.1` for local health checks. Use commas without spaces; do not include `http://` or a port. |
| `image` | Keep `ghcr.io/akinfold/unifi-sms:0.1.1` for this installation. Docker selects AMD64 or ARM64 automatically. |
| `sms-data` / `name` | Keep these values for a fresh installation. They identify the persistent data volume. |

If port 8080 is already in use, change only its host-side value, for example to `10.20.30.10:8090:8000`, and use port 8090 in your browser.

There is no `.env` file to create. Device credentials, SSH keys, host trust, and SMTP settings are entered in UniFi SMS after installation. Do not add them to this YAML.

## STEP 5 — Deploy the project

Select **Deploy**. The NAS pulls the image and starts the project. Wait for the download to finish, then find **`unifi-sms-app`** under **Docker → Container**.

Confirm the container is running. Where health status is available, allow about a minute for the first check. Select the container to inspect **Log** if it exits or restarts. [UGREEN's Project guide](https://support.ugnas.com/detail/article/en-US/411) and [Container guide](https://support.ugnas.com/detail/article/en-US/289) describe these screens; labels vary across releases.

Change Compose-managed settings in the project, rather than editing the individual container. This keeps the saved YAML consistent with the deployed service.

## STEP 6 — Open the setup page

On a computer on the same LAN, visit:

```text
http://10.20.30.10:8080
```

Use your actual NAS IP and host port. The protocol for this configuration is `http://`, even if you normally open UGOS Pro through an HTTPS address or a remote-access service.

The application should display **Create administrator**. If it does not open, confirm the container's published port and any firewall or VLAN rules between your computer and the NAS. A remote UGOS desktop connection does not automatically publish this application's port.

## STEP 7 — Set up the administrator, LTE Pro, and email

Continue with [First launch: administrator, LTE Pro, and email forwarding](nas-first-run.md). That guide walks through every application setup screen, including generating an SSH key and approving the device fingerprint.

After setup, confirm that a new SMS appears in the inbox and that the last successful scan updates. The container's HTTP health check verifies the web app and database; it does not establish that the modem is reachable.

## STEP 8 — Back up before relying on the archive

Follow [Back up the complete data volume](nas-maintenance.md#back-up-the-complete-data-volume), and retain a private copy outside the NAS. A copy of the UGOS project folder does not contain the named volume's SMS database.

For later configuration changes, open the project's **⋯ → Settings → Compose configuration**, edit the YAML, and select **Redeploy**. Keep the volume name unchanged during upgrades. The [upgrade checklist](nas-maintenance.md#upgrade-the-image) explains how to pull a new release and verify it.

The pinned `0.1.1` tag does not advance to another version when you check for image updates. Choose a published version explicitly. Do not delete the project or its volumes as an update method.

## Reference and validation

This original tutorial follows the numbered STEP format used by [Marius Hosting](https://mariushosting.com/). It is not affiliated with Marius Hosting, UGREEN, or Ubiquiti.

The Compose configuration, application setup labels, and backup/restore commands were checked for release `0.1.1`. Interface paths were checked against official UGREEN documentation on October 18, 2026. The workflow has not been tested on physical UGREEN hardware; use the equivalent controls in your installed UGOS Pro version.
