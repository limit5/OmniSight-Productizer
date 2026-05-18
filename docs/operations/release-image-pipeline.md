# Release Image Pipeline (OP-763)

## Goal

Every merge to `main` and every `v*` tag produces signed Docker images
for the three runtime planes — backend, frontend, hardware bridge —
pushed to GHCR, tagged immutably by git SHA, and signed with cosign
keyless. A retention sweep prunes old untagged builds while keeping
release tags forever.

## Triggers

The workflow `.github/workflows/build-images.yml` fires on:

| Event              | Builds | Tags applied                                                |
|--------------------|--------|-------------------------------------------------------------|
| `push` to `main`   | yes    | `:sha-<short>`                                              |
| `push` `v*` tag    | yes    | `:sha-<short>`, `:v<X.Y.Z>`, `:latest`                      |
| `workflow_dispatch`| yes    | same as the underlying ref                                  |
| `schedule` (weekly)| no     | retention-only                                              |

`:latest` is **only** updated by tag pushes — main-branch merges never
move `:latest`, so a deploy that pulls `:latest` always sees a real
release.

## Image inventory

| Image                                           | Dockerfile             | Source                          |
|-------------------------------------------------|------------------------|---------------------------------|
| `ghcr.io/<owner>/omnisight-backend`             | `Dockerfile.backend`   | `backend/`                      |
| `ghcr.io/<owner>/omnisight-frontend`            | `Dockerfile.frontend`  | Next.js app in repo root        |
| `ghcr.io/<owner>/omnisight-bridge`              | `Dockerfile.bridge`    | `tools/hardware_daemon/`        |

All three are multi-arch (`linux/amd64` + `linux/arm64`) so a single
pull resolves to the correct architecture on x86 cloud VMs and on
ARM SBCs / Apple-silicon hosts.

## Bundle manifest contract

The image workflow derives one `bundle_id` per git ref + commit and
passes it into all three Docker builds as `BUNDLE_ID`. It also writes a
schema-valid pre-build `bundle.json` before `docker/build-push-action`
runs, computes its sha256, and passes that value as `BUNDLE_SHA`.

Every pushed image carries these OCI labels:

| Label | Meaning |
|-------|---------|
| `org.opencontainers.image.bundle.id` | Bundle identity for the git ref + short SHA. |
| `org.opencontainers.image.bundle.sha` | sha256 of the pre-build `bundle.json` bytes used for the image build. |

Only the backend image bakes `bundle.json` into `/app/bundle.json`,
because `/api/version` reads that file. The frontend and bridge images
carry labels only; they do not serve `/api/version`.

The pre-build bundle uses placeholder image digests because the real
multi-arch digests do not exist until the images are pushed. After all
three images build and verify, the `bundle-manifest` job downloads the
per-image audit artifacts, re-runs `scripts/build_image_bundle.py` with
the real digests, validates the result against
`omnisight-bundle.schema.json`, and uploads
`bundle-<bundle_id>.json` as a workflow artifact.

Operator verification:

```bash
bundle_id=$(docker image inspect ghcr.io/<owner>/omnisight-backend:<tag> \
  --format '{{ index .Config.Labels "org.opencontainers.image.bundle.id" }}')
bundle_sha=$(docker image inspect ghcr.io/<owner>/omnisight-backend:<tag> \
  --format '{{ index .Config.Labels "org.opencontainers.image.bundle.sha" }}')
docker run --rm ghcr.io/<owner>/omnisight-backend:<tag> cat /app/bundle.json > /tmp/bundle.json
test "$(sha256sum /tmp/bundle.json | awk '{print $1}')" = "${bundle_sha}"
```

## Signing model — cosign keyless

We sign every image **by digest** (not by tag) using cosign keyless,
which exchanges the workflow's GitHub OIDC token for a short-lived
sigstore certificate. No long-lived signing key is held in CI.

The signature carries certificate claims that verifiers check:

- **Identity:** `https://github.com/<owner>/<repo>/.github/workflows/build-images.yml@<ref>`
- **Issuer:** `https://token.actions.githubusercontent.com`

If you ever need offline / air-gapped verification, drop a real PEM
key in `deploy/cosign/cosign.pub` (replacing the placeholder) and
add `cosign sign --key …` to the workflow. The verifier auto-detects
the key file and switches modes.

## Verifying a pulled image

```bash
scripts/verify_image_signature.sh ghcr.io/<owner>/omnisight-backend:v0.4.0
# → "OK" exit 0 on success, "FAIL" exit 1 on unsigned/tampered
```

The script prefers keyless verification, falling back to the project
public key if `deploy/cosign/cosign.pub` is populated with a real
`-----BEGIN PUBLIC KEY-----` block.

To verify an image built by a fork or a different workflow, override
the identity claims:

```bash
COSIGN_CERT_IDENTITY_REGEX='^https://github\.com/myfork/.*@refs/heads/main$' \
  scripts/verify_image_signature.sh ghcr.io/myfork/omnisight-backend:sha-abc123
```

## Retention policy

Enforced by the `retention` job (weekly cron, also runs on
`workflow_dispatch`). Implemented in `scripts/enforce_image_retention.sh`.

| Class                          | Policy                  |
|--------------------------------|-------------------------|
| Tagged (`v*`, `latest`, `sha-*`) | Kept forever            |
| Untagged, ≥ 30 days old        | Deleted (subject to floor) |
| Untagged, < 30 days old        | Kept                    |
| Floor                          | Last **20** untagged versions per package always kept |

The floor protects against runaway-build incidents and clock-skew
bugs — a misconfiguration that would otherwise prune everything still
leaves 20 versions per image.

Dry-run locally:

```bash
GH_TOKEN=$(gh auth token) \
PACKAGE_NAME=omnisight-backend \
OWNER=<your-org> \
DRY_RUN=1 \
  scripts/enforce_image_retention.sh
```

## Audit log integration (D18)

Each successful build emits `image-audit-<image>.json` as a workflow
artifact (90-day retention on the artifact itself). The structure is
deliberately compatible with the backend `audit.log()` payload shape
(see `backend/audit.py`):

```json
{
  "event": "image.signed_pushed",
  "image": "omnisight-backend",
  "registry": "ghcr.io",
  "digest": "sha256:…",
  "git_sha": "…",
  "git_ref": "refs/heads/main",
  "is_release": "false",
  "version": "",
  "actor": "claude-bot",
  "run_id": "1234567890",
  "run_url": "https://github.com/…/actions/runs/1234567890",
  "signed_with": "cosign-keyless",
  "cosign_version": "v2.4.1"
}
```

The prod-side audit poller (Phase-53 audit pipeline) is responsible
for downloading these artifacts and appending them to the hash-chained
audit log via `await audit.log(...)`. No long-lived backend credential
is embedded in CI.

## Synthetic pipeline test

The workflow includes a `verify` job that runs immediately after each
build matrix completes. It pulls the freshly-signed image by SHA tag
and runs `scripts/verify_image_signature.sh` against it — so a broken
signing step (e.g. revoked `id-token: write` permission) is caught
inside the same workflow run, not by an operator at deploy time.

To synthetically test the full pipeline outside CI:

1. Push a no-op commit to `main` (or open + merge a trivial PR).
2. Watch `.github/workflows/build-images.yml` in the Actions tab.
3. Once it goes green (target: < 10 min), run:
   ```bash
   short_sha=$(git rev-parse --short=12 HEAD)
   scripts/verify_image_signature.sh \
     ghcr.io/<owner>/omnisight-backend:sha-${short_sha}
   ```
   It should print `OK`.

## Troubleshooting

### "Error: failed to get tlog entries: ..."

The rekor log query failed. Either rekor is degraded (check
<https://status.sigstore.dev/>) or the runner has no egress to
`rekor.sigstore.dev`. Keyless verification cannot proceed without
rekor; fall back to key-based verification by populating
`deploy/cosign/cosign.pub` once a long-lived key exists.

### "FAIL: cosign keyless verification failed"

Common causes:

1. Image was pushed by a different workflow / fork — override
   `COSIGN_CERT_IDENTITY_REGEX`.
2. Image was pushed before signing was enabled — only images built
   after this workflow's first run on `main` are signed.
3. Tag has been overwritten since signing. Verify by digest instead:
   ```bash
   digest=$(docker buildx imagetools inspect ghcr.io/.../omnisight-backend:v0.4.0 --format '{{.Manifest.Digest}}')
   scripts/verify_image_signature.sh ghcr.io/.../omnisight-backend@${digest}
   ```

### Retention deleted too much / too little

Tweak `MIN_KEEP` / `MAX_AGE_DAYS` env vars in the workflow. `MIN_KEEP`
is a hard floor — the script refuses to dip below it even if every
remaining version is past the age cutoff.

## See also

- `.github/workflows/docker-publish.yml` — older tag-only publish
  (kept for parity until OP-763 has been live for one release cycle;
  the two workflows publish to the same GHCR namespace, so consumers
  see the same `:vX.Y.Z` regardless of which one ran).
- `docs/operations/release-discipline.md` — release-tagging policy
  and SemVer conventions.
- `docs/operations/key-management.md` — credential rotation, including
  any future cosign signing key.
