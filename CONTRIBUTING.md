# Contributing

Read the [development guide](docs/development.md) for source builds, local setup, and validation commands. Keep changes focused and explain the observable behavior in the pull request. Write documentation, code comments, and GitHub discussions in English.

## Tests and sample data

Use synthetic SMS, credentials, hostnames, addresses, and device identities. Never include real messages, phone numbers, network inventories, screenshots of personal deployments, or account details in commits or issues. Do not commit runtime files, database backups, private keys, or local Compose overrides.

Run the Python suite, migration check, JavaScript tests, and lint before submitting code. Add regression coverage when changing import, deletion, device trust, or delivery behavior. Tests must not require a real device, a mail account, or deployment credentials.

## Import and trust guarantees

Preserve the save-before-delete sequence and the per-slot PDU comparison. Failed decoding or persistence must leave the device slot intact. SSH credentials must only be sent after the administrator confirms the host key, and all authenticated connections must verify that key.

## Reporting problems

For ordinary bugs, include the application version, CPU architecture, installation method, reproduction steps, and sanitized error output. Remove message contents and personal identifiers before attaching logs. Follow [SECURITY.md](SECURITY.md) for vulnerabilities.

Contributions are distributed under the project's [MIT license](LICENSE).
