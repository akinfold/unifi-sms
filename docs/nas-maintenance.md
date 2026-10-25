# UniFi SMS on a NAS: maintenance and troubleshooting

These procedures apply to the [Synology](install-synology.md) and [UGREEN](install-ugreen.md) guides. They assume container **`unifi-sms-app`** and data volume **`unifi-sms_sms-data`**. Substitute the actual data volume name if you changed it during a restore.

The commands below use GHCR. For Docker Hub, replace `ghcr.io/akinfold/unifi-sms:0.1.6` with `docker.io/akinfold/unifi-sms:0.1.6`. Both registries provide the same release without a login; keep the data volume and other settings unchanged.

## Open a NAS terminal when needed

Routine installation uses the NAS web interface. Backup, restore, and password recovery below use Docker commands over SSH on the **NAS**.

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

The commands below briefly stop the application so SQLite and its companion files are consistent. They read the existing volume without changing it. First confirm the mounted volume:

```sh
sudo docker inspect unifi-sms-app --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'
```

For an installation made with these guides, the result is `unifi-sms_sms-data`. If it differs, replace `source=unifi-sms_sms-data` below with that result before continuing.

In your private backup folder, run:

```sh
umask 077
backup_file="unifi-sms-data-$(date +%Y%m%d-%H%M%S).tar.gz"
sudo docker stop -t 45 unifi-sms-app &&
if sudo docker run --rm --name unifi-sms-backup --user 0:0 \
  --entrypoint tar \
  --mount type=volume,source=unifi-sms_sms-data,target=/data,readonly \
  ghcr.io/akinfold/unifi-sms:0.1.6 \
  -czf - -C /data . > "$backup_file"; then
  sudo docker start unifi-sms-app
  if tar -tzf "$backup_file" >/dev/null; then
    printf 'Backup created: %s\n' "$backup_file"
  else
    printf 'Archive validation failed. Do not use this backup.\n' >&2
  fi
else
  sudo docker start unifi-sms-app
  printf 'Backup failed. Do not use the incomplete archive.\n' >&2
fi
```

Confirm the application is running again. Also save a copy of your customized Compose YAML beside the archive. Download both to a protected location outside the NAS; the archive is sensitive even though device credentials are encrypted. The application secret is part of the same backup.

If the SSH session is interrupted while the application is stopped, reconnect and run `sudo docker start unifi-sms-app`.

Docker's [volume documentation](https://docs.docker.com/engine/storage/volumes/) describes the underlying backup mechanism. Exporting a container image or copying the project folder is not a substitute for this data backup.

## Restore or migrate to another NAS

This restores into a **new volume**, leaving the destination's existing data volume intact. Use only a trusted backup you created yourself.

1. If migrating, make a final backup on the old host and leave its application stopped. Copy the archive and Compose YAML to a private folder on the destination NAS.
2. Deploy the application on the destination using the appropriate installation guide. Do not configure or trust a device in this temporary empty installation. Stop `unifi-sms-app` before restoring. If the destination already has data, back it up first.
3. In the NAS SSH session, change into the folder containing the backup. Set `backup_file` to the actual archive filename. Check it before proceeding:

```sh
backup_file="unifi-sms-data-YYYYMMDD-HHMMSS.tar.gz"
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

5. In the NAS project's Compose editor, change only the volume's explicit `name` to the printed value, for example:

```yaml
volumes:
  sms-data:
    name: unifi-sms-restore-YYYYMMDD-HHMMSS
```

Keep the service mount as `sms-data:/data`. Save and redeploy the project through its NAS interface. Redeployment must recreate the container with the new mount; starting the old stopped container alone does not change its volume.

6. Sign in with the restored application administrator. Check existing messages, device details, and the importer. The restored SSH host key and credentials should still work if the destination NAS can reach the same device. If the device address changed, use **Settings → Device → Change device connection** and complete verification again.

Keep the old volume and backup until you have verified the restored archive. Do not run the old and new importers simultaneously. If you later create another backup, use the **new** mounted volume name in the backup command.

## Upgrade the image

1. Confirm the desired version exists on [GHCR](https://github.com/users/akinfold/packages/container/package/unifi-sms) or [Docker Hub](https://hub.docker.com/r/akinfold/unifi-sms/tags) and read any migration requirements. Back up the complete data volume first.
2. In the NAS project editor, update the tag in your chosen image, `ghcr.io/akinfold/unifi-sms:VERSION` or `docker.io/akinfold/unifi-sms:VERSION`. Replace `VERSION` with the published version you intend to install. Keep the container name, `/data` mount, and the current volume name.
3. On Synology, use **Project → Details → YAML Configurations** and deploy the edited configuration. On UGREEN, use **Project → ⋯ → Settings → Compose configuration → Redeploy**. Ensure the new image is pulled and the container is recreated; a simple restart keeps the old image.
4. Verify the container's image and health, sign in, and check the archive and next device scan.

If you prefer the NAS SSH terminal, change into the folder containing the project's saved Compose file, then run:

```sh
sudo docker compose -p unifi-sms -f docker-compose.yml pull
sudo docker compose -p unifi-sms -f docker-compose.yml up -d
sudo docker compose -p unifi-sms -f docker-compose.yml ps
```

Use the actual filename if it differs. This CLI alternative requires the Compose plugin. If unavailable, use the NAS project interface; do not install a second Docker engine.

The version tag is deliberately pinned. Image update detection will not select a different release for you. Avoid automatic upgrades with `latest` when you need a predictable rollback point. Reverting the image alone may not undo database migrations; restore the matching backup if required by that release.

**Do not remove the data volume during an upgrade.** In particular, avoid Synology's project **Clean** action, deletion options that remove volumes, and `docker compose down -v`.

## Optional HTTPS access

Use HTTPS for browser, REST API, and MCP access over a network. The supplied NAS YAML serves HTTP on a specified LAN interface; it sends passwords, keys, and SMS without transport encryption. Use it only on a private network you control, or reach the loopback binding through an SSH tunnel. Complete administrator setup before granting other clients access.

For an HTTPS reverse proxy running on the NAS host:

1. Choose a hostname such as `sms.example.com`, make it resolve to the NAS for your clients, and obtain a certificate trusted by those clients.
2. Point the proxy's HTTPS frontend to `http://127.0.0.1:8080`. On Synology, reverse-proxy rules are under **Control Panel → Login Portal → Advanced → Reverse Proxy**; see [Synology's Advanced settings](https://kb.synology.com/en-global/DSM/help/DSM/AdminCenter/system_login_portal_advanced). A UGREEN deployment needs an HTTPS proxy you have configured separately; no equivalent built-in proxy is assumed here.
3. Change the application's port mapping and environment to:

```yaml
ports:
  - "127.0.0.1:8080:8000"
environment:
  DATA_DIR: /data
  ALLOWED_HOSTS: "sms.example.com,localhost,127.0.0.1"
  CSRF_TRUSTED_ORIGINS: "https://sms.example.com"
  REQUIRE_HTTPS: "1"
  TRUST_PROXY: "1"
  TRUSTED_PROXY_CIDRS: "REPLACE_WITH_PROXY_PEER_IP/32"
```

4. Replace `REPLACE_WITH_PROXY_PEER_IP/32` with the immediate proxy peer address as seen by the container. Docker may translate a host proxy to a bridge gateway address; do not assume it appears as `127.0.0.1`. Use `/128` for an IPv6 address. List multiple trusted addresses or narrowly scoped networks separated by commas. Do not trust all addresses (`0.0.0.0/0` or `::/0`).
5. Ensure the proxy preserves the requested host, replaces `X-Forwarded-For` with the client address, and sets `X-Forwarded-Proto: https` for HTTPS requests. Save the Compose changes and redeploy. Open `https://sms.example.com` and test sign-in and form submission.

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
| Port already allocated | Choose another host port, such as `8090`, while keeping container port `8000`. |
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

## Validation scope

The supplied Compose YAML was validated with Docker Compose. Backup and restore were exercised locally against a disposable Docker volume using the release image and synthetic data. These checks do not certify NAS firmware behavior; no physical Synology or UGREEN NAS was used for validation.
