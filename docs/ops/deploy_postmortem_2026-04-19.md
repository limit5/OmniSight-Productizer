# Production Deploy Post-Mortem — 2026-04-19

**Outcome**: First-time production bootstrap on WSL Ubuntu-24.04 via Path B
(`docker-compose.prod.yml` + Caddy dual-replica + self-signed internal CA)
completed successfully. All 4 services report `healthy`; end-to-end smoke
(login + /readyz + /api/v1/health + TLS via Caddy) passes.

**Final state**:

```
SERVICE     STATE     STATUS
backend-a   running   healthy    readyz.ready=true, migrations=0015
backend-b   running   healthy    readyz.ready=true, migrations=0015
caddy       running   healthy    https://localhost/ TLS via internal CA
frontend    running   healthy    :3000/ → /setup-required (307)
```

**Operator**: `user` on host `X870E-NOVA-WIFI` (WSL Ubuntu 24.04, Docker 29.4).
**Commit deployed**: master @ `cc55200`.
**Script used**: `scripts/bootstrap_prod.sh --yes`.

---

## 1. Topology (per `docs/ops/multi-wsl-deployment.md`)

| Role | WSL | Deploy mode | Public |
|---|---|---|---|
| **Production** | Ubuntu-24.04 *(this host)* | docker-compose.prod.yml | Cloudflare Tunnel → sora-dev.app |
| Staging | Ubuntu-22.04 | docker-compose.staging.yml | — |
| Dev + Testing | Ubuntu-26.04 *(future, shared with 24.04 today)* | `uvicorn --reload` + `next dev` | localhost |

Redis (`ai_cache`) and the existing Cloudflare Tunnel (`ai_tunnel`) on this host
belong to a separate `omnisight-ai-core_omnisight_net` network — we chose **not**
to join them for first-boot to minimise the dependency surface; see §5.

---

## 2. Issues encountered (ordered by discovery)

### 2.1 Script design — misreading the canonical deploy path

We initially followed `docs/ops/production_deploy.md §4.1 Path A` (systemd
single-host). `docs/ops/multi-wsl-deployment.md` clearly specifies Path B
(docker-compose HA) for the 24.04 prod role. Both paths are supported but only
Path B matches the documented topology. The systemd attempt was reverted via
`git checkout --` + `git clean`; all artifacts live on branch
`backup/deploy-attempts-20260419`.

### 2.2 `bash` gotcha — retry helper silently swallowed failures

Pattern used:

```bash
retry() {
  while ((a <= tries)); do
    if "$@"; then return 0; fi    # ← BUG
    rc=$?                          # $? clobbered to 0 here
    ...
  done
}
```

When the `if cmd` condition is false and no `then`/`else` branch runs, bash
resets `$?` to 0. So every failed retry reported `rc=0` and propagated success,
hiding pip / pnpm / alembic failures. **Fix**: `"$@" && return 0; rc=$?` —
preserves the command's exit code.

### 2.3 pydantic `Settings` `extra='forbid'` on `.env`

`backend/config.Settings` declares some keys as fields and reads others via
`os.environ.get(...)`. Putting the latter in `.env` crashes pydantic with
`ValidationError: Extra inputs are not permitted` for 8 keys (AUTH_MODE /
ADMIN_* / DECISION_BEARER / COOKIE_SECURE / READYZ_DEEP_CHECK / BACKEND_URL /
NEXT_PUBLIC_API_URL).

**Path A fix** (abandoned): split into `.env` + `.env.secrets`.
**Path B fix** (used): single merged `.env`. Works because the container's
`WORKDIR=/app` has no `.env` file copied in (the Dockerfile only copies
`.env.example`), so pydantic reads **only** `os.environ` (populated by compose
from the host's `.env` via `env_file:` — no `extra_forbidden` trap).

### 2.4 Next.js 16 — client taint prevents `generateMetadata` export

`app/api/workspace/[type]/session/route.ts` imported `WORKSPACE_TYPES` /
`isWorkspaceType` from `app/workspace/[type]/layout.tsx`. That layout
transitively imports `PersistentWorkspaceProvider` (has `"use client"`) →
layout becomes client-tainted → Turbopack refuses `export async function
generateMetadata`.

**Fix**: extract pure constants/types to `app/workspace/[type]/types.ts`
(zero React/client dependencies). All **8 importers** were updated (7
component/hook files + 1 API route):

```
app/api/workspace/[type]/session/route.ts
components/omnisight/persistent-workspace-provider.tsx
components/omnisight/workspace-bridge-card.tsx
components/omnisight/workspace-chat.tsx
components/omnisight/workspace-context.tsx
components/omnisight/workspace-navigation-sidebar.tsx
components/omnisight/workspace-shell.tsx
hooks/use-workspace-persistence.ts
```

`layout.tsx` re-exports the 3 symbols for backward compat.

### 2.5 PEP 668 on Ubuntu 24.04 (Path A only)

`pip install` refused on noble (`externally-managed-environment`). Would have
required a venv. **Not relevant to Path B** — deps install inside the Docker
image's builder stage which already uses `pip install --require-hashes` in an
isolated filesystem.

### 2.6 Alembic — `script_location` is relative to CWD, not the ini file

`backend/alembic.ini` has `script_location = alembic`. Alembic resolves that
against the **invocation CWD**, not the ini's directory. Running from repo
root or `/app` therefore looked for `/app/alembic` (doesn't exist). **Fix**:
`docker compose exec -w /app/backend` so CWD is `/app/backend` when alembic
starts.

### 2.7 `backend/platform.py` shadows stdlib `platform` inside container

Python auto-prepends CWD to `sys.path`. Once we cd'd to `/app/backend` for
alembic, the project-local `backend/platform.py` (a legitimate internal
module named after a stdlib module) won the import race over stdlib, and
SQLAlchemy's top-level `import platform` crashed with
`AttributeError: no attribute 'python_implementation'`. **Fix**:
`-e PYTHONSAFEPATH=1` — Python 3.11+ flag that disables the auto-inject.
Only needed for alembic; uvicorn's CWD is `/app` (no collision).

### 2.8 SQLite first-boot WAL-mode lock race

Both backend replicas share the same `omnisight-data` volume. On first boot
the DB file is empty and each replica's lifespan runs
`PRAGMA journal_mode=WAL`, which requires an **exclusive** lock. Racing
backend-a vs backend-b → one gets `database is locked` and crashes in a
restart loop. **Fix**: `bootstrap_prod.sh` starts **backend-a alone**, waits
for alembic + `/readyz` ready (implies WAL established), then starts
backend-b. This only matters at first boot; subsequent starts re-use the
already-WAL-mode DB file.

### 2.9 Caddyfile — `email {$VAR:}` unset → parse error

Empty env var with empty default expanded to a bare `email` directive with
no argument → Caddy refused to parse. **Fix**:
`email {$OMNISIGHT_ACME_EMAIL:admin@omnisight.invalid}` — placeholder
default. Never actually submitted to ACME because the site block uses
`tls internal` (see 2.10).

### 2.10 Caddyfile — `issuer acme` + `issuer internal` catch-all TLS failure

Original: `tls { issuer acme {...}; issuer internal }`. On a `:443` catch-all
site (no hostname), the ACME issuer has nothing to validate against and
poisons the TLS handshake with `alert internal error 80` — even though
`issuer internal` is listed as the next fallback. **Fix**: `tls internal`
only. The CF Tunnel architecture terminates real TLS at the CF edge and
forwards HTTP to origin, so origin certs only need to parse, not to be
publicly trusted.

### 2.11 Caddyfile — `health_follow_redirects false` unrecognised

In Caddy 2 it's a **flag** directive (presence = on), not a bool-taking one.
`health_follow_redirects false` treats `false` as a second argument and
errors. Caddy defaults to not following redirects, so removing the line
preserves intent.

### 2.12 Caddyfile — `tls internal` won't mint certs for raw IP SNI

With a `:443` catch-all + `tls internal`, Caddy had no declared hostnames to
pre-issue for. curl sending `SNI=127.0.0.1` got `alert internal error`.
**Fix**: make the site multi-name —
`{$OMNISIGHT_PUBLIC_HOSTNAME:localhost}, 127.0.0.1 { ... }`. Caddy now
pre-issues certs for both names; smoke test connects via
`--resolve localhost:443:127.0.0.1`.

### 2.13 docker-compose healthchecks — busybox wget quirks

Two separate bugs conflated:
- **busybox wget rejects `--tries=` / `--timeout=`** (only accepts `-T SEC`
  and no retry option). Wrong flags were silently parsed as URLs, so wget
  "connected" to a garbage URL and exit code was unpredictable. Fix: use
  `-T SEC` only.
- **busybox wget prefers IPv6** when `localhost` resolves to both `::1`
  and `127.0.0.1`. Next.js standalone binds IPv4 only, so `localhost:3000`
  tried `[::1]:3000` → `Connection refused`. Fix: use `127.0.0.1` literal
  in the healthcheck URL, not `localhost`.

### 2.14 Next.js standalone — binds to `$HOSTNAME`, not `0.0.0.0`

`server.js` (from the standalone build) binds to `process.env.HOSTNAME`.
Docker sets `HOSTNAME=<container-id-short>`, which resolves to the
container's private network IP (e.g. 172.19.0.5) — **not loopback**. So
healthchecks inside the container could never reach it via `localhost` or
`127.0.0.1`. **Fix**: `Environment: HOSTNAME=0.0.0.0` in the compose
frontend service.

---

## 3. Files changed

| File | Type | Notes |
|---|---|---|
| `app/workspace/[type]/types.ts` | **new** | pure constants/types, breaks client-taint cycle |
| `app/workspace/[type]/layout.tsx` | edit | import + re-export from `./types` |
| `app/api/workspace/[type]/session/route.ts` | edit | import from `./types` |
| `components/omnisight/*.tsx` (6 files) | edit | import from `./types` |
| `hooks/use-workspace-persistence.ts` | edit | import from `./types` |
| `deploy/reverse-proxy/Caddyfile` | edit | 4 fixes per §2.9–§2.12 |
| `docker-compose.prod.yml` | edit | healthchecks + frontend HOSTNAME |
| `scripts/bootstrap_prod.sh` | **new** | Path B first-boot automation |
| `docs/ops/deploy_postmortem_2026-04-19.md` | **new** | this file |

Live-only (not committed, `.env*` is gitignored):
- `.env` — merged consolidation with real secrets
- `.env.bak-20260419-050023` + `.env.secrets.bak-20260419-050023` — backups
- Salvage branch `backup/deploy-attempts-20260419` (sha `78e57b7`) holds
  the systemd-path artifacts; safe to delete once this postmortem is reviewed.

---

## 4. How to re-run (idempotent)

```bash
# 1. Ensure .env has: OMNISIGHT_LLM_PROVIDER, OMNISIGHT_ANTHROPIC_API_KEY,
#    ANTHROPIC_API_KEY (unprefixed alias), OMNISIGHT_ADMIN_EMAIL,
#    OMNISIGHT_ADMIN_PASSWORD (≥ 12 chars, NOT "omnisight-admin"),
#    OMNISIGHT_DECISION_BEARER, OMNISIGHT_COOKIE_SECURE=true,
#    OMNISIGHT_FRONTEND_ORIGIN. See `.env.example` for full list.
# 2. First-ever boot:
scripts/bootstrap_prod.sh --yes

# 3. If a previous attempt left a dirty DB volume:
scripts/bootstrap_prod.sh --yes --fresh

# 4. Skip re-building images when iterating on the script:
scripts/bootstrap_prod.sh --yes --skip-build

# 5. Subsequent *upgrades* (NOT re-bootstrap) use the rolling-restart script:
scripts/deploy-prod.sh
```

---

## 5. Known limitations & follow-ups

1. **Redis not wired.** Backend runs `OMNISIGHT_WORKERS=1` per replica to
   stay safe without shared state. To enable cross-worker SSE + global
   rate-limiter, attach backend-a/b to `omnisight-ai-core_omnisight_net`
   via a compose override and set `OMNISIGHT_REDIS_URL=redis://ai_cache:6379/0`.

2. **Cloudflare Tunnel ingress not yet configured for sora-dev.app.** The
   existing `ai_tunnel` container routes other services; add an ingress
   rule for `sora-dev.app → http://localhost:80` (points at Caddy's :80
   which 301s to :443). See `docs/operations/cloudflare_tunnel_wizard.md`.

3. **`:443` TLS for raw IP SNI still fails.** Caddy's internal CA doesn't
   mint certs for `127.0.0.1` literal-SNI. Any smoke test must use
   `--resolve localhost:443:127.0.0.1 https://localhost/...`. Production
   traffic arrives via CF Tunnel with real hostname SNI, so this doesn't
   affect users — only local direct-IP testing.

4. **SQLite single-file + two replicas** (G2 limitation — see comment in
   `docker-compose.prod.yml`). Under sustained write contention, expect
   sporadic `database is locked` errors. G4 (HA-04) migrates to PostgreSQL
   and removes the constraint.

5. **Admin password rotation.** The `.env`-provided password is strong
   random but has been visible in at least one operator session. Rotate
   after first login via `/api/v1/auth/change-password` and remove
   `OMNISIGHT_ADMIN_PASSWORD` from `.env` once the new password is saved
   in the team password manager.
