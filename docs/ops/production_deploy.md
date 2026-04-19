# Production Deploy — Go-Live Runbook

**Prerequisites**: all 4 **Critical** (`C1-C4`) + 7 **High** (`H1-H7`)
pre-prod audit blockers merged to `master` and CI green. This
runbook assumes the latest commits are on origin/master and the new
`backend-coverage-combine` CI job is passing.

Cross-references:
* SLO / alert thresholds → [`docs/ops/slo.md`](./slo.md)
* Failure-mode runbooks → [`docs/ops/failure_modes.md`](./failure_modes.md)
* Blue-green ceremony → [`docs/ops/blue_green_runbook.md`](./blue_green_runbook.md)
* DR RTO/RPO → [`docs/ops/dr_rto_rpo.md`](./dr_rto_rpo.md)
* Shutdown / restart scripts → [`docs/operations/shutdown-restart.md`](../operations/shutdown-restart.md)
* Initial install (first-ever deploy) → [`docs/operations/deployment.md`](../operations/deployment.md)

---

## 0. Go / No-Go Gate

Before starting — verify on the deploy host:

```bash
cd /home/$USER/work/sora/OmniSight-Productizer
git fetch origin
git log origin/master..HEAD --oneline    # should be empty (in sync)
git log --oneline -5 origin/master        # last 5 commits visible
```

**STOP if any of these are true:**

- [ ] CI on the last master commit is red
- [ ] Any of the four Critical commits (C1–C4) is missing: `26c4453` (C1), `b38eb1b` (C2), `9a08e49` (C3), `dd011c1` (C4)
- [ ] `backend-coverage-combine` job has never passed with aggregate ≥ 60%
- [ ] `OMNISIGHT_ADMIN_PASSWORD` / `OMNISIGHT_DECISION_BEARER` not prepared (see §1)
- [ ] Backup of current production DB not taken (see §2)

---

## 1. Environment Variables (required before boot)

Create `/home/$USER/work/sora/OmniSight-Productizer/.env` from
`.env.example` and set **all** of the following. `backend/config.py`
strict mode (which is now the default in production per C1 + H1)
refuses to start if any is missing:

```bash
# ── Required (strict-mode hard errors) ──
OMNISIGHT_ENV=production                      # triggers ENV=production + strict gate
OMNISIGHT_AUTH_MODE=strict                    # session + admin required
OMNISIGHT_ADMIN_PASSWORD=<rotate-me>          # C1 — must be strong, ≥ 12 chars, not "omnisight-admin"
OMNISIGHT_DECISION_BEARER=<rotate-me>         # H1 — ≥ 16 chars random secret
OMNISIGHT_COOKIE_SECURE=true                  # HTTPS via Cloudflare Tunnel

# ── LLM provider (one of) ──
OMNISIGHT_LLM_PROVIDER=anthropic              # or openai / google / ollama / etc.
ANTHROPIC_API_KEY=sk-ant-...                  # matching provider

# ── Optional but recommended ──
OMNISIGHT_REDIS_URL=redis://127.0.0.1:6379/0  # required for multi-worker
OMNISIGHT_WORKERS=4                           # uvicorn worker count
OMNISIGHT_DATABASE_PATH=/home/$USER/work/sora/OmniSight-Productizer/data/omnisight.db
OMNISIGHT_READYZ_DEEP_CHECK=1                 # C3 — real provider connectivity, opt-in

# ── MUST BE ABSENT / false in production ──
# OMNISIGHT_DEBUG — leave unset; setting true relaxes strict gate
# OMNISIGHT_CI_MODE — leave unset; CI-only bypass per H7
```

Generate secrets:

```bash
# Strong random values, URL-safe base64
python3 -c "import secrets; print('pw:', secrets.token_urlsafe(24))"
python3 -c "import secrets; print('bearer:', secrets.token_urlsafe(32))"
```

Store in your password manager **before** writing to `.env`.

Validate without starting the server:

```bash
cd /home/$USER/work/sora/OmniSight-Productizer
set -a && source .env && set +a
python3 -c "
from backend.config import validate_startup_config
warnings = validate_startup_config()
print(f'OK — {len(warnings)} warning(s), 0 hard errors')
for w in warnings: print(' ·', w)
"
```

If this raises `ConfigValidationError`, fix the reported vars before continuing.

---

## 2. Pre-Deploy Backup

**Never skip this.** Takes < 2 s; saves bacon in every rollback.

```bash
cd /home/$USER/work/sora/OmniSight-Productizer
mkdir -p data/backups
sqlite3 data/omnisight.db \
  ".backup 'data/backups/pre-deploy-$(date +%Y%m%d-%H%M%S).db'"
ls -lh data/backups/pre-deploy-*.db | tail -3
```

Verify the backup opens cleanly:

```bash
sqlite3 data/backups/pre-deploy-*.db "PRAGMA quick_check;"   # → "ok"
```

---

## 3. Infrastructure Prerequisites

### 3.1 DNS + Cloudflare Tunnel

If not already wired (first-time only):

```bash
# Follow docs/operations/deployment.md §1-§4 for:
#   - Cloudflare Zone + DNS delegation
#   - cloudflared tunnel create omnisight
#   - Tunnel route DNS to your hostname
#   - systemd unit for cloudflared
```

Verify tunnel is up:

```bash
sudo systemctl status cloudflared --no-pager
curl -sS https://<your-hostname>/healthz      # should return {"status":"ok"}
```

### 3.2 Redis (for HA / multi-worker)

```bash
sudo systemctl status redis-server --no-pager
redis-cli -u "$OMNISIGHT_REDIS_URL" ping      # should return PONG
```

If Redis is absent, you can still deploy single-worker — but SSE
fan-out across replicas and cross-worker dist-locks won't work.

### 3.3 Database migrations

**Always run before starting the backend.** H2 adds a Prometheus
alert that fires if migrations are out of sync, but prevention > cure:

```bash
cd /home/$USER/work/sora/OmniSight-Productizer
python3 -m alembic -c backend/alembic.ini upgrade head
python3 -c "
import sqlite3, os
conn = sqlite3.connect(os.environ['OMNISIGHT_DATABASE_PATH'])
print('current:', conn.execute('SELECT version_num FROM alembic_version').fetchone())
"
```

Expected output: the latest revision matches the filename prefix of
the newest `backend/alembic/versions/*.py`.

---

## 4. Deploy

> **IMPORTANT — decide first-time vs subsequent deploy.**
>
> `scripts/deploy.sh` is a **re-deploy / upgrade** tool. Its three
> strategies (`systemd` / `rolling` / `blue-green`) all assume the
> services / containers / blue-green state directory **already exist**.
>
> - **First-time** (services never booted on this host) → §4.1 or §4.2
>   (one-time bootstrap).
> - **Subsequent deploys** → §4.3 (`scripts/deploy.sh`).
>
> After the first bootstrap, record the host in the change log (§10)
> so the next operator knows the bootstrap is done and can go straight
> to §4.3.

### 4.1 First-time — Path A: systemd (single-host, recommended)

Run these **in order** on a clean host. Each block is idempotent
enough that re-running after a mid-failure resume is safe (the
`enable --now` and `systemctl status` gates report existing state
instead of erroring). Total time: ~10 min on a warm host, ~25 min
on a cold one.

```bash
cd /home/$USER/work/sora/OmniSight-Productizer

# ─── (a) Backend deps + frontend build ────────────────────────
pip install --require-hashes -r backend/requirements.txt
pnpm install --frozen-lockfile --prefer-offline
pnpm run build

# ─── (b) Initialise the SQLite database ───────────────────────
mkdir -p data data/backups
python3 -m alembic -c backend/alembic.ini upgrade head
sqlite3 data/omnisight.db "SELECT version_num FROM alembic_version;"
# Expected: the revision matching the newest file prefix under
#   backend/alembic/versions/*.py

# ─── (c) Install systemd unit files (one-time) ────────────────
# Edit each file first: replace `USER_HOME` / `USERNAME` placeholders
# with the actual runtime user's $HOME path + username. Leave the
# rest (ExecStart, ReadWritePaths, KillSignal, TimeoutStopSec) alone —
# those are pinned to match backend/lifecycle.py + backend/worker.py.
sudo cp deploy/systemd/omnisight-backend.service  /etc/systemd/system/
sudo cp deploy/systemd/omnisight-worker@.service  /etc/systemd/system/
sudo cp deploy/systemd/omnisight-frontend.service /etc/systemd/system/
sudo cp deploy/systemd/cloudflared.service        /etc/systemd/system/
sudo systemctl daemon-reload

# ─── (d) Start services (order matters — tunnel LAST) ─────────
# Backend first; wait up to 25s for lifespan (DB init + config
# validation + 11 background tasks + G1 signal handler). Cold-start
# with alembic + startup_cleanup can take 15-40s.
sudo systemctl enable --now omnisight-backend
for i in $(seq 1 30); do
  if curl -sSf http://127.0.0.1:8000/readyz >/dev/null 2>&1; then
    echo "backend ready after ${i}s"; break
  fi
  sleep 1
done
curl -sSf http://127.0.0.1:8000/readyz | jq .ready    # must be true

# Workers (scale @N per OMNISIGHT_WORKERS; 2-3 is typical).
sudo systemctl enable --now omnisight-worker@1
sudo systemctl enable --now omnisight-worker@2

# Frontend (Next.js production server).
sudo systemctl enable --now omnisight-frontend
sleep 5 && curl -sSf http://127.0.0.1:3000/ >/dev/null

# Cloudflare Tunnel LAST — opens the public URL only after the
# stack above is verified ready. If you swap this order you'll
# answer external 5xx for the tunnel-open → backend-ready window.
sudo systemctl enable --now cloudflared

# ─── (e) Verify the whole stack is live ───────────────────────
for unit in omnisight-backend omnisight-worker@1 omnisight-worker@2 \
            omnisight-frontend cloudflared; do
  echo "--- $unit ---"
  sudo systemctl is-active "$unit"
done
```

Record this bootstrap in §10 with the host + date.

### 4.2 First-time — Path B: docker-compose HA (dual-replica + Caddy)

Use this when the deploy target is a docker host and you want the
G2 rolling-restart topology from day 1. Bootstrap brings up both
`backend-a` + `backend-b` + Caddy so `scripts/deploy.sh --strategy
rolling` works for subsequent upgrades.

```bash
cd /home/$USER/work/sora/OmniSight-Productizer

# ─── (a) Pull the release image (GHCR) or build locally ───────
# `pull_policy: missing` in compose means it only pulls when absent;
# an explicit pull avoids the first-boot race where compose builds
# from the Dockerfile instead (slower + no reproducible tag).
docker compose -f docker-compose.prod.yml pull || \
  docker compose -f docker-compose.prod.yml build

# ─── (b) First-boot: brings up all services ───────────────────
# docker-compose.prod.yml orders:
#   backend-a + backend-b  (parallel, healthcheck /readyz)
#   caddy                  (depends_on both backends: service_healthy)
#   frontend               (depends_on caddy: service_healthy)
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps
# Expected: STATE=running, HEALTH=healthy on every row.

# ─── (c) First-time alembic upgrade inside the container ──────
# The compose-mounted volume `omnisight-data` persists the DB; alembic
# runs INSIDE backend-a so it sees the containerised /app/data path.
docker compose -f docker-compose.prod.yml exec -T backend-a \
  python3 -m alembic -c /app/backend/alembic.ini upgrade head

# ─── (d) Verify both replicas + Caddy fan-out ─────────────────
curl -sSf http://127.0.0.1:8000/readyz | jq .ready    # backend-a
curl -sSf http://127.0.0.1:8001/readyz | jq .ready    # backend-b
curl -sSf http://127.0.0.1:80/                        # Caddy → frontend
```

### 4.3 Subsequent deploys — `scripts/deploy.sh`

Once either bootstrap above is complete, use `scripts/deploy.sh` for
every future upgrade. Its three strategies correspond to the topology
you bootstrapped:

| Strategy | When to use | Command |
|----------|-------------|---------|
| `systemd` (default) | Path A bootstrap (single-host) | `scripts/deploy.sh prod <tag>` |
| `rolling` | Path B bootstrap (dual-replica) | `scripts/deploy.sh --strategy rolling prod <tag>` |
| `blue-green` | Path B + N10 blue-green gate triggered (major dep bump) | `scripts/deploy.sh --strategy blue-green prod <tag>` |

`scripts/deploy.sh` already handles:
* git fetch + checkout of the tag (§1 inside the script);
* WAL-safe SQLite backup via `sqlite3 .backup` (§2 inside);
* N10 blue-green gate for prod (§1b — refuses if the last-merged PR
  was labelled `requires-blue-green` but `systemd` strategy is
  chosen);
* `pip install --require-hashes` + `pnpm run build` before restart;
* Strategy-appropriate restart (systemctl restart / drain-recreate-
  readyz poll per replica / cutover ceremony);
* `/api/v1/health` smoke against every active port.

Rollback:

```bash
scripts/deploy.sh --rollback
# Blue-green path only. Flips the Caddy upstream symlink back to the
# previous color (kept warm for 24 h). <5 s to complete. See
# docs/ops/blue_green_runbook.md for the retention window semantics.
```

For a systemd rollback (Path A), checkout the previous tag and
re-run `scripts/deploy.sh prod <prev-tag>`; the lifespan teardown
honours C2 WAL-checkpoint + G1 graceful drain so the restart is
safe even without blue-green.

### 4.4 Optional — observability sidecars

```bash
# Path B only — Prometheus + Grafana under the `observability` profile.
docker compose -f docker-compose.prod.yml --profile observability up -d
# Prometheus at :9090, Grafana at :3001
# After first boot:
#   - Load deploy/observability/prometheus/alerts.yml as a rule file
#     (adds the 4 alerts: ReplicaLagHigh, RollingDeploy5xxRateHigh,
#      BackendInstanceDown, MigrationMismatch per H2 audit).
#   - Import deploy/observability/grafana/ha.json as a dashboard.
```

---

## 5. Post-Deploy Smoke Test

Run these in order. Each takes < 30 s. **Stop at the first failure
and rollback (§8).**

```bash
# 1. Liveness
curl -sSf https://<your-hostname>/livez        # 200, {"status":"alive"}

# 2. Readiness — deep check catches rotated keys
curl -sSf https://<your-hostname>/readyz | jq .
# Expected: "ready": true, every "ok": true in checks:
#   draining, db, migrations, provider_chain

# 3. Metrics exposed
curl -sSf https://<your-hostname>/metrics | head -30
# Should include omnisight_backend_instance_up, omnisight_readyz_*, ...

# 4. Auth flow (expects 428 the first time — you must rotate the admin password)
curl -sS -X POST https://<your-hostname>/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@omnisight.local","password":"'"$OMNISIGHT_ADMIN_PASSWORD"'"}' | jq .
# Expected: {"must_change_password": true, ...}

# 5. Rotate admin password (replaces the default-flag)
curl -sS -X POST https://<your-hostname>/api/v1/auth/change-password \
  -H 'Content-Type: application/json' \
  -H "Cookie: session=..." \
  -d '{"current_password":"'"$OMNISIGHT_ADMIN_PASSWORD"'",
       "new_password":"<chosen-final-password>"}' | jq .

# 6. Smoke-test script (comprehensive)
python3 scripts/prod_smoke_test.py --url https://<your-hostname>
# Generates a report at data/smoke-YYYYMMDD-HHMMSS.md
```

---

## 6. Monitoring (first 24 h)

### 6.1 Watch the four alerts (per H2 + pre-existing G7 alerts)

Grafana HA dashboard → Alert panel should show:

| Alert | Expected state | Trigger |
|---|---|---|
| `OmniSightBackendInstanceDown` | OK (both replicas 1) | replica down > 2 min |
| `OmniSightRollingDeploy5xxRateHigh` | OK (< 1%) | 5xx rate > 1% for 2 min |
| `OmniSightReplicaLagHigh` | OK (< 10 s) | Postgres replication lag |
| `OmniSightMigrationMismatch` | OK (0) | alembic head drift for 5 min |

### 6.2 SLO budget (per `docs/ops/slo.md`)

```bash
# Quick SLO burn check — p95 latency over last 5 min
curl -sS https://<your-hostname>/metrics | \
  grep -E 'readyz_latency_seconds|rolling_deploy_5xx_rate' | head -10
```

Targets:
* p50 ≤ 100 ms, p95 ≤ 500 ms, p99 ≤ 2 s
* 5xx rate < 0.5 %
* Availability 99.5% monthly (≤ 21.6 min budget)

### 6.3 Log tail

```bash
# systemd path
sudo journalctl -u omnisight-backend -f --since "1 min ago"

# docker path
docker compose -f docker-compose.prod.yml logs -f backend-a backend-b
```

Expect a clean log — the H3 `--no-access-log` fix removes the
`/readyz` probe noise from Caddy's 2 s polling.

---

## 7. Canary → Full Rollout

If the first deploy is a **version upgrade** (not first-ever
deploy), use blue-green for zero downtime:

```bash
scripts/deploy.sh --strategy blue-green prod <git-ref>
# See docs/ops/blue_green_runbook.md for the 5-min observation window,
# automatic rollback triggers, and 24 h retention semantics.
```

For a **first-ever launch**, the plain `systemctl enable --now` /
`docker compose up` path in §4 is already canary-like: traffic
starts at 0, only grows once Cloudflare Tunnel opens. Monitor for
~2 h before sharing the URL widely.

---

## 8. Rollback

### 8.1 Fast path — configuration-only issue

Undo the env change and restart:

```bash
# systemd
sudo systemctl restart omnisight-backend

# docker
docker compose -f docker-compose.prod.yml restart backend-a backend-b
```

### 8.2 Code rollback — blue-green flip

```bash
scripts/deploy.sh --rollback
# Flips Caddy back to the previous color (kept warm for 24 h).
# < 5 s to complete. See docs/ops/blue_green_runbook.md §4.
```

### 8.3 Database rollback — if migration was the cause

```bash
# STOP traffic first!
sudo systemctl stop omnisight-backend
# (or docker compose stop backend-a backend-b)

# Restore from pre-deploy backup taken in §2
cp data/backups/pre-deploy-YYYYMMDD-HHMMSS.db data/omnisight.db
sqlite3 data/omnisight.db "PRAGMA quick_check;"   # verify "ok"

# Re-deploy the previous code version BEFORE restarting:
git checkout <previous-tag>
sudo systemctl start omnisight-backend
```

**SQLite-specific warning**: alembic downgrades are lossy on SQLite
(see `docs/ops/failure_modes.md` §4). The backup restore above is the
ONLY reliable rollback path for schema-affecting changes on SQLite.

### 8.4 Total recovery — graceful shutdown

```bash
scripts/shutdown.sh --backup-db    # drains + checkpoints WAL (C2 fix)
# investigate...
scripts/restart.sh                 # brings everything back in order
```

---

## 9. Post-Launch Follow-Up (week 1)

- [ ] Confirm the `OmniSightMigrationMismatch` alert (H2) does NOT fire
- [ ] Confirm aggregate coverage in CI (`backend-coverage-combine`) stays ≥ 60%
- [ ] Verify the hourly DB backup cron is running (if the operator
      added it — it's a Medium-severity follow-up from the audit)
- [ ] Review the SLO budget burn (`docs/ops/slo.md` §3) after 7 days
- [ ] Scan `sudo journalctl -u omnisight-backend` for
      `WARN [lifecycle] drain timed out` — if present, the drain
      window may need tuning past 30 s (see
      `backend/lifecycle.py:DEFAULT_DRAIN_TIMEOUT_SECONDS`)

---

## 10. Change Log

| Date | Operator | Action | Commit/tag |
|---|---|---|---|
| 2026-04-19 | user | first-time bootstrap via Path B (`scripts/bootstrap_prod.sh`); see [post-mortem](./deploy_postmortem_2026-04-19.md) | cc55200 |
| 2026-04-19 | user | CF Tunnel ingress live: `ai.sora-dev.app` → `frontend:3000` (`docker compose --profile tunnel up -d cloudflared`) | HEAD |
| YYYY-MM-DD | | (fill in per deploy) | |
