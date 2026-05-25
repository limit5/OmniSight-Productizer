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
  the image build.
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
for it in this phase. (Candidate forward-compat preflight — migration/env/bundle
deployability through downstream stages — is a separate phase-2 workstream that
will fill the reserved `candidate_compat` JSONL `check_family`; this phase
implements only `parity`.)

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
