# Image Promotion Runbook (OP-1481)

## Goal

Promote an already-built image bundle between environment aliases
without rebuilding. Promotion retags each image by digest with
`docker buildx imagetools create`, preserving the original multi-arch
manifest and the original cosign signature on the source digest. The
promotion event receives its own cosign attestation and a local JSONL
audit row.

## Prerequisites

- `docker buildx` can read and write the target registry.
- `cosign` can verify the build signature and create keyless
  attestations via the same GitHub Actions OIDC/Fulcio path used by
  the image build. The inline verify (`scripts/verify_image_signature.sh`)
  self-locates cosign — it honours `$COSIGN_BIN`, else probes `~/bin` /
  `/usr/local/bin` / `~/go/bin` (OP-1736). Run promote from your normal
  shell (not `env -i`, which strips a `~/bin` cosign and aborts the
  promote mid-retag); use `OMNISIGHT_TOOLING_TOLERATE_EXTRA_ENV=1` if the
  gate trips pydantic on a polluted shell.
- The bundle manifest exists as `artifacts/bundle-<bundle-id>.json`,
  `bundles/<bundle-id>/bundle.json`, or an explicit path passed to
  `--bundle`.
- The operator has approval references ready, for example
  `OP-1234,OP-5678`.

## Promotion preflight — cross-stage parity (OP-1720)

Before promoting between stages, run the **declarative cross-stage parity
audit** as a preflight gate. It is the standing, re-runnable successor to the
2026-05-25 deploy-line deep audit (OP-1709): it reads the committed deploy
artefacts (compose / `.env` / env-lock / systemd unit files) **statically** and
flags wrong-direction drift across `dev → staging → canary → prod` — e.g. a prod
stage left on a mutable `latest` tag, a stage defaulting to the decommissioned
GHCR registry (ADR-0042), a backend/frontend pair pinned inconsistently (RT-21),
`AUTH_MODE` looser than the stage allows, or a non-PostgreSQL DSN where one is
required. Direction matters: prod/canary are strictest, dev loosest, so a
right-direction difference (dev `AUTH_MODE=open`) is OK while the same value at
prod is a violation.

```bash
# Static preflight (exit non-zero on any wrong-direction parity violation):
scripts/deployment-audit.sh --cross-stage-parity

# Best-effort live annotation from /readyz + /api/version + systemctl
# (unreachable targets are annotated live=unknown — never a hard failure):
scripts/deployment-audit.sh --cross-stage-parity --live

# Persist a JSONL row (mirrors the deployment-audit sink convention):
DEPLOY_PARITY_JSONL_LOG=audit/deploy_line_parity.jsonl \
  scripts/deployment-audit.sh --cross-stage-parity
```

A non-zero exit means the line is internally inconsistent — **stop and fix the
drift before promoting** (the audit is REPORT-ONLY; it never remediates). This
is an on-demand + promotion-preflight invocation only; **no timer is installed**
for it in this phase.

## Promotion preflight — candidate forward-compat (OP-1721)

The cross-stage parity audit above answers *"is the deployed line internally
consistent?"*. Phase 2 answers the complementary promote-time question: *"is
**this candidate bundle** deployable through the downstream stages?"* — run it
before promoting a freshly-built `bundle.json`. It is **STATIC ONLY**: it reads
the candidate bundle, the downstream stage compose/env, and the
`backend/alembic/versions` tree, and runs **no live migration**, **no release
simulation**, and **no live stage probe**. Four checks, emitted as
`check_family=candidate_compat` rows:

1. **bundle completeness** — backend + frontend image digests present and
   non-placeholder (an all-zeros digest is not deployable; deep-audit #23).
2. **env-contract delta** — any env var the candidate declares (its compose /
   `.env`) that **no** downstream stage (staging/canary/prod) can supply is
   flagged `declared by candidate, no matching stage key` (reuses the OP-1720
   env-contract extractor).
3. **migration compatibility** — the candidate's `contracts.db_migration_head`
   must be **descendant-reachable from the deployed head** (a static
   reachability walk reusing `scripts/check_migration_compat.py`; **never** a
   live `alembic upgrade/downgrade` against any DB).
4. **FE bundle-shape** — the candidate's `contracts.frontend_built_against_api`
   must be in its `contracts.api_supported` set, else the V5
   `frontend_compat_check` (see [`release-image-pipeline.md`](release-image-pipeline.md)
   §"Tie-in to V5 `frontend_compat_check`") would FAIL post-deploy.

```bash
# Static forward-compat preflight for a candidate bundle (exit non-zero on any
# forward-compat break). --deployed-head is the alembic head prod is currently
# on (the base the candidate must build forward from):
scripts/deployment-audit.sh --cross-stage-parity \
  --candidate-bundle bundle.json \
  --candidate-compose docker-compose.prod.yml \
  --deployed-head "$(OMNISIGHT_DEPLOYED_TAG=v0.5.0 \
      git grep -hE '^revision' v0.5.0 -- backend/alembic/versions | …)"

# Persist a JSONL row (candidate_compat rows land alongside the parity rows):
DEPLOY_PARITY_JSONL_LOG=audit/deploy_line_parity.jsonl \
  scripts/deployment-audit.sh --cross-stage-parity --candidate-bundle bundle.json
```

A non-zero exit means the candidate would break forward through the line —
**do not promote** until the flagged digest/env/migration/FE-shape issue is
resolved at build time. Like the parity audit this gate is REPORT-ONLY: it never
touches the release-train, registry, tags, or promotion flow.

## Dry Run

```bash
python3 scripts/promote_image_bundle.py \
  --bundle v0.5.0-rc3-3f1c0a4e \
  --from staging \
  --to canary \
  --actor sora \
  --approval-refs OP-1234,OP-5678 \
  --dry-run
```

The dry run prints every `docker buildx imagetools create`, `cosign
verify`, and attestation command it would execute. It does not write
`audit/image_promotion_audit.jsonl` and does not update the target
lock file.

## Staging To Canary

```bash
python3 scripts/promote_image_bundle.py \
  --bundle v0.5.0-rc3-3f1c0a4e \
  --from staging \
  --to canary \
  --actor sora \
  --approval-refs OP-1234,OP-5678 \
  --no-dry-run
```

After the command completes:

```bash
tail -n 1 audit/image_promotion_audit.jsonl | jq .
docker buildx imagetools inspect ghcr.io/omnisight/omnisight-backend:canary
cosign verify-attestation \
  --type omnisight.image.promotion.v1 \
  ghcr.io/omnisight/omnisight-backend@sha256:<digest>
```

## Canary To Prod

Use the same bundle id and approval references, changing the source
and target aliases:

```bash
python3 scripts/promote_image_bundle.py \
  --bundle v0.5.0-rc3-3f1c0a4e \
  --from canary \
  --to prod \
  --actor sora \
  --approval-refs OP-1234,OP-5678 \
  --no-dry-run
```

Each invocation appends one JSONL row and updates the target lock file
(`canary.env.lock.json` or `prod.env.lock.json` unless `--lock-file`
is supplied).

## Rollback

Promotion rollback is the same zero-copy operation pointed at the
previous known-good bundle:

```bash
python3 scripts/promote_image_bundle.py \
  --bundle v0.5.0-previous-9d2cbeef \
  --from prod \
  --to prod \
  --actor sora \
  --approval-refs OP-rollback-incident \
  --no-dry-run
```

Verify the prod alias resolves to the previous digest:

```bash
docker buildx imagetools inspect ghcr.io/omnisight/omnisight-backend:prod \
  --format '{{.Manifest.Digest}}'
```

Compare that digest to the previous bundle manifest. The digest must
match exactly; if it does not, stop and investigate before restarting
services.

## Local Registry Test Path

For isolated retag testing against `registry:2`, create a bundle whose
image entries include local `repository` values such as
`localhost:5000/omnisight-backend`. Then run the CLI with
`--skip-cosign-verify` because the local fixture image is not signed:

```bash
python3 scripts/promote_image_bundle.py \
  --bundle /tmp/local-bundle.json \
  --from staging \
  --to canary \
  --actor sora \
  --approval-refs OP-1481 \
  --audit-log /tmp/image_promotion_audit.jsonl \
  --lock-file /tmp/canary.env.lock.json \
  --skip-cosign-verify \
  --no-dry-run
```

This mode is only for the local registry fixture. Real promotions must
verify cosign and must leave `--skip-cosign-verify` unset.
