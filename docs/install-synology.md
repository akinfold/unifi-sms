# How to install UniFi SMS on a Synology NAS

Archive SMS from a UniFi LTE Pro on your NAS, read them in a browser, and optionally forward new messages by email. This guide uses the published Docker image and Synology Container Manager. No source checkout, Portainer, registry login, or separate database is required.

**Applies to:** DSM 7.2 or later, on a NAS that offers Container Manager and can run Linux AMD64 or ARM64 containers. The image does not support 32-bit ARM. Menu labels can vary by DSM and package version.

**Example throughout:** NAS `10.20.30.10`, browser port `8080`. Replace the example IP with your NAS IP.

## STEP 1 — Check what you need

Have these ready:

- A DSM administrator account and a storage volume with free space.
- The NAS's LAN IP address, preferably kept stable with a DHCP reservation.
- Internet access from the NAS to pull the public image from GitHub Container Registry (GHCR) or Docker Hub.
- Network access from the NAS to the LTE Pro's SSH port, usually TCP 22.
- Access to the UniFi Network application that manages the device, so you can obtain its SSH credentials or install a public key.

Use a trusted LAN for the initial HTTP setup. Do not forward port 8080 from your router to the internet. For access from elsewhere, use a VPN or configure an HTTPS reverse proxy as described in [NAS maintenance](nas-maintenance.md#optional-https-access).

If you are moving an existing installation, use the [migration and restore procedure](nas-maintenance.md#restore-or-migrate-to-another-nas) instead of creating an empty archive. Stop the old importer before activating the new one.

## STEP 2 — Install Container Manager

Open **Package Center**, search for **Container Manager**, and install it. Open the package after installation.

If the package is unavailable, check [Synology's package compatibility information](https://www.synology.com/en-in/dsm/packages/ContainerManager). This procedure requires the package's **Project** feature; instructions for the older DSM Docker package differ.

## STEP 3 — Create the project folder

In **File Station**, create a folder called `unifi-sms` inside a shared folder you use for containers, for example `docker/unifi-sms`. Create the shared folder first if necessary. Select the actual location on your NAS; do not assume its storage volume is named `volume1`.

The project folder stores the Compose configuration. Application data lives in the Docker volume **`unifi-sms_sms-data`**, mounted at `/data`; it is not stored inside the project folder. Docker initializes the volume for the application's UID/GID `10001:10001`. Do not add a bind mount, `PUID`, `PGID`, or a `user:` override for this procedure.

The volume holds SMS, administrator accounts, saved connection settings, trusted host keys, and the persistent application secret. Recreating the container with the same volume keeps that data. Copying the project folder alone does **not** back it up. [Docker explains how named volumes persist](https://docs.docker.com/engine/storage/volumes/).

## STEP 4 — Create the project and paste the Compose configuration

In **Container Manager → Project → Create**, set the project name to **`unifi-sms`**, choose the folder from STEP 3, and select the source option that lets you create a Compose file. Paste the following YAML. Alternatively, upload [compose.nas.yaml](compose.nas.yaml) through the upload option.

```yaml
services:
  app:
    image: ghcr.io/akinfold/unifi-sms:0.1.4
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
| `image` | Use the GHCR image above or the Docker Hub alternative below. Docker selects AMD64 or ARM64 automatically. |
| `sms-data` / `name` | Keep these values for a fresh installation. They identify the persistent data volume. |

The example uses [GHCR](https://github.com/users/akinfold/packages/container/package/unifi-sms). To use [Docker Hub](https://hub.docker.com/r/akinfold/unifi-sms/tags), replace only the `image:` line before deploying:

```yaml
    image: docker.io/akinfold/unifi-sms:0.1.4
```

Both registries provide the same release for Linux AMD64 and ARM64 without a login. Keep the data volume and other settings unchanged when switching registries.

If port 8080 is already in use, change only its host-side value, for example to `10.20.30.10:8090:8000`, and use port 8090 in your browser.

There is no `.env` file to create. Device credentials, SSH keys, host trust, and SMTP settings are entered in UniFi SMS after installation. Do not add them to this YAML.

The click path and project fields follow [Synology's Project documentation](https://kb.synology.com/en-global/DSM/help/ContainerManager/docker_project).

## STEP 5 — Deploy and check the container

Continue through the wizard. A Web Station portal is unnecessary for this direct-port setup. Review the configuration, select the option to start the project after creation, and finish with **Done**.

The first deployment downloads the image and initializes the database. Allow a few minutes for a slow connection. In **Container Manager → Container**, find **`unifi-sms-app`** and check that it is running. If health status is displayed, allow about a minute for the first health check.

If the container stops or keeps restarting, inspect its logs before retrying. Common causes and the exact diagnostic commands are in [Troubleshooting](nas-maintenance.md#troubleshooting).

## STEP 6 — Open UniFi SMS

From a computer on the same LAN, open:

```text
http://10.20.30.10:8080
```

Use your actual NAS IP and chosen host port. Use `http://` for this configuration, even if DSM itself uses HTTPS.

You should see **Create administrator**. If the page is unreachable, confirm the IP and port mapping, then check any NAS or network firewall rules for access from your computer to TCP 8080. Do not disable the firewall as a shortcut.

## STEP 7 — Create the administrator and connect the device

Continue with [First launch: administrator, LTE Pro, and email forwarding](nas-first-run.md). That guide walks through every application setup screen, including generating an SSH key and approving the device fingerprint.

After setup, confirm that a new SMS appears in the inbox and that the last successful scan updates. The container's HTTP health check verifies the web app and database; it does not establish that the modem is reachable.

## STEP 8 — Make the first backup

After confirming the installation works, follow [Back up the complete data volume](nas-maintenance.md#back-up-the-complete-data-volume). Keep a copy off the NAS and protect it as private data.

The service restarts automatically when Docker starts because the Compose file uses `restart: unless-stopped`. A container you explicitly stop stays stopped until you start it again.

For a future upgrade, back up first, edit the image tag in **Project → Details → YAML Configurations**, and redeploy. Use the full [upgrade checklist](nas-maintenance.md#upgrade-the-image). Do not use **Clean**, delete the data volume, or run `docker compose down -v` to update the application.

## Validation

The Compose configuration, application setup labels, and backup/restore commands were checked for release `0.1.1`. Vendor interface paths were checked against official documentation on October 18, 2026. The workflow has not been tested on physical Synology hardware; check labels against your installed DSM version.
