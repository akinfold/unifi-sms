# UniFi SMS on a NAS: maintenance and troubleshooting

These procedures apply to the [Synology](install-synology.md) and [UGREEN](install-ugreen.md) guides. They assume container **`unifi-sms-app`** and default data volume **`unifi-sms_sms-data`**. The backup script finds the actual mounted volume, including after a restore. Substitute its name in other commands if you changed it.

The installation guides use the public `latest` image. Where a command below uses GHCR, you can use Docker Hub by replacing `ghcr.io/akinfold/unifi-sms` with `docker.io/akinfold/unifi-sms`, keeping the tag. Both registries provide the same release without a login; keep the data volume and other settings unchanged.

## Open a NAS terminal when needed

Synology installation, [backup](#synology-back-up-from-dsm-without-nas-ssh), and upgrades can use the NAS web interface. The terminal procedures below are for UGREEN backups, restore, password recovery, and optional diagnostics. SSH here means a session on the **NAS**, separate from the LTE Pro SSH connection configured in the app.

On Synology, enable SSH under **Control Panel → Terminal & SNMP → Terminal**; see [Synology's Terminal documentation](https://kb.synology.com/en-me/DSM/help/DSM/AdminCenter/system_terminal?version=7). On UGREEN, open **Control Panel → Terminal** and enable SSH; see [UGREEN's SSH guide](https://support.ugnas.com/detail/article/en-US/481). Use an administrator account with permission to run `sudo docker`.

From your computer, connect with your NAS username, address, and configured SSH port:

```sh
ssh -p 22 NAS_ADMIN@NAS_IP
```

Run all subsequent shell commands in that NAS session. Work in a private, writable backup folder on a NAS shared folder, using its real filesystem path. Do not use a public share. You can disable NAS SSH again after maintenance.

## Check the service

```sh
sudo docker ps --filter name=unifi-sms-app
sudo docker inspect unifi-sms-app --format '{{.State.Status}} / {{.State.Health.Status}}'
sudo docker logs --tail 100 unifi-sms-app
```

HTTP health covers the web process and SQLite connectivity. Check the application's inbox for modem errors, pending cleanup, and the last successful scan. Review logs before sharing them; they can contain device identifiers or message metadata.

## Back up the complete data volume

Back up all of `/data`, including `sms.sqlite3`, any SQLite companion files, `secret-key`, and `known_hosts`. The secret key is needed to decrypt saved device credentials, SMTP passwords, and webhook secrets. The backup also contains SMS and administrator data. A complete-volume backup includes both encrypted credentials and their decryption secret; older backups can contain plaintext SMTP passwords. Restrict access, encrypt backups separately, and keep an off-NAS copy.

### Synology: back up from DSM without NAS SSH

This procedure works for both Task Scheduler and Compose installations. It briefly stops the application to capture a consistent SQLite database, then restarts it. Run one backup at a time, and do not upgrade, restore, or change the container while it runs.

1. In **File Station**, create a private backup folder on a shared folder that only your NAS administrators can access. Open the folder's **Properties** and copy its full filesystem location, such as `/volume1/private-backups/unifi-sms`. Use that absolute path, not a browser URL or a Windows network path. Restrict shared-folder permissions first; the script makes the archive readable only by its owner, but does not change the shared folder's access rules.
2. Open **Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script**. Name it `Back up UniFi SMS`, choose **User: root**, and clear **Enabled**. On **Schedule**, select a one-time run with no repeat where offered.
3. On **Task Settings**, paste the entire script below into **Run command**. Change only `BACKUP_DIR` to the folder location you copied. Leave the task disabled and run it manually for this backup.

```sh
set -eu
PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 077

BACKUP_DIR="/volume1/private-backups/unifi-sms"

case "$BACKUP_DIR" in
  /*) ;;
  *) echo "BACKUP_DIR must be an absolute NAS filesystem path." >&2; exit 1 ;;
esac
test -d "$BACKUP_DIR" && test -w "$BACKUP_DIR"
command -v docker >/dev/null
docker info >/dev/null
if [ "$(docker inspect unifi-sms-app --format '{{.State.Running}}')" != "true" ]; then
  echo "Start unifi-sms-app before running this backup." >&2
  exit 1
fi
data_volume=$(docker inspect unifi-sms-app --format '{{range .Mounts}}{{if and (eq .Destination "/data") (eq .Type "volume")}}{{.Name}}{{end}}{{end}}')
if [ -z "$data_volume" ]; then
  echo "No named volume mounted at /data. Check the container mounts." >&2
  exit 1
fi
docker volume inspect "$data_volume" >/dev/null
printf 'Data volume: %s\n' "$data_volume"
image_id=$(docker inspect unifi-sms-app --format '{{.Image}}')
docker image inspect "$image_id" >/dev/null

backup_file="$BACKUP_DIR/unifi-sms-data-$(date +%Y%m%d-%H%M%S)-$$.tar.gz"
(set -C; : > "$backup_file")
restart_needed=0
backup_valid=0
cleanup() {
  result=$?
  trap - 0
  trap '' HUP INT TERM
  if [ "$restart_needed" -eq 1 ]; then
    if ! docker start unifi-sms-app >/dev/null; then
      echo "Restart failed. Start unifi-sms-app in Container Manager." >&2
      result=1
    fi
  fi
  if [ "$backup_valid" -ne 1 ]; then
    rm -f "$backup_file"
    echo "Backup failed; the incomplete archive was removed." >&2
  fi
  exit "$result"
}
trap cleanup 0
trap 'exit 1' HUP INT TERM
chmod 600 "$backup_file"

restart_needed=1
docker stop -t 45 unifi-sms-app >/dev/null
docker run --rm --pull=never --name "unifi-sms-backup-$$" --user 0:0 \
  --entrypoint tar \
  --mount "type=volume,source=$data_volume,target=/data,readonly" \
  "$image_id" -czf - -C /data . > "$backup_file"
docker start unifi-sms-app >/dev/null
restart_needed=0
docker run --rm -i --pull=never --name "unifi-sms-backup-verify-$$" --user 0:0 \
  --entrypoint tar "$image_id" -tzf - < "$backup_file" >/dev/null
backup_valid=1
printf 'Backup created and archive validated: %s\n' "$backup_file"
exit 0
```

The script uses the app's **already installed image ID** for both archive creation and validation. It does not download an image, including while the service is stopped. It resolves the current `/data` named volume rather than assuming the default name. A bind mount is not supported by this script; it stops before stopping the app if no named volume is found.

4. Save the task, accept DSM's root-task warning, then select the task and **Run**. Check the task result/output for **Backup created and archive validated**. The root-owned archive has permissions `600` and its name includes the timestamp and task process ID. Use an authorized DSM administrator to download it from the private folder. If File Station denies access, use the archive's **Properties → Permission** to grant read access only to the administrator who will save the off-NAS copy. Keep other users denied; do not make the folder public.
5. In **Container Manager → Container**, confirm `unifi-sms-app` is running and becomes healthy. Open the app and check the existing archive and importer. Also save your customized Compose YAML or installation-task script with the backup and keep a protected off-NAS copy of both.

The cleanup handler attempts to restart the application after an error or a normal termination signal and removes an unvalidated archive. It cannot recover from a NAS power loss or a forced kill. If the task is interrupted or reports a restart failure, check the container and start it in Container Manager before leaving maintenance. Archive validation checks that the compressed tar can be read; a [restore into a new volume](#restore-or-migrate-to-another-nas) is the fuller recovery check.

### UGREEN or another NAS: back up over SSH

Open a [NAS SSH session](#open-a-nas-terminal-when-needed). Create a private backup folder using the NAS file manager and note its absolute filesystem path. Start a root shell:

```sh
sudo -i
```

Paste the **same complete backup script above**, with `BACKUP_DIR` set to that folder. It runs on the NAS and exits the root shell when finished. The original data volume remains attached throughout. Check that the container is running again, save the Compose YAML or installation-task script, and copy the validated archive and configuration to a protected location off the NAS.

To inspect the mounted volume separately, use:

```sh
sudo docker inspect unifi-sms-app --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'
```

If the SSH connection is lost during backup, reconnect and check `unifi-sms-app`. If it is stopped, run `sudo docker start unifi-sms-app`. Confirm the backup task has finished and the archive is valid before relying on that file.

Docker's [volume documentation](https://docs.docker.com/engine/storage/volumes/) describes the underlying backup mechanism. Exporting a container image or copying the project folder is not a substitute for this data backup.

## Restore or migrate to another NAS

This restores into a **new volume**, leaving the destination's existing data volume intact. Use only a trusted backup you created yourself.

1. If migrating, make a final backup on the old host and leave its application stopped. Copy the archive and Compose YAML or Task Scheduler installation script to a private folder on the destination NAS.
2. Deploy the application on the destination using the appropriate installation guide. Do not configure or trust a device in this temporary empty installation. Stop `unifi-sms-app` before restoring. If the destination already has data, back it up first.
3. In the NAS SSH session, change into the folder containing the backup. Set `backup_file` to the actual archive filename. Check it before proceeding:

```sh
backup_file="unifi-sms-data-YYYYMMDD-HHMMSS-PID.tar.gz"
tar -tzf "$backup_file"
sudo docker stop -t 45 unifi-sms-app
```

4. Create a new, empty volume and extract the backup. The empty-volume check prevents overwriting an existing volume if the generated name is reused:

```sh
restore_volume="unifi-sms-restore-$(date +%Y%m%d-%H%M%S)"
sudo docker volume create "$restore_volume"
sudo docker run --rm -i --name unifi-sms-restore --user 0:0 \
  --entrypoint sh \
  --mount "type=volume,source=$restore_volume,target=/data" \
  ghcr.io/akinfold/unifi-sms:0.1.6 \
  -c 'test -z "$(ls -A /data)" || { echo "Restore volume is not empty" >&2; exit 1; }; tar -xzpf - -C /data && chown -R 10001:10001 /data && chmod 700 /data' \
  < "$backup_file" &&
printf 'Restored volume: %s\n' "$restore_volume"
```

Proceed only if extraction succeeds. An error means the new volume may be incomplete; keep the application stopped until you resolve it. Do not point it at a failed restore.

5. If you installed with Compose, change only the volume's explicit `name` in the NAS project's editor to the printed value, for example:

```yaml
volumes:
  sms-data:
    name: unifi-sms-restore-YYYYMMDD-HHMMSS
```

Keep the service mount as `sms-data:/data`. Save and redeploy the project through its NAS interface. Redeployment must recreate the container with the new mount; starting the old stopped container alone does not change its volume.

For a Synology Task Scheduler installation, edit the saved installation task instead. Replace `source=unifi-sms_sms-data` in `--mount` with the printed restored volume name. With the application stopped, delete **only** the `unifi-sms-app` container in Container Manager, keeping all volumes, then run the edited task. The new container uses the restored volume. Keep the NAS IP and port from your current installation.

6. Sign in with the restored application administrator. Check existing messages, device details, and the importer. The restored SSH host key and credentials should still work if the destination NAS can reach the same device. If the device address changed, use **Settings → Device → Change device connection** and complete verification again.

Keep the old volume and backup until you have verified the restored archive. Do not run the old and new importers simultaneously. The backup script detects the new mounted volume automatically.

## Upgrade the image

Choose the procedure for the method you used to install the application. Both preserve data only when the existing `/data` volume remains attached.

### Synology Task Scheduler installation

1. [Back up the complete data volume](#back-up-the-complete-data-volume). Note the **Data volume** value in the backup output, or use the inspection command in that section.
2. Open the saved **Install UniFi SMS** task. Keep `IMAGE="docker.io/akinfold/unifi-sms:latest"` (or the GHCR equivalent). If you used a fixed version in an older installation, change its tag to `latest`. Keep the NAS IP, current host port (default **8098**), and existing volume name unchanged.
3. Before removing the current container, download the current `latest` image. In the NAS SSH session, run `sudo docker pull docker.io/akinfold/unifi-sms:latest`. Alternatively, run `docker pull docker.io/akinfold/unifi-sms:latest` as a separate one-time root task in Task Scheduler. Use the same registry as your installation task. Proceed only after the download succeeds.
4. In **Container Manager → Container**, stop `unifi-sms-app` and delete **only the container**, keeping all data volumes. Do not delete `unifi-sms_sms-data`. If you restored to another volume, preserve that volume instead.
5. Run the edited installation task once. Open `http://NAS_IP:8098` (or your configured host port), sign in with the existing administrator, and check the archive and importer. If a new administrator wizard appears, stop and check the `/data` mount before continuing.

Do not use `docker compose up` to manage a container created by the Task Scheduler `docker run` command. It lacks the Compose project labels. Keep using the task, or deliberately recreate the container as a project with the same data volume.

### Synology or UGREEN Compose project

1. Read the [latest release notes](https://github.com/akinfold/unifi-sms/releases/latest), including any migration requirements. Back up the complete data volume first.
2. In the NAS project editor, keep the image as `ghcr.io/akinfold/unifi-sms:latest` or `docker.io/akinfold/unifi-sms:latest`, with `pull_policy: always`. If you used a fixed version in an older installation, change its tag to `latest`. Keep the current host port, container name, `/data` mount, and volume name. Older UGREEN installations may use host port **8080**; an image upgrade does not require changing it.
3. On Synology, use **Project → Details → YAML Configurations** and deploy the edited configuration. On UGREEN, use **Project → ⋯ → Settings → Compose configuration → Redeploy**. Ensure the new image is pulled and the container is recreated; a simple restart keeps the old image.
4. Verify the container's image and health, sign in, and check the archive and next device scan.

If you prefer the NAS SSH terminal, change into the folder containing the project's saved Compose file, then run:

```sh
sudo docker compose -p unifi-sms -f docker-compose.yml pull
sudo docker compose -p unifi-sms -f docker-compose.yml up -d
sudo docker compose -p unifi-sms -f docker-compose.yml ps
```

Use the actual filename if it differs. This CLI alternative requires the Compose plugin. If unavailable, use the NAS project interface; do not install a second Docker engine.

`latest` tracks the latest stable release. Each installation or redeployment checks the registry; a running container does not update in the background. Reverting the image alone may not undo database migrations; restore the matching backup if required by that release.

**Do not remove the data volume during an upgrade.** In particular, avoid Synology's project **Clean** action, deletion options that remove volumes, and `docker compose down -v`.

## Optional HTTPS access

Use HTTPS for browser, REST API, and MCP access over a network. The supplied NAS YAML serves HTTP on a specified LAN interface; it sends passwords, keys, and SMS without transport encryption. Use it only on a private network you control, or reach the loopback binding through an SSH tunnel. Complete administrator setup before granting other clients access.

For an HTTPS reverse proxy running on the NAS host:

1. Choose a hostname such as `sms.example.com`, make it resolve to the NAS for your clients, and obtain a certificate trusted by those clients.
2. Point the proxy's HTTPS frontend to `http://127.0.0.1:8098` for either NAS, or use the host port you configured. On Synology, reverse-proxy rules are under **Control Panel → Login Portal → Advanced → Reverse Proxy**; see [Synology's Advanced settings](https://kb.synology.com/en-global/DSM/help/DSM/AdminCenter/system_login_portal_advanced). A UGREEN deployment needs an HTTPS proxy you have configured separately; no equivalent built-in proxy is assumed here.
3. For a Compose installation, change the application's port mapping and environment as shown below. The example uses host port **8098**; substitute your configured port if it differs. Keep container port **8000**. For a Task Scheduler installation, recreate the container with the equivalent `--publish` and `--env` options after backing up its data; keep the same `/data` volume.

```yaml
ports:
  - "127.0.0.1:8098:8000"
environment:
  DATA_DIR: /data
  ALLOWED_HOSTS: "sms.example.com,localhost,127.0.0.1"
  CSRF_TRUSTED_ORIGINS: "https://sms.example.com"
  REQUIRE_HTTPS: "1"
  TRUST_PROXY: "1"
  TRUSTED_PROXY_CIDRS: "REPLACE_WITH_PROXY_PEER_IP/32"
```

4. Replace `REPLACE_WITH_PROXY_PEER_IP/32` with the immediate proxy peer address as seen by the container. Docker may translate a host proxy to a bridge gateway address; do not assume it appears as `127.0.0.1`. Use `/128` for an IPv6 address. List multiple trusted addresses or narrowly scoped networks separated by commas. Do not trust all addresses (`0.0.0.0/0` or `::/0`).
5. Ensure the proxy preserves the requested host, replaces `X-Forwarded-For` with the client address, and sets `X-Forwarded-Proto: https` for HTTPS requests. Apply the changes by redeploying the Compose project or recreating the Task Scheduler container with its existing data volume. Open `https://sms.example.com` and test sign-in and form submission.

This example assumes the proxy runs on the NAS host. A proxy in another container cannot reach the NAS through its own `127.0.0.1`; configure an appropriate private Docker network and restrict direct backend access before enabling proxy trust. Forwarded headers are ignored unless the immediate peer is trusted; `TRUST_PROXY=1` alone does not trust a proxy. A wrong peer address can cause HTTPS redirect loops. Restrict backend access to the proxy and test two different clients so their login limits remain separate. Enabling HTTPS in DSM or UGOS alone does not enable it for UniFi SMS. Router port forwarding is not needed for LAN or VPN access.

## Recover an administrator password

With the container running:

```sh
sudo docker exec -it unifi-sms-app python manage.py changepassword ADMIN_USERNAME
```

Replace `ADMIN_USERNAME` with the existing UniFi SMS username and enter the new password when prompted. Do not delete the database or application secret to reopen first-run setup.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No matching image / unsupported platform | The NAS must support Linux AMD64 or ARM64 and offer a Docker package. No 32-bit ARM image is published. |
| Image download fails | Check NAS DNS, internet access, and outbound HTTPS to the selected registry. Try the other registry by changing only `image:`. GHCR and Docker Hub images are public; no registry credentials are needed. |
| Port already allocated / address already in use | Follow the port-change steps for [Synology](install-synology.md#if-the-port-is-already-in-use), [UGREEN](install-ugreen.md#if-the-port-is-already-in-use), or [Docker Compose](install-docker.md#choose-another-host-port). Recreate the container with the same data volume and use the new host port in the browser URL; keep container port `8000`. |
| Cannot assign requested address | Replace the example IP in the port mapping with an address actually assigned to the NAS. Update it after an address change. |
| Browser cannot connect | Confirm the container is running, the NAS IP and host port are correct, and firewall/routing rules permit the connection. |
| HTTP 400 / DisallowedHost | Add the exact browser hostname or IP to `ALLOWED_HOSTS`, without scheme or port, then redeploy. |
| HTTP 403 / CSRF error | Reload and sign in again. For HTTPS, verify the origin, forwarded protocol header, and proxy settings match the URL you use. |
| Container cannot write `/data` | Use the named volume and no `user:` override. For restored data, verify ownership is `10001:10001`. Do not solve this with permissions `777`. |
| Setup asks for a new administrator after an upgrade | Stop and inspect the `/data` mount. A different or missing volume can make the application look empty. Reattach the original volume. |
| No discovery results | Enter the LTE Pro IP manually. Check device SSH access and NAS-to-device routing. A bridged container may not receive LAN broadcasts. |
| SSH authentication fails | Use the managed device's SSH account, not the NAS or UniFi web account. For keys, confirm the public key was applied to the device. |
| Host key changed | Verify the new fingerprint independently, then use the device wizard to approve the verified replacement. Do not disable host-key checking. |
| Device has no phone number | Some SIM cards do not report one. This does not prevent SMS import. |
| Web container is healthy but no messages arrive | Check importer status in the inbox, the device SSH connection, and whether the SIM actually received a new SMS. |
| SMTP test fails | Verify provider host, port, TLS mode, SMTP credentials, authorized sender, and outbound network access. The test does not save edited settings. |
