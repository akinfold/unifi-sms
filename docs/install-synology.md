# Install UniFi SMS on a Synology NAS

Install the container with one script in DSM **Task Scheduler**, then finish setup in your browser. You do not need to enable NAS SSH, create data folders, or edit an `.env` file.

**Web UI: `http://NAS_IP:8098`** — replace `NAS_IP` with your Synology's LAN IP. This guide uses port **8098** by default in the installation command and the [ready-to-use `docker-compose.yml`](synology/docker-compose.yml). If it is busy, [choose another host port](#if-the-port-is-already-in-use) and use it in the URL.

## Before you start

- Use DSM 7.2 or later on a model that offers **Container Manager** in Package Center. The image supports Linux AMD64 and ARM64; 32-bit ARM is not supported.
- Sign in to DSM as an administrator. Find the NAS LAN IP under **Control Panel → Network → Network Interface** and keep it stable, for example with a DHCP reservation.
- Have internet access from the NAS, the LTE Pro's IP address, and its device SSH credentials ready. Find the device address in UniFi Network; your UniFi web login may differ from its SSH account. The NAS must be able to reach the device, usually on TCP port 22.
- Use this HTTP setup on a trusted private LAN. Create the application administrator before sharing access, and do not forward the app's host port from the internet. For remote access, use a VPN or [configure HTTPS](nas-maintenance.md#optional-https-access).

These steps are for a fresh installation. If UniFi SMS is already installed, follow [Upgrade the image](nas-maintenance.md#upgrade-the-image) to keep its data and configuration.

## Step 1. Install Container Manager

Open **Package Center**, search for **Container Manager**, and select **Install**. If it is already installed, open it and confirm it is running.

## Step 2. Create an installation task

Open **Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script**.

On the **General** tab:

- **Task:** `Install UniFi SMS`
- **User:** `root`
- **Enabled:** clear the checkbox. You will run this task manually once.

On the **Schedule** tab, select **Run on the following date** and **Do not repeat** where those options are available. DSM labels can vary by version.

## Step 3. Paste the installation command

Open **Task Settings** and paste the entire block below into **Run command**.

Change `NAS_IP="10.20.30.10"` to your NAS LAN IP. For example, if you open DSM at `http://10.20.30.10:5000`, the IP is `10.20.30.10` — do not include `http://` or `:5000`. Leave `HOST_PORT="8098"` unless that port is busy; then choose a free port, for example `HOST_PORT="18098"`.

```sh
set -eu
PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH

NAS_IP="10.20.30.10"
HOST_PORT="8098"
IMAGE="docker.io/akinfold/unifi-sms:latest"

command -v docker >/dev/null
docker info >/dev/null
if docker container inspect unifi-sms-app >/dev/null 2>&1; then
  echo "unifi-sms-app already exists. Follow the upgrade instructions instead." >&2
  exit 1
fi

docker pull "$IMAGE"
docker run -d \
  --name unifi-sms-app \
  --restart unless-stopped \
  --stop-timeout 45 \
  --publish "${NAS_IP}:${HOST_PORT}:8000" \
  --env DATA_DIR=/data \
  --env "ALLOWED_HOSTS=$NAS_IP,localhost,127.0.0.1" \
  --mount type=volume,source=unifi-sms_sms-data,target=/data \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  "$IMAGE"

printf 'After startup, open http://%s:%s\n' "$NAS_IP" "$HOST_PORT"
```

The command pulls **`latest`** from Docker Hub before creating the data volume and starting `unifi-sms-app`. It checks for an updated image even if `latest` is already cached. If the download fails, installation stops. No registry login is required. To use GHCR instead, change `IMAGE` to `ghcr.io/akinfold/unifi-sms:latest`.

The task refuses to replace an existing container. It does not delete an existing data volume. Your chosen host port is published only on the NAS IP you entered; container port **8000** stays unchanged.

## Step 4. Run the task

1. Select **OK** to save. Accept DSM's warning about running a task as root and enter your DSM administrator password if prompted.
2. Select **Install UniFi SMS** in the task list, click **Run**, and confirm.
3. Allow time for the image download and startup. Open **Container Manager → Container** and find **unifi-sms-app**. It should be running; the health status changes from starting to healthy after initialization.

Leave the task disabled after installation. Docker's restart policy starts the container after a NAS restart unless you explicitly stopped it; the installation task does not need to run again.

## Step 5. Open the app and finish setup

Open **`http://NAS_IP:8098`** from a computer on the same LAN, using your NAS IP and chosen host port. For example, with `HOST_PORT="18098"`, open `http://10.20.30.10:18098`. Use HTTP for this initial setup; DSM's HTTPS port does not apply to the container.

1. On **Create administrator**, choose an application username and a password of at least **8 characters**. Common or entirely numeric passwords are rejected. There is no default account or setup token.
2. In the device wizard, enter the LTE Pro's IP address. **Search local network** is optional; Docker networking can limit discovery. Use the **device's** SSH username and password, or generate an SSH key and follow the instructions to add its public key to UniFi Network.
3. Verify the SSH fingerprint through a trusted console or administrator, then check access. Starting imports saves SMS to the archive and deletes their device copies to free slots. Select **Trust device and start imports** when ready.
4. Send a test SMS to the SIM and wait for the next scan; imports run about once a minute. Open the message and look for **Saved in your archive. Device slot cleared.** If cleanup is pending, check the importer status and the next scan.

Follow [First launch](nas-first-run.md) if you need help finding SSH credentials or verifying the fingerprint. The application account, NAS account, and device SSH account are separate.

Optional: [forward SMS to email](../README.md#forward-new-sms-to-email) or [connect REST API, MCP, or webhooks](integrations.md). All are configured in the browser and disabled by default.

## Alternative: install with docker-compose.yml

Use this method **instead of** the Task Scheduler command if you prefer a Container Manager project. Choose one method; a project cannot take over a container created by `docker run` without recreating it.

Open the ready-to-use [**docker-compose.yml**](synology/docker-compose.yml), or copy the configuration below. Replace both instances of `10.20.30.10` with your NAS LAN IP. It uses the same `latest` image, port **8098**, container name, and data volume as the task above. `pull_policy: always` checks the registry on each deployment. No `.env` file is needed.

If **8098** is busy, change only the middle number under `ports`, for example to `10.20.30.10:18098:8000`. Keep your NAS IP and container port **8000**. Open `http://NAS_IP:18098` after deployment. See [port-conflict recovery](#if-the-port-is-already-in-use) if the project already failed to start.

<details>
<summary>Show docker-compose.yml</summary>

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

</details>

1. In **File Station**, create `docker/unifi-sms` inside your Docker shared folder.
2. Open **Container Manager → Project → Create**. Set the project name to `unifi-sms` and select that folder.
3. Select **Create docker-compose.yml** and paste the edited YAML, or upload the edited file if DSM offers that option.
4. Continue without configuring a Web Station portal. Select **Start the project after creation** and finish with **Done**.
5. When `unifi-sms-app` is running, open **`http://NAS_IP:8098`**, substituting your NAS IP and chosen host port, and complete [Step 5](#step-5-open-the-app-and-finish-setup).

## If the port is already in use

For **Port is already allocated** or **Address already in use**, choose a free host port. The steps below use **18098** as an example; if it is also busy, choose another port.

For a **Task Scheduler** installation:

1. Edit the saved **Install UniFi SMS** task and set `HOST_PORT="18098"` in its script. With an older script that has no `HOST_PORT`, change the host port in `--publish` from `8098` to `18098`; keep the final `:8000`.
2. If the failed installation left an `unifi-sms-app` container, delete **only that failed container** in **Container Manager → Container**, keeping its data volume. Otherwise, the task's existing-container check blocks the retry. If the container belongs to a working installation, follow the [backup and recreation steps](nas-maintenance.md#synology-task-scheduler-installation) instead, using your new host port.
3. Save the task and run it again.

For a **Container Manager project**:

1. Open **Container Manager → Project → unifi-sms → YAML Configurations** and change the mapping under `ports` to `"10.20.30.10:18098:8000"`, using your NAS IP.
2. Save the YAML and follow the prompts to deploy the project with the new settings. Docker must recreate the container; restarting it alone does not apply a new port mapping. Keep the same project and data volume, and do not use **Clean**. See [Synology's Project guide](https://kb.synology.com/en-global/DSM/help/ContainerManager/docker_project?version=7).

After the container starts, open **`http://NAS_IP:18098`**. If the DSM firewall is enabled, allow the chosen TCP port from your trusted LAN clients. Update any bookmarks, direct REST or MCP URLs, and reverse-proxy backend settings that still use the old port.

## Where your data lives

Both methods store SMS, the administrator account, settings, and encryption keys in the Docker volume **`unifi-sms_sms-data`**, mounted at `/data`. Docker manages this volume; no File Station data folder or manual permission changes are needed. The Compose project folder alone is not a backup.

Follow [Back up the complete data volume](nas-maintenance.md#back-up-the-complete-data-volume) before updates or migration. Keep the volume when recreating the container. Do not use **Clean**, delete the data volume, or run `docker compose down -v`. Run only one importer per LTE Pro.

For updates, use the [instructions for your installation method](nas-maintenance.md#upgrade-the-image). For migration, follow [Restore or migrate to another NAS](nas-maintenance.md#restore-or-migrate-to-another-nas).

## If something goes wrong

| What you see | What to check |
| --- | --- |
| `docker: command not found` | Install and start Container Manager, then run the task again. Keep the `PATH` lines in the script. |
| Cannot connect to the Docker daemon | Confirm Container Manager is running and the task's **User** is `root`. |
| `unifi-sms-app already exists` | Open the existing app or use the update instructions. If a failed port-conflict installation left the container, follow [port-conflict recovery](#if-the-port-is-already-in-use). |
| Image download fails | Check NAS internet access and DNS. Try the GHCR image in the `IMAGE` setting. |
| Port is already allocated / address already in use | Follow [port-conflict recovery](#if-the-port-is-already-in-use) for your installation method. |
| Cannot assign requested address | Set `NAS_IP` to an address actually assigned to the NAS. In Compose, replace the example IP in both places. |
| The page does not open | Confirm `unifi-sms-app` is running and use `http://NAS_IP:HOST_PORT` with your NAS IP and chosen host port (default **8098**). If the DSM firewall is enabled, allow that TCP port from your trusted LAN clients. |
| HTTP 400 / DisallowedHost | Open the exact NAS IP configured in the task or YAML. A different hostname must also be listed in `ALLOWED_HOSTS` before recreating the container. |
| The device search is empty | Enter the LTE Pro's IP manually. Docker networking can prevent broadcast discovery. |

If an installation fails with a port or address error, Docker may leave a container that never started. Correct the IP or host port in the task, delete **only that failed `unifi-sms-app` container** in Container Manager while keeping its data volume, then run the task again. The existing-container check otherwise blocks the retry.

For container errors, open **Container Manager → Container → unifi-sms-app → Details → Log**. For task errors, enable **Task Scheduler → Settings → Save output results**, choose a private shared folder, rerun the task, and open **Action → View Result**. Do not post logs containing credentials or message data.
