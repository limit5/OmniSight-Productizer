# Runner Image Pipeline Runbook (OP-864)

> Sprint D, META OP-761 §Phase 1.
>
> Sibling document of `release-image-pipeline.md` (OP-763, which
> covers the production backend / frontend / bridge images on GHCR).
> This runbook is specifically for the **runner image** — the
> `auto-runner-jira.py` runtime that the agent fleet consumes from
> the self-hosted registry at `registry.sora.services:5000`.

## Goal

Every change to `Dockerfile.runner`, `backend/agents/`,
`auto-runner-jira.py`, or the build scripts produces a signed runner
image pushed to `registry.sora.services:5000/omnisight/runner` and
tagged by `<semver>` + `sha-<git-short>`. A weekly cron prunes old
tags per the retention policy.

## Pipeline overview

```
┌─────────────────────────────────────────────────────────────────┐
│  push / tag / workflow_dispatch                                 │
│     │                                                           │
│     ▼                                                           │
│  scripts/build_image.sh --tag vX.Y.Z                            │
│     │                                                           │
│     ├──── docker buildx build (Dockerfile.runner, multi-stage)  │
│     │                                                           │
│     ├──── docker push  → registry.sora.services:5000/...:vX.Y.Z │
│     │                  → registry.sora.services:5000/...:sha-Z  │
│     │                                                           │
│     └──── cosign sign --key $OMNISIGHT_COSIGN_KEY <ref>@<digest>│
│                                                                 │
│  weekly cron (Mon 04:11 UTC)                                    │
│     │                                                           │
│     └──── scripts/gc_image_registry.sh                          │
│              keeps last MAX_AGE_DAYS (30) OR MIN_KEEP (20)      │
└─────────────────────────────────────────────────────────────────┘
```

## Triggers (`.github/workflows/image-build.yml`)

| Event                       | Action                                       |
|-----------------------------|----------------------------------------------|
| Push to `main` (relevant files) | build, sign, push                          |
| Push tag `runner-v*`        | build, sign, push (canonical release)         |
| `workflow_dispatch`         | build with operator-supplied tag             |
| `schedule` (Mon 04:11 UTC)  | retention GC only — no build                  |

## Image inventory

| Image                                                       | Source              |
|-------------------------------------------------------------|---------------------|
| `registry.sora.services:5000/omnisight/runner:<semver>`     | `Dockerfile.runner` |
| `registry.sora.services:5000/omnisight/runner:sha-<short>`  | same                |

Both refs point at the same content per build, but the digest tag is
immutable across the registry's lifetime; the semver tag may be
re-pushed if a release is recut.

## Signing model — cosign key-based

We sign with a long-lived cosign key (not keyless / OIDC) because
the self-hosted registry is reachable only from inside the sora
network and the keyless model requires the verifier to reach
`fulcio.sigstore.dev` / `rekor.sigstore.dev`, which we don't want
to depend on for an internal control-plane image.

| Material                | Location                                            |
|-------------------------|-----------------------------------------------------|
| Private key (operator)  | `/home/user/.config/omnisight/cosign-private-key`   |
| Public key (verifiers)  | `deploy/cosign/runner-cosign.pub` (committed)       |
| Password (key encrypted)| `~/.config/omnisight/cosign-password` (local) /    |
|                         | `SORA_COSIGN_PASSWORD` (CI secret)                  |

### Key generation (first-time setup)

If `/home/user/.config/omnisight/cosign-private-key` does not yet
exist, generate the keypair:

```bash
mkdir -p /home/user/.config/omnisight
cd /home/user/.config/omnisight

# Generate. cosign prompts for an encryption password; pick a
# strong one and store it in `cosign-password` (mode 600) plus the
# `SORA_COSIGN_PASSWORD` GitHub Actions secret.
cosign generate-key-pair

# The above writes:
#   cosign.key   →  rename to cosign-private-key, chmod 600
#   cosign.pub   →  copy to <repo>/deploy/cosign/runner-cosign.pub
mv cosign.key cosign-private-key
chmod 600 cosign-private-key cosign-password
```

Then commit the public key to the repo (the private half never
leaves the operator host or the GHA secret store):

```bash
cp /home/user/.config/omnisight/cosign.pub \
   /home/user/work/sora/OmniSight/deploy/cosign/runner-cosign.pub
```

### CI key staging

`.github/workflows/image-build.yml` stages the key from the
`SORA_COSIGN_KEY` secret to
`/home/runner/.config/omnisight/cosign-private-key` with `chmod 600`
before invoking `build_image.sh`. The script reads the env override
`OMNISIGHT_COSIGN_KEY` to find it.

## Local build (operator)

```bash
# Build, sign, push:
scripts/build_image.sh --tag v0.2.0

# Build only — no push, no sign — quick smoke:
scripts/build_image.sh --tag v0.2.0 --no-push --no-sign

# Dry run (print the docker invocation but don't run it):
scripts/build_image.sh --tag v0.2.0 --dry-run
```

The script prints a final `BUILT image=... digest=... git_sha=...
tag=...` line on success — CI parses this for the audit log.

## Retention policy (AC #4)

> Keep last 30 days OR last 20 tags — whichever MORE.

Concretely (`scripts/gc_image_registry.sh`):

1. List all tags via `GET /v2/<name>/tags/list`.
2. For each tag, resolve the manifest's image-config `.created`
   timestamp.
3. Sort by `created` DESC (newest first).
4. **Floor**: the first `MIN_KEEP` (default 20) tags are *always*
   kept, regardless of age. Protects against clock skew or a
   runaway-build window.
5. **Window**: of the remainder, keep tags with
   `created ≥ now - MAX_AGE_DAYS` (default 30 days).
6. Delete everything else via
   `DELETE /v2/<name>/manifests/<digest>`.

The registry's garbage-collector then reclaims disk on its next
sweep (the OP-864 GC script handles *manifest* deletion; layer GC
runs separately on the registry side, configured via
`storage.delete.enabled=true` in the registry config).

### Run interactively

```bash
REGISTRY=registry.sora.services:5000 \
IMAGE_PATH=omnisight/runner \
REGISTRY_USER=$(cat /home/user/.config/omnisight/registry-user) \
REGISTRY_PASS=$(cat /home/user/.config/omnisight/registry-pass) \
DRY_RUN=1 \
scripts/gc_image_registry.sh

# Confirm the listed deletions look right, then re-run with DRY_RUN=0.
```

## Reproducibility (AC #5)

`build_image.sh` pins `SOURCE_DATE_EPOCH` to the commit's author
timestamp (`git show -s --format=%ct HEAD`) and passes
`--output type=docker,...,rewrite-timestamp=true` to BuildKit. With
BuildKit ≥ 0.12 on the same host, 2 builds at the same git SHA
produce identical image digests.

Verify locally:

```bash
git checkout <some-sha>
scripts/build_image.sh --tag v0.0.0-repro1 --no-push --no-sign
DIGEST1="$(docker image inspect --format '{{.Id}}' \
            registry.sora.services:5000/omnisight/runner:v0.0.0-repro1)"

# Rebuild from scratch:
docker buildx prune -af
scripts/build_image.sh --tag v0.0.0-repro2 --no-push --no-sign
DIGEST2="$(docker image inspect --format '{{.Id}}' \
            registry.sora.services:5000/omnisight/runner:v0.0.0-repro2)"

[[ "$DIGEST1" == "$DIGEST2" ]] && echo "REPRO OK" || echo "REPRO FAIL"
```

Known non-determinism sources we explicitly defang:

| Source                                  | Defense                                  |
|-----------------------------------------|------------------------------------------|
| `apt` index timestamps                  | not used (alpine)                        |
| `apk` cache files                       | `--no-cache` flag                        |
| pip `__pycache__/*.pyc`                 | `PYTHONDONTWRITEBYTECODE=1` + post-strip |
| pip `*.dist-info/RECORD` mtimes         | `rewrite-timestamp=true`                 |
| Image creation label                    | derived from `SOURCE_DATE_EPOCH`         |

Caveat: cross-host reproducibility (different kernel, different
buildkit version) is **not** guaranteed — that requires bit-exact
toolchain pinning out of scope for OP-864.

## Error catalog

| Code  | Symbol                      | Cause                                       | Action                                |
|-------|-----------------------------|---------------------------------------------|---------------------------------------|
| 1     | `ImageBuildFailed`          | Docker layer error                          | Re-run with `BUILDKIT_PROGRESS=plain`; if reproducible, file a child of OP-864 |
| 2     | `CosignSignFailed`          | Key missing or revoked                      | Verify `/home/user/.config/omnisight/cosign-private-key` exists and is the expected fingerprint; if revoked, rotate per §Key rotation |
| 3     | `RegistryPushUnauthorized`  | `docker push` 401/403                       | Check `SORA_REGISTRY_USER` / `SORA_REGISTRY_PASS` (CI) or your `docker login` (local) |
| 4     | bad invocation              | missing dep / bad arg                       | `scripts/build_image.sh --help`       |
| 1†    | `RetentionGCFailed`         | GC script could not list / delete           | Check `REGISTRY_USER/REGISTRY_PASS`; rerun with `DRY_RUN=1` to inspect |

† The GC script exits 1 on its own; the symbol is what the audit
log emits to the operator alert channel.

## Verification

To verify a deployed runner image:

```bash
cosign verify \
  --key deploy/cosign/runner-cosign.pub \
  registry.sora.services:5000/omnisight/runner:v0.2.0
```

If verification fails before deploy, **abort** — an unsigned or
mis-signed runner image is treated as a supply-chain compromise.

## Key rotation

1. Generate a new keypair (above).
2. Add the new public key as `deploy/cosign/runner-cosign.pub.next`.
3. Sign the next release with the new private key.
4. Once at least one fleet-wide deploy has consumed the new key,
   delete the old `.pub` and rename `.next` → primary.
5. Update the `SORA_COSIGN_KEY` GHA secret.

The window between step 2 and step 4 is the only time both keys are
trusted; keep it under 7 days.

## See also

- `docs/operations/release-image-pipeline.md` — sibling pipeline for
  GHCR-hosted production images (OP-763).
- ADR-0011 §D2 — sprint plan + rationale for separate runner vs.
  production-image pipelines.
- `scripts/verify_image_signature.sh` — verification helper (auto-
  detects key-based vs. keyless mode).
