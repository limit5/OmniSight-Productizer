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

## 4. Deploy (choose one path)

### 4.1 Path A — systemd (single-host, recommended for first launch)

```bash
# 1. Install (first time only)
sudo cp deploy/systemd/omnisight-backend.service /etc/systemd/system/
sudo cp deploy/systemd/omnisight-worker@.service /etc/systemd/system/
sudo cp deploy/systemd/omnisight-frontend.service /etc/systemd/system/
sudo cp deploy/systemd/cloudflared.service /etc/systemd/system/
# Edit placeholders (USER_HOME / USERNAME) in each file first.
sudo systemctl daemon-reload

# 2. Start — order matters: tunnel last so it opens only when ready
sudo systemctl enable --now omnisight-backend.service
# Wait ~10s for lifespan startup (DB init + validation + bg tasks)
sleep 10
curl -sSf http://127.0.0.1:8000/readyz | jq .    # must return 200 + ready:true

sudo systemctl enable --now omnisight-worker@1.service
sudo systemctl enable --now omnisight-worker@2.service

sudo systemctl enable --now omnisight-frontend.service
sleep 5
curl -sSf http://127.0.0.1:3000/                 # Next.js landing

sudo systemctl enable --now cloudflared.service  # opens public URL
```

### 4.2 Path B — docker-compose HA (dual-replica + Caddy)

```bash
cd /home/$USER/work/sora/OmniSight-Productizer

# Pull / build images
docker compose -f docker-compose.prod.yml pull || \
  docker compose -f docker-compose.prod.yml build

# Bring up with ordered health gating
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps    # all should be "healthy"

# Verify both replicas
curl -sSf http://127.0.0.1:8000/readyz | jq .status    # backend-a
curl -sSf http://127.0.0.1:8001/readyz | jq .status    # backend-b
curl -sSf http://127.0.0.1/                            # Caddy → frontend
```

### 4.3 Optional — observability sidecars

```bash
docker compose -f docker-compose.prod.yml --profile observability up -d
# Prometheus at :9090, Grafana at :3001
# Load deploy/observability/prometheus/alerts.yml into Prometheus
# Load deploy/observability/grafana/ha.json into Grafana
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
| YYYY-MM-DD | | (fill in per deploy) | |
