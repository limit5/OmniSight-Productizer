# Deploy Prod Runbook

`scripts/deploy-prod.sh` is the rolling production deploy entrypoint.
Under the single-trunk release train (RT-20, image-tag-only) the only
deploy identity it accepts is a cosign-verified **image digest**:
`scripts/check_deploy_ref.sh` gates the digest, Alembic migrations are
applied, then backend replicas restart one at a time. The image is
already built and cosign-verified at that digest, so there is **no git
fetch / checkout and no source build** — the deploy never touches the
git working tree.

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
