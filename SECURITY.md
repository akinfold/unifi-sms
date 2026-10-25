# Security policy

## Reporting a vulnerability

Do not include vulnerabilities, credentials, SMS contents, private network details, or database files in public issues.

Use [GitHub private vulnerability reporting](https://github.com/akinfold/unifi-sms/security/advisories/new) when **Report a vulnerability** is available on the repository's Security tab. If private reporting is unavailable, open an issue titled **Private security contact requested** without technical details. Wait for the maintainer to arrange a private channel before sharing the report.

Include the affected version, reproduction steps using synthetic data, expected and actual behavior, and impact. Do not send live credentials or personal messages.

## Supported versions

Security fixes are made on `main`. Use the latest patched release when available; older image tags are not maintained as separate branches. This is a community project without a guaranteed response time.

Release `0.1.1` verifies the SSH host key before sending credentials and upgrades Paramiko to 5.0.0. Upgrade from `0.1.0`, which authenticates during its initial connection check before the user confirms the host key. If you must use `0.1.0`, verify the device independently before using that check.

Paramiko 5 removes SHA-1 SSH signatures and key exchanges. A device that only offers those legacy algorithms cannot connect to version `0.1.1` or later. Update its firmware or SSH configuration rather than re-enabling SHA-1. See the [Paramiko changelog](https://www.paramiko.org/changelog.html#5.0.0).

## Deployment boundaries

- Keep the first-run administrator page on loopback, an SSH tunnel, or a trusted private network until the administrator exists. The first visitor can create that account.
- Use HTTPS for browser, REST API, and MCP access across a network, or use an SSH tunnel. Enable proxy-header trust only behind a reverse proxy that replaces those headers, and list its immediate peer addresses in `TRUSTED_PROXY_CIDRS`.
- Confirm the device's SSH fingerprint through a trusted channel before sending SSH credentials. A discovery result is not proof of device identity.
- Protect the complete `/data` volume and its backups. They contain messages, credentials, and the persistent application secret. Device credentials, SMTP passwords, and webhook secrets are encrypted with that secret. The secret is stored in the same volume by default, so encryption does not protect against theft of the complete volume. Restrict backup access and encrypt backups separately. Older backups may still contain plaintext SMTP passwords.
- Run one importer per device and keep other SMS deletion tools away from the modem. The archive cannot prevent an unrelated process from changing a slot between modem commands.

See [behavior and limits](docs/behavior.md) for persistence, host-key checks, deletion, and email delivery guarantees.
