# Staging Environment (OP-767)

Staging is a standalone Docker Compose environment for Sprint D release
promotion. It listens for the `main_promoted` release event, pulls the
matching GHCR images, applies Alembic, starts the stack, and runs the D7
smoke subset against `https://staging.sora.services`.

## Topology

The staging stack lives under `deploy/staging/` and mirrors production
at lower capacity:

- `postgres` — PostgreSQL 16 with a smaller memory envelope and a
  read-only mount for sanitized prod-backup refresh inputs.
- `backend-a` and `backend-b` — one container each, same backend image as
  prod, with 2 GB memory ceilings instead of prod's 4 GB per replica.
- `frontend` — same frontend image as prod, with
  `VITE_API_BASE=https://staging.sora.services/api` and
  `NEXT_PUBLIC_API_URL=https://staging.sora.services/api/v1`.
- `caddy` — Caddy JSON config at `deploy/staging/caddy.json`, routing
  `/api/*`, `/readyz`, and `/healthz` to backend-a/b and all other paths
  to frontend.
- `bridge-daemon` — profile-gated with `--profile bridge`; defaults to
  staging Gerrit settings and can be left disabled until the staging
  Gerrit vs mocked bridge scope decision is closed.

## Host Setup

Create the local env file on the staging host:

```bash
cp .env.example deploy/staging/.env
${EDITOR:-nano} deploy/staging/.env
```

Minimum staging-only values:

```bash
POSTGRES_PASSWORD=...
POSTGRES_DB=omnisight_staging
OMNISIGHT_GHCR_NAMESPACE=<ghcr-owner>
OMNISIGHT_AUTH_MODE=strict
OMNISIGHT_ENV=staging
```

DNS must point `staging.sora.services` at the staging host or tunnel.
Expose the stack with:

```bash
docker compose -f deploy/staging/docker-compose.yml \
  --env-file deploy/staging/.env up -d
curl -sf https://staging.sora.services/readyz
```

## Auto-Deploy Worker

The worker consumes the same release milestone log family as OP-766 and
OP-769:

```bash
python3 scripts/auto_deploy_staging.py \
  --event-log /home/user/work/sora/logs/release-milestone/systemd.log \
  --cursor /home/user/work/sora/logs/release-milestone/auto-deploy-staging.cursor
```

For each `main_promoted` record, the worker:

1. Sets `OMNISIGHT_IMAGE_TAG` from `image_tag`, `promoted_tip`,
   `main_sha`, or `sha`.
2. Runs `docker compose -f deploy/staging/docker-compose.yml pull`.
3. Starts PostgreSQL if needed.
4. Runs `python -m alembic upgrade heads` in an ephemeral `backend-a`
   container from the promoted image.
5. Runs `docker compose ... up -d`.
6. Runs `scripts/prod_smoke_test.py https://staging.sora.services --subset dag1`.

The default deadline is 300 seconds. A deploy that cannot finish pull,
migration, compose up, and smoke within that window exits non-zero
instead of emitting `staging_deployed`.

## Synthetic Check

Append one synthetic event to the release log:

```bash
printf '%s\n' '{"event":"main_promoted","promoted_tip":"<image-tag-or-sha>"}' \
  >> /home/user/work/sora/logs/release-milestone/systemd.log
```

Then watch the worker log. Success emits:

```json
{"event":"staging_deployed","image_tag":"<image-tag-or-sha>","staging_url":"https://staging.sora.services","deadline_seconds":300}
```

## Staging-Prod Parity Lesson

Staging is useful only when it rehearses prod's deploy contract, not just
prod's URL shape. Keep the same image names, Caddy routing layer,
backend-a/b service names, Alembic command, and smoke entrypoint in both
environments. Scale down resource ceilings and Postgres size, but avoid
inventing staging-only deploy steps; otherwise staging can pass while the
next prod promotion fails on a path staging never exercised.
