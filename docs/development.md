# Build and develop UniFi SMS

For deployment from the published image, use the [README quick start](../README.md#quick-start-with-docker-compose). The procedures below require a source checkout. Repository access is separate from anonymous access to the public image.

## Build from source with Docker Compose

From the repository root:

```sh
cp .env.example .env
docker compose -f compose.yaml up --build -d
docker compose -f compose.yaml ps
```

Open [http://localhost:8080](http://localhost:8080), create the administrator, and complete the device wizard. The source Compose file builds `unifi-sms:0.1.5` locally, binds port `8080` to loopback, and uses project `unifi-sms`, container `unifi-sms-app`, and volume `unifi-sms_sms-data`.

The source and registry Compose files identify the same deployment. Do not start both as separate installations on one host. The explicit `-f compose.yaml` also excludes local override files; include any intended override explicitly.

## Run locally with Python

Use Python **3.14** and the pinned dependencies in `requirements.txt`. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py bootstrap
.venv/bin/python manage.py runserver --insecure
```

In a second terminal, from the same directory:

```sh
.venv/bin/python manage.py runworker
```

Open [http://localhost:8000](http://localhost:8000). `--insecure` serves the collected local assets through Django's development server. Use the Docker image for deployment.

The local application stores its database and persistent secret in the ignored `data/` directory unless `DATA_DIR` is set. Stop any deployed importer that uses the same device before activating a local connection.

## Validate changes

With the Python environment above and Node.js 22 available, install the development tools and run:

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m ruff check .
.venv/bin/python -m pip_audit --disable-pip --no-deps -r requirements.txt
.venv/bin/python manage.py test inbox modem
.venv/bin/python manage.py makemigrations --check --dry-run
node --test tests/*.test.cjs
docker build -t unifi-sms:ci .
python3 scripts/smoke_install.py --image unifi-sms:ci
```

The installation smoke test requires a running local Docker daemon. It uses a temporary container, a new named volume, and a loopback port assigned by Docker. It creates a disposable administrator through the HTTP setup form and verifies login and persistence after a restart. It removes its test resources on success or failure and leaves existing installations unchanged. CI runs this check before release publication.

To test an upgrade, pull the previous stable image and pass it alongside the candidate image:

```sh
docker pull ghcr.io/akinfold/unifi-sms:0.1.4
python3 scripts/smoke_upgrade.py --previous-image ghcr.io/akinfold/unifi-sms:0.1.4 --image unifi-sms:ci
```

The upgrade test creates synthetic data with the previous image, stops it, and starts the candidate with the same temporary volume. It checks administrator login, sessions, SMS, device and SMTP settings, encrypted credentials, and host trust. It removes both containers and the volume after the run. CI selects and verifies the previous release automatically; see the [upgrade gate](releases.md#upgrade-gate).

## Validate integrations in the container

Build the candidate image with `docker build -t unifi-sms:ci .`, then install the host-side test dependencies and Chromium:

```sh
python -m pip install -r scripts/requirements-integrations.txt
python -m playwright install --with-deps chromium
python scripts/smoke_integrations.py --image unifi-sms:ci
```

The check uses disposable Docker resources, synthetic SMS, a test administrator, the official MCP SDK, Chromium, and a local webhook receiver. It verifies API authentication and CRUD, webhook delivery and retries, and status visibility. Existing containers and their data are not used. Pass `--artifacts-dir PATH` to save failure diagnostics. CI runs the same [integration gate](releases.md#integration-gate) before publication to either registry.

## Test coverage

Tests use synthetic messages and a simulated modem. They cover PDU decoding, saving before deletion, slot reuse, recovery, multipart assembly, administrator setup, authentication and CSRF, trusted connection setup, device credential encryption, SMTP settings, and forwarding retries. They also cover REST and MCP authentication and archive CRUD, webhook settings and retries, delivery status rendering, and hiding disabled integrations. Legacy device and SMTP environment variables are ignored rather than overriding saved settings.

Version `0.1.1` and the current source confirm the host key before sending SSH credentials and use Paramiko 5.0.0. They reject legacy SHA-1 SSH algorithms; verify firmware compatibility before upgrading from `0.1.0`.

A Linux-only test exercises the device-side shell through a virtual serial port, including mode restoration and lock cleanup. Verify compatibility with the target LTE Pro firmware before relying on unattended import; simulated tests do not establish hardware compatibility.

PDU fields follow [3GPP TS 23.040](https://www.etsi.org/deliver/etsi_ts/123000_123099/123040/16.00.00_60/ts_123040v160000p.pdf). SMS access uses SSH and the modem serial interface; it does not use the controller HTTP API.
