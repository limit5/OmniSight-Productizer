# Deploy Prod Runbook

`scripts/deploy-prod.sh` is the rolling production deploy entrypoint.
Under the single-trunk release train (RT-20, image-tag-only) the only
deploy identity it accepts is a cosign-verified **image digest**:
`scripts/check_deploy_ref.sh` gates the digest, Alembic migrations are
applied, then backend replicas restart one at a time. The image is
already built and cosign-verified at that digest, so there is **no git
fetch / checkout and no source build** — the deploy never touches the
git working tree.

## Run From — and Advance — the Release-SHA-Pinned Prod Checkout (OP-1741)

OP-1717 moved the prod compose stack onto a **dedicated, release-SHA-pinned
checkout** at `/home/user/omnisight-prod`
(`omnisight-compose-prod.service` `WorkingDirectory`, pinned `@ v0.6.2/ecde4787`
at cutover). Staging boots from `/home/user/sora-bridge` (develop-tip). So a
production deploy **must run from `/home/user/omnisight-prod`** — never from
the develop-tip staging tree and never from a throwaway `git worktree add`
checkout under `/tmp`.

When a new release ships, the prod pin must **advance** to the release SHA so
the compose context (`docker-compose.prod.yml` + `scripts/`) matches the
deployed release. Two equivalent ways:

```bash
cd /home/user/omnisight-prod

# (A) one command — deploy-prod.sh advances the pin first, then deploys:
./scripts/deploy-prod.sh \
  --release-sha=<release-sha> \
  --backend-digest=sha256:<64hex> \
  --frontend-digest=sha256:<64hex>

# (B) two steps — advance the checkout, then deploy from it:
./scripts/advance_prod_checkout.sh --release-sha=<release-sha>
./scripts/deploy-prod.sh \
  --backend-digest=sha256:<64hex> \
  --frontend-digest=sha256:<64hex>
```

`--release-sha` (or `advance_prod_checkout.sh`) does a `git fetch` +
`git checkout --detach <release-sha>` and **fails closed if the working tree
is dirty** — unreviewed compose/script edits must not ship. With `--release-sha`,
`deploy-prod.sh` re-execs itself from the freshly-pinned tree so the rest of the
deploy runs at the release SHA.

**The SHA only moves the compose-file pin — it is NOT the deploy identity.**
The deploy is still by `--backend-digest`/`--frontend-digest` (RT-20, below);
the SHA never changes the digest-deploy contract. Omitting `--release-sha`
(e.g. redeploying / rolling the same release) still runs a canonical-checkout
assertion: a plain redeploy aborts on a dirty tree or a throwaway worktree, and
warns if run from a path other than the canonical pinned checkout. Override the
canonical path on non-standard hosts / in tests with `OMNISIGHT_PROD_CHECKOUT`.

## Deploy Identity (RT-20 — image-tag-only)

A production deploy identity is a cosign-verified image **digest**. The
backend and frontend are SEPARATE images with distinct digests, so a
fully digest-pinned deploy pins BOTH:

```bash
./scripts/deploy-prod.sh \
  --backend-digest=sha256:<64hex> \
  --frontend-digest=sha256:<64hex>
```

`--digest=sha256:<64hex>` is a back-compat alias for `--backend-digest`;
on its own it pins only the backend (the frontend then falls back to the
mutable `:${OMNISIGHT_IMAGE_TAG}` tag path — not a content-addressed
deploy), so pass both flags for a fully cosign-verified deploy.

**`--tag` / `v*` git-tag deploys are retired (OP-1734).** No `v*` git tag
is ever created — one would trip the `^v` CI build rule and rebuild a
*different* digest, breaking "validated digest == shipped digest". A
`--tag` invocation is rejected by `check_deploy_ref.sh` with an
actionable pointer back to the digest command above; there is no
`--gerrit-source` / git-fetch step to reach. Deploy the promoted image's
validated digest instead.

Rollback follows the same form: redeploy the previous release's validated
backend + frontend digests.

## Alembic Modes

Default mode applies migrations to the live database before any backend
replica restarts:

```bash
./scripts/deploy-prod.sh --alembic-mode=apply
```

For dry validation, use a PostgreSQL clone instead of Alembic `--sql`.
This catches migrations that depend on runtime bind behavior such as
`exec_driver_sql`, without mutating the live DB:

```bash
./scripts/deploy-prod.sh --alembic-mode=pg-clone
```

`pg-clone` reads `SQLALCHEMY_URL`, `OMNISIGHT_DATABASE_URL`, or
`DATABASE_URL` from the environment or `.env`, creates a temporary
database on `omnisight-pg-primary`, copies the live DB into it with
`pg_dump | pg_restore`, runs `python -m alembic upgrade heads` inside
the backend image against the clone, then drops the clone.

Use `--dry-run --alembic-mode=pg-clone` to print the clone plan without
creating or dropping any database.

## Pre-deploy Backup (fail-closed)

Before any replica restarts, `deploy-prod.sh` takes a WAL-safe online
backup via `scripts/backup_prod_db.sh` (Step 1b). This step is
**fail-closed (OP-1740)**: if the backup helper is missing or
non-executable, or the backup itself fails, the deploy **aborts** — it
no longer warns and proceeds without a backup. Skipped only in
`--dry-run`.

### Backup passphrase source

`backup_prod_db.sh` encrypts the dump with AES-256 and **requires**
`OMNISIGHT_BACKUP_PASSPHRASE` (it fails closed when unset). That
passphrase is **not** in `.env` — it lives in
`/etc/omnisight/backup-dr.env` on the prod DB host, the same file the
backup systemd timers source (see
`docs/operations/backup-dr-runbook.md` §2). `deploy-prod.sh` sources
that file (if present) before the backup step, so an interactive
operator deploy gets the passphrase without hunting for it. The
passphrase is never printed or echoed.

Override the path for testing/non-standard hosts with
`OMNISIGHT_BACKUP_DR_ENV=/path/to/env`. If the file is absent the deploy
falls back to whatever `OMNISIGHT_BACKUP_PASSPHRASE` is already exported
in the operator shell.

### Skipping the backup

`--skip-backup` is the only sanctioned way to deploy without a
pre-deploy backup. It is logged loudly and is **not recommended** —
reserve it for situations where a backup is provably redundant (e.g. an
immediately-prior manual snapshot).

## Image Tag Propagation

`OMNISIGHT_IMAGE_TAG` must be visible to Docker Compose and the SLO
monitor. `deploy-prod.sh` computes the current image tag from
`OMNISIGHT_IMAGE_TAG` (else the pinned backend/frontend digest), then
writes these values into `.env` before restarting services:

- `OMNISIGHT_IMAGE_TAG`
- `OMNISIGHT_PREVIOUS_IMAGE_TAG` when the tag changes

Do not rely on a one-off shell export alone for production deploy state;
the persistent `.env` value is the handoff point consumed by Compose
`env_file` and monitor restarts.
