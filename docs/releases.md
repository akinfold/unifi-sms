# Publish release images

The [Publish release images workflow](../.github/workflows/release.yml) builds one image for **Linux AMD64 and ARM64** and publishes it to GHCR and Docker Hub when a GitHub release is published. Draft releases and ordinary pushes do not publish images.

Every release runs the existing CI checks against the exact tagged commit before building. Registry credentials are used only in the publishing job. Actions are pinned to commit SHAs and updated by Dependabot.

## Configure the registries once

### Docker Hub

1. Create a **public** Docker Hub repository named `unifi-sms` under your account or organization. Public visibility allows installation without a Docker Hub login.
2. In [Docker account settings](https://app.docker.com/settings/personal-access-tokens), create a personal access token with repository **Read & Write** permission. Delete permission is not required. See [Docker's token instructions](https://docs.docker.com/security/access-tokens/personal-access-tokens/).
3. In the GitHub repository, open **Settings → Secrets and variables → Actions** and add:

   | Tab | Name | Value |
   | --- | --- | --- |
   | Variables | `DOCKERHUB_USERNAME` | The Docker ID used to sign in, not an email address. |
   | Secrets | `DOCKERHUB_TOKEN` | The Docker Hub access token. |
   | Variables (optional) | `DOCKERHUB_IMAGE` | A complete lowercase `namespace/repository`, such as `example-org/unifi-sms`. Defaults to `DOCKERHUB_USERNAME/unifi-sms`. |

For an organization repository, the token's account must have permission to push there. Set `DOCKERHUB_IMAGE` to the organization's repository while keeping the login account in `DOCKERHUB_USERNAME`. Store the token only as an Actions secret; do not add it to source files, Compose, `.env.example`, or workflow inputs.

If the required settings are missing, publication fails with a configuration error before logging in or pushing to either registry. Build-only runs do not require Docker Hub settings.

### GitHub Container Registry

GHCR uses the workflow's built-in `GITHUB_TOKEN` with `packages: write`; no personal GitHub token or extra secret is needed. Images use `ghcr.io/OWNER/REPOSITORY` in lowercase.

If the package already exists from a manual push, open its **Package settings → Manage Actions access**, add this repository, and grant **Write** access. A package that already inherits access from this repository needs no extra grant. See [GitHub's package permissions](https://docs.github.com/en/packages/learn-github-packages/about-permissions-for-github-packages#maintaining-access-to-packages-in-github-actions-workflows).

Check package visibility separately: a public source repository does not make every existing container package public. Set a new GHCR package to **Public** once if anonymous installation is required. The workflow does not change registry visibility.

## Publish a release

1. Merge the release source into the default branch and wait for CI to pass. Update version references in deployment examples and documentation when preparing a new version.
2. Open **Releases → Draft a new release**, create a tag such as `v0.2.0` at the intended commit, and write the release notes. The commit must include this workflow for automatic publication.
3. For a preview, use a tag such as `v0.2.0-rc.1` and select **Set as a pre-release**. The suffix and GitHub prerelease setting must agree. Build metadata suffixes such as `+build.1` are not supported as Docker tags.
4. Select **Publish release**. Open **Actions → Publish release images** and wait for all jobs to pass.
5. Check the run summary for image names, source commit, digest, and platforms. The workflow checks that both registries return the build's digest with AMD64 and ARM64 manifests.

For release `v0.2.0`, the workflow publishes `0.2.0` and `sha-FULL_COMMIT_SHA` to both registries. It updates `latest` only when GitHub identifies that release as the latest stable release. Prereleases receive their full version tag, such as `0.2.0-rc.1`, and never update `latest`. Rebuilding an older release also leaves `latest` unchanged.

Do not move a published release tag. A retry rebuilds the same source commit, but a changed base image can produce a different digest. Use a new version for source changes.

## Build without publishing, or retry a release

Open **Actions → Publish release images → Run workflow** on the default branch. Enter an **existing published release tag** and leave **Push images to both registries** unchecked to run CI and a multi-platform build without registry logins or pushes. The build checks both platforms in a temporary OCI archive; the archive is discarded with the runner. This mode can validate the workflow before Docker Hub credentials are configured.

To publish an existing release or retry a failed upload, run the same workflow with the checkbox selected. A manual run can publish a tag created before this workflow was added. Arbitrary branches, unpublished tags, and draft releases are rejected.

Uploads to separate registries are not atomic. If one upload succeeds and the other fails, fix access or connectivity and retry; the workflow republishes and verifies both version tags before updating `latest`. Runs for the same release do not cancel an active publication. Different release versions may build concurrently.

## Verify installation access

After a successful publication, replace the example Docker Hub namespace and version below with the run summary values:

```sh
docker buildx imagetools inspect ghcr.io/akinfold/unifi-sms:0.2.0
docker buildx imagetools inspect example/unifi-sms:0.2.0
```

Both should list `linux/amd64` and `linux/arm64`. To check anonymous installation, pull each version from a host that is not logged in to either registry. To use Docker Hub with the existing Compose file, replace only the `image:` value with `YOUR_NAMESPACE/unifi-sms:VERSION`; keep the data volume and application settings.
