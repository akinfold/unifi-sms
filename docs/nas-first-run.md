# UniFi SMS on a NAS: first launch

Finish setup after installing the container with the [Synology](install-synology.md) or [UGREEN](install-ugreen.md) guide. Read carrier notifications and other SMS sent to your LTE Pro's SIM in a searchable inbox on your NAS. You can also forward new SMS to email or webhooks, connect scripts through REST, or give an MCP client access to the archive. These integrations are optional.

The browser connects to the NAS; the application connects from the NAS to the LTE Pro. All addresses below are examples.

## Step 1. Create the application administrator

Open the address from your installation guide: **`http://NAS_IP:8098` for both Synology and UGREEN**. Use your actual NAS IP and the host port you configured if you changed it. On **Create administrator**, choose a username and a password of at least **8 characters**. Avoid common or entirely numeric passwords. Save the credentials in your password manager.

There is no default account or setup token. Create the account promptly on a trusted LAN: the initial wizard is available until the first administrator is created. Your UniFi SMS account is separate from your NAS account and the device's SSH account.

After account creation, the device connection wizard opens if no device is configured. You can return to it through **Settings → Device**.

## Step 2. Enter the LTE Pro address

In UniFi Network, open **UniFi Devices**, select the LTE Pro, and find its IP address. On **Where is the device?** in UniFi SMS, enter that address. Manual entry is the shortest path and works when Docker networking prevents discovery.

For example, enter `10.20.30.20` if that is the device's actual address. Do not enter the NAS IP, the UniFi controller IP, or a full URL.

If you do not know the address, choose **Search local network** instead. You can specify **LAN subnet (optional)**, such as `10.20.30.0/24`. Search is limited to subnets with at most 256 addresses. The countdown starts at 1 minute 15 seconds; wait for the result rather than clicking repeatedly.

Docker bridge networking and network isolation can prevent discovery. An empty result does not prove the device is offline. Enter the address from UniFi Network manually if necessary. A discovered name is only a hint; the later authenticated check verifies the model.

Select **Continue**.

<a id="step-3--obtain-the-devices-ssh-account"></a>

## Step 3. Obtain the device's SSH account

In UniFi Network, open **UniFi Devices → Device Updates and Settings**, then expand **Device SSH Settings**. Enable **Device SSH Authentication** if necessary and obtain its username. Use the credentials configured for managed devices; your UniFi web login may differ.

Some versions place these settings under **Settings → Devices** or **Settings → System → Advanced → Device Authentication**. Consult [Ubiquiti's current SSH guide](https://help.ui.com/hc/en-us/articles/204909374-Connecting-to-UniFi-with-Debug-Tools-SSH) if your interface differs.

Back in UniFi SMS, enter that username and the SSH port, usually `22`. These are credentials for the **LTE Pro**, not for the NAS. You do not need to enable SSH on the NAS to connect the application to the device.

## Step 4. Choose password or SSH-key authentication

Choose the method you intend to use:

| Method | Action |
| --- | --- |
| Password | Enter the device SSH password configured in UniFi Network. |
| Existing key | Choose SSH-key authentication and paste your unencrypted private key into the private-key field. |
| New key | Choose SSH-key authentication, select generation in the application, and click **Generate SSH key**. |

For a generated key, select **Copy public key**. In UniFi Network, locate **SSH Keys** in the device SSH settings, add a key with a descriptive name such as `UniFi SMS`, and paste the complete public-key line. Save or apply the change and allow the device to receive its updated configuration. [Ubiquiti's SSH-key guide](https://help.ui.com/hc/en-us/articles/235247068-Adding-SSH-Keys-to-UniFi-Devices) shows both interface paths.

Copy only the **public** key into the controller. The generated private key stays encrypted in the application's database. You do not need to create an `lte_key` file or mount a private-key file into the container.

Select **Continue** once the credentials or key are ready.

## Step 5. Verify and approve the device

Select **Check connection** to open **Verify the SSH fingerprint** without sending credentials. Verify the fingerprint using the trusted connection described below, then select **Confirm fingerprint and check access**. The application authenticates only with the confirmed host key.

On **Confirm this device**, review the IP address, model, device name, SSH key type and fingerprint, and phone number if the SIM reports one.

Use an independent trusted connection to compare the fingerprint. If your LTE Pro and UniFi Network version offer a Debug Console:

1. Open UniFi Network through your usual trusted address and sign in.
2. Open **UniFi Devices**, select the **LTE Pro itself**, then open its **Settings**, scroll to the bottom, and select **Debug**. This is the route in [Ubiquiti's Debug Console guide](https://help.ui.com/hc/en-us/articles/204909374-Connecting-to-UniFi-with-Debug-Tools-SSH).
3. In that device console, run the following read-only command if the device provides `ssh-keygen` and OpenSSH public host-key files:

```sh
for key in /etc/ssh/ssh_host_*_key.pub; do
  [ -f "$key" ] || continue
  ssh-keygen -lf "$key" -E sha256
done
```

Compare the `SHA256:...` value for the key type displayed in UniFi SMS. Debug Console availability, host-key locations, and `ssh-keygen -E sha256` support depend on the device and software version. If **Debug** is absent, the command fails, or it prints no matching key, ask your device administrator to obtain that fingerprint through a trusted console. Do not run it on the NAS or controller: those have different host keys.

If you already have independently verified SSH access from a trusted workstation, run the same command in that LTE Pro SSH session instead. Accepting an unknown key in a new SSH session does not verify the wizard's fingerprint. Continue only when the expected fingerprint matches.

Before selecting **Trust device and start imports**, understand that imports start automatically after approval: each SMS segment is saved to the NAS archive before its device slot is deleted. This frees space on the device, so the NAS archive and its backups become the retained copy. Keep any other importer for this device stopped. Select the button once you have verified the identity and accepted this behavior.

After approval, the application stores the host key in `/data/known_hosts` and checks it on subsequent connections. You do not have to supply a `known_hosts` file. If the key later changes, verify the reason before trusting a replacement. A connection check expires after ten minutes; repeat it if needed.

## Step 6. Import a test SMS

Open the inbox. Imports run about once a minute. Send a test SMS to the device's SIM, then wait for the next scan and open the message. Confirm the sender, received date, text, and **Saved in your archive. Device slot cleared.** status. If it says **Device cleanup is pending**, the segment is saved but its device slot has not yet been cleared; check importer status if that persists.

The archived message remains available after its device slot is cleared. Deleting it from the inbox removes the archive copy; keep backups if you need long-term retention.

The inbox also displays the configured device address and model. A missing phone number is normal for SIM cards that do not store their own number. Select **Update from device** to refresh device information; this does not write a new number to the SIM.

If moving from another installation, leave its importer stopped. Only one instance should manage this device and archive at a time.

## Step 7. Set up optional forwarding and integrations

Open **Settings → Email forwarding** and enter your provider's values:

| Field | Value |
| --- | --- |
| Host | Your SMTP server hostname. |
| Port and security | Commonly `587` with STARTTLS or `465` with SSL/TLS; follow your provider's instructions. |
| Username and password | SMTP credentials, which can differ from the provider's web-login credentials. |
| Sender | An email address the provider permits you to send from. |
| Recipients | One or more destination email addresses, separated by commas. |

Select **Send test email** and check the inbox and spam folder. The button uses the current form values without saving them and works while forwarding is disabled. A successful result means SMTP acceptance, not guaranteed inbox delivery.

Enable **Enable email forwarding**, then select **Save settings**. Send a **new SMS after saving** to verify forwarding. Existing archive messages and records created through REST or MCP are not forwarded. A blank password keeps the saved password; use **Replace password** to change it.

You can also choose any of these independent options, disabled by default:

- [Use the REST API](integrations.md#use-the-rest-api) to search, read, create, update, or delete archive records from scripts.
- [Connect an MCP client](integrations.md#connect-an-mcp-client) to work with the archive through supported tools.
- [Forward new SMS to a webhook](integrations.md#forward-new-sms-to-a-webhook) to send JSON to your automation. Enable and save it first, then send a new SMS; old archive records and API-created records do not test forwarding.

Creating a record through REST or MCP adds it to the archive; it does not send a cellular SMS.

## Step 8. Protect the archive

Make a [complete backup](nas-maintenance.md#back-up-the-complete-data-volume). Keep the database and persistent secret key together: saved device credentials cannot be recovered from the database alone if that key is lost.

For upgrades, HTTPS, password recovery, or a move to another NAS, continue with [NAS maintenance](nas-maintenance.md).
