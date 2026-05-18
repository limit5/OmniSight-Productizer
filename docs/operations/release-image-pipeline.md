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

## Env lock files + preflight (OP-1482, V4)

Until OP-1482, prod and staging compose files referenced images by
tag (`ghcr.io/.../omnisight-backend:${OMNISIGHT_IMAGE_TAG}`) with
`pull_policy: missing`. That combination is unsafe: if the alias
(`:latest`, `:vX.Y.Z`, `:staging`) is retagged in the registry,
`docker compose up` keeps using whatever happens to be in the local
cache, silently no-op'ing a promotion. Codex flagged this as the #2
priority gap in the image pipeline.

The V4 fix is:

1. **Per-env lock file**, JSON, checked into the repo. Each file
   pins a `bundle_id`, the per-image `repository` + `sha256:` digest,
   `last_promoted_at`, `promoted_from`, and an `attestation_ref` back
   to a sigstore log entry:

   - `staging.env.lock.json` — sealed when CI cuts a new bundle
   - `canary.env.lock.json` — sealed by the canary cron when a bundle
     graduates from staging (≥6 h burn-in + clean alerts)
   - `prod.env.lock.json` — sealed when an operator approves the
     canary→prod promotion

   Schema (informal — `omnisight-bundle.schema.json` covers the
   compatible bundle manifest):

   ```json
   {
     "env": "prod",
     "bundle_id": "v0.5.0-3f1c0a4e",
     "last_promoted_at": "2026-05-18T14:22:00Z",
     "promoted_from": "canary",
     "attestation_ref": "sigstore://rekor.sigstore.dev/api/v1/log/entries/<uuid>",
     "images": {
       "backend":  { "repository": "ghcr.io/<owner>/omnisight-backend",
                     "digest":     "sha256:…",
                     "env_var":    "OMNISIGHT_BACKEND_DIGEST" },
       "frontend": { "repository": "ghcr.io/<owner>/omnisight-frontend",
                     "digest":     "sha256:…",
                     "env_var":    "OMNISIGHT_FRONTEND_DIGEST" },
       "bridge":   { "repository": "ghcr.io/<owner>/omnisight-bridge",
                     "digest":     "sha256:…",
                     "env_var":    "OMNISIGHT_BRIDGE_DIGEST" }
     }
   }
   ```

2. **`scripts/load_env_lock.sh`** translates the lock JSON into env
   vars consumed by the compose `image:` interpolation. Two modes:

   ```bash
   # Source-mode — exports OMNISIGHT_*_DIGEST into the caller shell.
   source scripts/load_env_lock.sh prod.env.lock.json

   # File-mode — writes a `--env-file`-formatted chunk. Preferred for
   # the deploy runbook so the digests live in a single artifact.
   scripts/load_env_lock.sh prod.env.lock.json --out /tmp/prod-digests.env
   ```

3. **`scripts/verify_image_bundle.py`** is the preflight gate. It
   refuses to exit 0 unless, for every image referenced in the lock:

   - the compose file's `image:` line is pinned by `@sha256:<digest>`
     (catches a future regression where someone re-introduces a
     `:tag` reference),
   - `docker manifest inspect` against the registry returns the same
     digest (catches a retagged alias),
   - the cosign signature on `<repo>@<digest>` verifies
     (delegates to `scripts/verify_image_signature.sh` for keyless /
     key-mode auto-detection consistency),
   - `docker image inspect`'s RepoDigests on the local cache include
     `<repo>@<digest>` (catches Codex's #15 failure mode where an
     operator `docker tag`'d an old digest onto the alias).

   ```bash
   python3 scripts/verify_image_bundle.py \
       --compose docker-compose.prod.yml \
       --lock prod.env.lock.json
   # exit 0 → safe to deploy
   # exit 1 → at least one image diverges; output names which and why
   # exit 2 → broken invocation (no docker, malformed lock); not a deploy
   #          failure — the runner can distinguish the two.
   ```

   Skip flags exist for situations where part of the chain is
   unreachable:

   - `--skip-remote` — no registry network, but still want compose +
     cosign + local-cache checks
   - `--skip-signature` — sigstore (rekor) is degraded; degrade
     gracefully and document in the deploy log
   - `--skip-cache` — first-time deploy on a clean host; the cache
     check would fail until `docker compose pull` lands

4. **Compose changes** (`docker-compose.prod.yml`,
   `docker-compose.staging.yml`): every `omnisight-{backend,frontend}`
   reference is now `@${OMNISIGHT_*_DIGEST}` and `pull_policy:
   always`. With digest pinning, `always` is safe — Docker
   content-addresses the local cache and will dedupe the layer pull
   when the digest is already present locally.

### Full deploy procedure (prod)

```bash
# 1. Load digests from the lock file.
scripts/load_env_lock.sh prod.env.lock.json --out /tmp/prod-digests.env

# 2. Preflight gate. Refuses to pass on any digest mismatch.
python3 scripts/verify_image_bundle.py \
    --compose docker-compose.prod.yml \
    --lock prod.env.lock.json

# 3. Apply.
docker compose \
    --env-file .env \
    --env-file /tmp/prod-digests.env \
    -f docker-compose.prod.yml \
    up -d --wait

# 4. Smoke: every running container must report a digest, not a tag.
docker ps --filter name=backend --format '{{.Image}}'
# Expected: ghcr.io/<owner>/omnisight-backend@sha256:…
```

### Promoting a bundle (sealing a new lock)

A bundle is "ready to promote" once CI's `image-audit-<image>.json`
artifacts (see § Audit log integration) name the same `bundle_id` and
cosign-verify clean. The promotion workflow rewrites
`<env>.env.lock.json` with the new digests, bumps `bundle_id`,
`last_promoted_at`, and `attestation_ref`, and commits the lock file
behind a Gerrit Code-Review +2 like any other change. Until that
commit is merged, deploy preflight refuses to run with the new
digests because the lock file is still pinned to the old bundle —
which is exactly the safety property we wanted.

## See also

- `.github/workflows/docker-publish.yml` — older tag-only publish
  (kept for parity until OP-763 has been live for one release cycle;
  the two workflows publish to the same GHCR namespace, so consumers
  see the same `:vX.Y.Z` regardless of which one ran).
- `docs/operations/release-discipline.md` — release-tagging policy
  and SemVer conventions.
- `docs/operations/key-management.md` — credential rotation, including
  any future cosign signing key.
- `omnisight-bundle.schema.json` + `scripts/build_image_bundle.py`
  (OP-1479) — bundle manifest schema and CI-side builder. The lock
  files share the same digest-pinning shape so a bundle manifest can
  be diffed against the live lock file at release time.
