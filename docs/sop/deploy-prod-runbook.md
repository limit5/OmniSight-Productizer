# Deploy Prod Runbook

`scripts/deploy-prod.sh` is the rolling production deploy entrypoint. It
fetches the deploy ref, verifies it with `scripts/check_deploy_ref.sh`,
builds or reuses images, handles Alembic, then restarts backend replicas
one at a time.

## Git Source

Production deploys must fetch from Gerrit, not a stale GitHub mirror.
The script auto-detects the source in this order:

1. `--gerrit-source=<remote>` or `OMNISIGHT_GERRIT_SOURCE=<remote>`.
2. A remote named `gerrit`.
3. Any configured remote whose URL looks like Gerrit (`gerrit`,
   `sora.services`, or SSH port `29418`).

If no Gerrit source is found, the deploy refuses to continue. To force a
known-good remote:

```bash
./scripts/deploy-prod.sh --gerrit-source=gerrit
```

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
`OMNISIGHT_IMAGE_TAG`, `--tag`, or the current commit, then writes these
values into `.env` before restarting services:

- `OMNISIGHT_IMAGE_TAG`
- `OMNISIGHT_PREVIOUS_IMAGE_TAG` when the tag changes

Do not rely on a one-off shell export alone for production deploy state;
the persistent `.env` value is the handoff point consumed by Compose
`env_file` and monitor restarts.
