# Publish release images

The [Publish release images workflow](../.github/workflows/release.yml) builds one image for **Linux AMD64 and ARM64** and publishes it to GHCR and Docker Hub when a GitHub release is published. Draft releases and ordinary pushes do not publish images.

Every release runs CI against the exact tagged commit before publication. CI builds a local image, tests first-run setup, verifies an upgrade from the previous stable release, checks the full SMS cycle with simulated LTE Pro and SMTP servers, verifies email forwarding statuses in Chromium, and checks REST API, MCP, and webhooks. A failed check blocks publication to both registries. Release builds use registry credentials only in the publishing job. Actions are pinned to commit SHAs and updated by Dependabot.

## Fresh installation gate

CI starts the locally built image with the deployment security options and an empty data volume. It waits for Docker health and checks the setup form, CSRF protection, the eight-character password minimum, administrator creation, device onboarding, logout, and login. It then restarts the container with the same volume and verifies that the administrator, authenticated session, and closed setup wizard persist. The test removes its temporary container and volume, including after a failure, and prints container logs on failure.

This check runs in ordinary CI and release validation before either registry receives an image. It exercises the runner's native platform, Linux AMD64 in GitHub Actions; the release build still produces AMD64 and ARM64 images. It does not connect to an LTE device or SMTP server.

## SMS cycle gate

CI runs the candidate image in a disposable container with networking disabled. A simulated LTE Pro accepts real SSH connections with a pinned host key and serves synthetic SMS through the modem command protocol. A local SMTP server captures the email. Both servers use the container's loopback interface; the check needs no device, mail account, or registry credentials.

The check runs the application's worker and verifies PDU decoding, sender and received time, message and receipt storage, slot rereading, slot deletion, and email delivery. When the device receives a delete command, the simulator opens a separate SQLite connection and requires the archived SMS to be committed already. The SMTP server first rejects delivery temporarily: the SMS must remain archived, its slot must be cleared, and a retry must be recorded. The next worker run must deliver the email with the correct SMTP envelope, headers, sender, received time, and Unicode text. A final worker run must leave the archive unchanged and send no duplicate email.

This gate runs in ordinary CI and before every release publication. A failed assertion blocks both registries. It uses the runner's native platform, Linux AMD64 in GitHub Actions, and removes its temporary container and data volume after the run. To run it locally against an existing image:

```sh
python scripts/smoke_sms.py --image unifi-sms:ci
```

## Email forwarding UI gate

CI opens the candidate image in Chromium and signs in through the login form. The check uses a disposable container and data volume with synthetic SMS and forwarding records. It runs the web server without the importer worker, so no device connection or email delivery occurs.

The browser checks **Email sent**, **Email pending**, **Email error**, and **Email not queued** in both the inbox list and the message reader. A sent message must show its stored sending time in a visible, localized timestamp and the HTML `datetime` attribute. Its received time differs from its sending time to catch timestamp mix-ups. Unsent messages must have no sending timestamp.

The check disables forwarding and refreshes the inbox with a sent message open. Status labels and sending times must disappear from the list and reader, including after opening another SMS and reloading the page. Re-enabling forwarding must restore them. The database must retain the original sending time and delivery attempt counts.

This check runs in ordinary CI and release validation. A failure blocks publication to both registries. It uses the runner's native platform, Linux AMD64 in GitHub Actions. The test removes its temporary container and volume after each run; CI retains a failure screenshot and container logs for seven days.

To run it locally against an existing image, install the browser test dependencies on the host:

```sh
python -m pip install -r scripts/requirements-ui.txt
python -m playwright install --with-deps chromium
python scripts/smoke_forwarding_ui.py --image unifi-sms:ci
```

## Integration gate

CI starts the candidate image with a new data volume and signs in to its administrator interface in Chromium. REST API and MCP requests use actual HTTP connections; MCP checks use the official Python SDK. The check configures each integration through the settings pages and uses only synthetic SMS and credentials.

The check verifies separate Bearer keys for REST API and MCP, rejection of missing or incorrect keys, and rejection of session-only access. It tests generated and manually supplied keys, rotation and revocation, disabled endpoints, and SMS creation, listing, reading, updating, and deletion through both interfaces.

A local HTTP receiver rejects the first webhook with HTTP 503 and accepts its retry with HTTP 204. The check uses the application's queue and delivery functions to verify persisted retries, the payload, the configured secret header, an optional secret, a stable idempotency key, and no duplicate delivery. Chromium checks pending, error, and sent statuses, the sending timestamp, and hiding statuses when webhooks are disabled. Existing SMS cycle tests separately cover queueing webhooks after an imported SMS is committed.

A failed check blocks publication to both registries. The test uses the runner's native platform, Linux AMD64 in GitHub Actions, and removes its temporary containers and volume after the run. CI retains failure diagnostics for seven days. It never uses an existing installation or external integration credentials.

For local commands, see [Validate integrations in the container](development.md#validate-integrations-in-the-container).

## Upgrade gate

CI selects the highest published stable version below the release being validated. For example, rebuilding `v0.1.2` tests an upgrade from `v0.1.1`, even if newer releases exist. A prerelease uses the previous stable version; drafts and prereleases are never upgrade sources. Ordinary branch and pull request CI uses the highest published stable release.

The check pulls that version from GHCR, verifies its source revision against the release tag, and uses the downloaded image by digest. It starts the previous image with a disposable volume, creates an administrator through the setup form, and saves synthetic SMS, trusted device settings, SSH credentials, and SMTP settings using that image's Django runtime. It then stops the old container and starts the candidate image with the same volume.

The candidate must preserve the administrator's password and permissions, existing session, all stored SMS fields and receipts, device and SMTP settings, encrypted SSH credentials, persistent application secret, and `known_hosts` file. The check also verifies credential decryption, SQLite integrity, a fresh administrator login, closed first-run setup, and access to the inbox and settings pages. New database columns are allowed; existing values must remain intact.

The installation and upgrade smoke tests use separate temporary volumes and named containers, remove them after the run, and print container logs on failure. Fixture connection settings point only to the test container's loopback interface; no real device or SMTP server is contacted. The upgrade check runs on the runner's native platform, Linux AMD64 in GitHub Actions. It skips only when no earlier published stable release exists. A missing image, mismatched revision, download failure, or failed assertion blocks publication.

For manual rebuilds of older releases, CI takes the smoke tests and version selector from the workflow commit while building the application from the resolved release commit. The old release does not need to contain the new scripts. The candidate must still pass every current gate; a release that predates a required integration will fail that integration check.

## Configure the registries once

### Docker Hub

1. Create a **public** Docker Hub repository named `unifi-sms` under your account or organization. Public visibility allows installation without a Docker Hub login.
2. In [Docker account settings](https://app.docker.com/settings/personal-access-tokens), create a personal access token with repository **Read, Write, Delete** permission. The description-update action requires this permission level; **Read & Write** is sufficient only for pushing images. See [Docker's token instructions](https://docs.docker.com/security/access-tokens/personal-access-tokens/) and the [action's token requirements](https://github.com/peter-evans/dockerhub-description#action-inputs).
3. In the GitHub repository, open **Settings → Secrets and variables → Actions** and add:

   | Tab | Name | Value |
   | --- | --- | --- |
   | Variables | `DOCKERHUB_USERNAME` | The Docker ID used to sign in, not an email address. |
   | Secrets | `DOCKERHUB_TOKEN` | The Docker Hub access token. |
   | Variables (optional) | `DOCKERHUB_IMAGE` | A complete lowercase `namespace/repository`, such as `example-org/unifi-sms`. Defaults to `DOCKERHUB_USERNAME/unifi-sms`. |

If you already configured a **Read & Write** token, replace the `DOCKERHUB_TOKEN` secret with a **Read, Write, Delete** token before the next image publication or description update. No additional Actions secret is needed.

For an organization repository, the token's account must have **Admin** permission to update its description. Set `DOCKERHUB_IMAGE` to the organization's repository while keeping the login account in `DOCKERHUB_USERNAME`. Store the token only as an Actions secret; do not add it to source files, Compose, `.env.example`, or workflow inputs.

If the required settings are missing, publication fails with a configuration error before logging in or pushing to either registry. Build-only runs do not require Docker Hub settings.

### GitHub Container Registry

GHCR uses the workflow's built-in `GITHUB_TOKEN` with `packages: write`; no personal GitHub token or extra secret is needed. Images use `ghcr.io/OWNER/REPOSITORY` in lowercase.

If the package already exists from a manual push, open its **Package settings → Manage Actions access**, add this repository, and grant **Write** access. A package that already inherits access from this repository needs no extra grant. See [GitHub's package permissions](https://docs.github.com/en/packages/learn-github-packages/about-permissions-for-github-packages#maintaining-access-to-packages-in-github-actions-workflows).

Check package visibility separately: a public source repository does not make every existing container package public. Set a new GHCR package to **Public** once if anonymous installation is required. The workflow does not change registry visibility.

## Update the GHCR description

Each release build takes the first introductory paragraph after the title in the tagged commit's `README.md`, removes inline Markdown, and joins the text into one line. Descriptions longer than 512 characters are shortened with `...` to fit [GHCR's description limit](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#labelling-container-images).

The release workflow adds this text to the image label and the multi-platform index annotation `org.opencontainers.image.description`. GHCR displays the index annotation on the package page. The same image metadata is published to both registries; this does not change Docker Hub's full description.

No additional secret or registry setting is needed. README-only pushes do not change the GHCR description; the next image publication uses the README from its release commit. Rebuilding an older release uses that release's README.

## Update the Docker Hub description

The [Update Docker Hub description workflow](../.github/workflows/dockerhub-description.yml) updates the repository's full description whenever `README.md` or that workflow changes on `main`. It uses the latest README from the default branch and converts relative Markdown links to GitHub URLs.

This workflow uses the Docker Hub settings above and does not build or push images. README-only pushes and pull requests also skip the CI workflow, which includes a Docker build. Image releases do not update Docker Hub's full description, so rebuilding an older release cannot overwrite it with an older README.

To update the description manually, open **Actions → Update Docker Hub description → Run workflow**, select `main`, and run it. Manual runs from other branches are skipped.

If an update fails, check that `DOCKERHUB_TOKEN` has **Read, Write, Delete** permission and that its account can administer the Docker Hub repository. Retry this workflow after fixing access; no release or image rebuild is needed.

## Publish a release

1. Merge the release source into the default branch and wait for CI to pass. Update version references in deployment examples and documentation when preparing a new version.
2. Open **Releases → Draft a new release**, create a tag such as `v0.2.0` at the intended commit, and write the release notes. The commit must include this workflow for automatic publication.
3. For a preview, use a tag such as `v0.2.0-rc.1` and select **Set as a pre-release**. The suffix and GitHub prerelease setting must agree. Build metadata suffixes such as `+build.1` are not supported as Docker tags.
4. Select **Publish release**. Open **Actions → Publish release images** and wait for all jobs to pass.
5. Check the run summary for image names, source commit, digest, and platforms. The workflow checks that both registries return the build's digest, AMD64 and ARM64 manifests, and the README description in the image index.

For release `v0.2.0`, the workflow publishes `0.2.0` and `sha-FULL_COMMIT_SHA` to both registries. It updates `latest` only when GitHub identifies that release as the latest stable release. Prereleases receive their full version tag, such as `0.2.0-rc.1`, and never update `latest`. Rebuilding an older release also leaves `latest` unchanged.

Do not move a published release tag. A retry rebuilds the same source commit, but a changed base image can produce a different digest. Use a new version for source changes.

## Build without publishing, or retry a release

Open **Actions → Publish release images → Run workflow** on the default branch. Enter an **existing published release tag** and leave **Push images to both registries** unchecked to run CI and a multi-platform build without registry logins or pushes. The build checks both platforms and the description annotation in a temporary OCI archive; the archive is discarded with the runner. This mode can validate the workflow before Docker Hub credentials are configured.

To publish an existing release or retry a failed upload, run the same workflow with the checkbox selected. A manual run can publish a tag created before this workflow was added. Arbitrary branches, unpublished tags, and draft releases are rejected.

Uploads to separate registries are not atomic. If one upload succeeds and the other fails, fix access or connectivity and retry; the workflow republishes and verifies both version tags before updating `latest`. Runs for the same release do not cancel an active publication. Different release versions may build concurrently.

## Verify installation access

The public images are `ghcr.io/akinfold/unifi-sms` and `docker.io/akinfold/unifi-sms`. After a successful publication, replace the example version below with the run summary value. For a fork, also use the image names from that summary:

```sh
docker buildx imagetools inspect ghcr.io/akinfold/unifi-sms:0.2.0
docker buildx imagetools inspect docker.io/akinfold/unifi-sms:0.2.0
```

Both should list `linux/amd64` and `linux/arm64`. To check anonymous installation, pull each version from a host that is not logged in to either registry. To use Docker Hub with the existing Compose file, replace only the `image:` value with `docker.io/akinfold/unifi-sms:VERSION`; keep the data volume and application settings.
