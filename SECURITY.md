# Security policy

## Reporting a vulnerability

Do not include vulnerabilities, credentials, SMS contents, private network details, or database files in public issues.

Use [GitHub private vulnerability reporting](https://github.com/akinfold/unifi-sms/security/advisories/new) when **Report a vulnerability** is available on the repository's Security tab. If private reporting is unavailable, open an issue titled **Private security contact requested** without technical details. Wait for the maintainer to arrange a private channel before sharing the report.

Include the affected version, reproduction steps using synthetic data, expected and actual behavior, and impact. Do not send live credentials or personal messages.

## Supported versions

Security fixes are made on `main`. Use the latest patched release when available; older image tags are not maintained as separate branches. This is a community project without a guaranteed response time.

The published `0.1.0` image authenticates during its initial connection check, before the user confirms the SSH host key. Verify the device independently before using that check. The current source fixes this order and upgrades Paramiko to 5.0.0; these changes require a new image release before they reach registry installations.

Paramiko 5 removes SHA-1 SSH signatures and key exchanges. A device that only offers those legacy algorithms cannot connect to the current source build. Update its firmware or SSH configuration rather than re-enabling SHA-1. See the [Paramiko changelog](https://www.paramiko.org/changelog.html#5.0.0).

## Deployment boundaries

- Keep the first-run administrator page on loopback, an SSH tunnel, or a trusted private network until the administrator exists. The first visitor can create that account.
- Use HTTPS for access across an untrusted network. Enable proxy-header trust only behind a reverse proxy that replaces those headers.
- Confirm the device's SSH fingerprint through a trusted channel before sending SSH credentials. A discovery result is not proof of device identity.
- Protect the complete `/data` volume and its backups. They contain messages, credentials, and the persistent application secret. Device credentials are encrypted with that secret; SMTP passwords are stored in SQLite.
- Run one importer per device and keep other SMS deletion tools away from the modem. The archive cannot prevent an unrelated process from changing a slot between modem commands.

See [behavior and limits](docs/behavior.md) for persistence, host-key checks, deletion, and email delivery guarantees.
