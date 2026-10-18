# UniFi SMS on a NAS: first launch

Follow this guide after deploying the [Synology](install-synology.md) or [UGREEN](install-ugreen.md) Compose project. All addresses below are examples. The NAS and LTE Pro have different roles: the browser connects to the NAS; the application connects from the NAS to the LTE Pro.

## STEP 1 — Create the application administrator

Open `http://NAS_IP:8080` using your actual NAS IP. On **Create administrator**, choose a username and a password of at least **8 characters**. Avoid common or entirely numeric passwords. Save the credentials in your password manager.

There is no default account or setup token. Create the account promptly on a trusted LAN: the initial wizard is available until the first administrator is created. Your UniFi SMS account is separate from your NAS account and the device's SSH account.

After account creation, the device connection wizard opens if no device is configured. You can return to it through **Settings → Device**.

## STEP 2 — Choose the LTE Pro address

On **Where is the device?**, either enter its IP address or hostname, or select **Search local network**.

For example, enter `10.20.30.20` if that is the device's actual address. Do not enter the NAS IP, the UniFi controller IP, or a full URL.

For discovery, you can specify **LAN subnet (optional)**, such as `10.20.30.0/24`. Search is limited to subnets with at most 256 addresses. The countdown starts at 1 minute 15 seconds; wait for the result rather than clicking repeatedly.

Docker bridge networking and network isolation can prevent discovery. An empty result does not prove the device is offline. Enter the address from UniFi Network manually if necessary. A discovered name is only a hint; the later authenticated check verifies the model.

Select **Continue**.

## STEP 3 — Obtain the device's SSH account

In UniFi Network, open **UniFi Devices → Device Updates and Settings**, then expand **Device SSH Settings**. Enable **Device SSH Authentication** if necessary and obtain its username. Use the credentials configured for managed devices; your UniFi web login may differ.

Some versions place these settings under **Settings → Devices** or **Settings → System → Advanced → Device Authentication**. Consult [Ubiquiti's current SSH guide](https://help.ui.com/hc/en-us/articles/204909374-Connecting-to-UniFi-with-Debug-Tools-SSH) if your interface differs.

Back in UniFi SMS, enter that username and the SSH port, usually `22`. These are credentials for the **LTE Pro**, not for the NAS. You do not need to enable SSH on the NAS to connect the application to the device.

## STEP 4 — Choose password or SSH-key authentication

Choose the method you intend to use:

| Method | Action |
| --- | --- |
| Password | Enter the device SSH password configured in UniFi Network. |
| Existing key | Choose SSH-key authentication and paste your unencrypted private key into the private-key field. |
| New key | Choose SSH-key authentication, select generation in the application, and click **Generate SSH key**. |

For a generated key, select **Copy public key**. In UniFi Network, locate **SSH Keys** in the device SSH settings, add a key with a descriptive name such as `UniFi SMS`, and paste the complete public-key line. Save or apply the change and allow the device to receive its updated configuration. [Ubiquiti's SSH-key guide](https://help.ui.com/hc/en-us/articles/235247068-Adding-SSH-Keys-to-UniFi-Devices) shows both interface paths.

Copy only the **public** key into the controller. The generated private key stays encrypted in the application's database. You do not need to create an `lte_key` file or mount a private-key file into the container.

Select **Continue** once the credentials or key are ready.

## STEP 5 — Check the connection and approve the device

In version `0.1.1` and later, **Check connection** opens **Verify the SSH fingerprint** without sending credentials. Verify the fingerprint using the trusted connection described below, then select **Confirm fingerprint and check access**. The application authenticates only with the confirmed host key.

If you still use `0.1.0`, upgrade first. That version authenticates immediately when you select **Check connection**.

On **Confirm this device**, review the IP address, model, device name, SSH key type and fingerprint, and phone number if the SIM reports one.

Check the identity independently before selecting **Trust device and start imports**. If you already have verified SSH access from a trusted workstation, this command shows fingerprints for the device's public host keys:

```sh
ssh -p 22 DEVICE_SSH_USER@DEVICE_IP 'for key in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$key" -E sha256; done'
```

Replace the user, address, and port. Run it only over a previously verified connection; accepting an unknown host key in that SSH session does not independently verify the wizard's fingerprint. Match the fingerprint for the key type shown by UniFi SMS. If the device lacks `ssh-keygen` or stores host keys elsewhere, obtain the fingerprint through a trusted administrative console or your device administrator.

After approval, the application stores the host key in `/data/known_hosts` and checks it on subsequent connections. You do not have to supply a `known_hosts` file. If the key later changes, verify the reason before trusting a replacement. A connection check expires after ten minutes; repeat it if needed.

## STEP 6 — Confirm SMS import and device information

Open the inbox. Imports run about once a minute. Send a test SMS to the device's SIM, then refresh after the next scan. Confirm the sender, received date, and message text.

The importer saves each segment before deleting its corresponding slot on the device. Once imported and cleaned up, the message is stored in the NAS archive. Deleting it from the inbox removes the archive copy; keep backups if you need long-term retention.

The inbox also displays the configured device address and model. A missing phone number is normal for SIM cards that do not store their own number. Select **Update from device** to refresh device information; this does not write a new number to the SIM.

If moving from another installation, leave its importer stopped. Only one instance should manage this device and archive at a time.

## STEP 7 — Set up optional email forwarding

Open **Settings → Email forwarding** and enter your provider's values:

| Field | Value |
| --- | --- |
| Host | Your SMTP server hostname. |
| Port and security | Commonly `587` with STARTTLS or `465` with SSL/TLS; follow your provider's instructions. |
| Username and password | SMTP credentials, which can differ from the provider's web-login credentials. |
| Sender | An email address the provider permits you to send from. |
| Recipients | One or more destination email addresses, separated by commas. |

Select **Send test email** and check the inbox and spam folder. The button uses the current form values without saving them and works while forwarding is disabled. A successful result means SMTP acceptance, not guaranteed inbox delivery.

Enable **Enable email forwarding**, then select **Save settings**. Send another SMS to verify forwarding. Existing archived messages are not sent retroactively. A blank password keeps the saved password; use **Replace password** to change it.

## STEP 8 — Protect the archive

Make a [complete backup](nas-maintenance.md#back-up-the-complete-data-volume). Keep the database and persistent secret key together: saved device credentials cannot be recovered from the database alone if that key is lost.

For upgrades, HTTPS, password recovery, or a move to another NAS, continue with [NAS maintenance](nas-maintenance.md).
