# Failure Modes — Production Runbook (H5, 2026-04-19)

Pre-prod audit H5: the system handled several failure modes well
(network partition, DB connection loss, OOM via cgroup sampling) but
had **no documented procedure** for "disk full" or similar
infrastructure-tier failures. First-night-oncall without a runbook
means guessing — and guessing deletes backups at 3 am.

Each section is written for the person paged at 03:00 with no context.
Keep commands copy-paste-able. Cross-link to the relevant metric /
alert so the operator knows *why* they're here before executing.

---

## 1. Disk full (SQLite WAL cannot grow)

**Symptoms**
* Writes timeout; `/readyz` returns 503 intermittently; queue backs up.
* `OmniSightBackendInstanceDown` alert fires intermittently as
  lifespan retries fail to checkpoint the WAL.
* Log line: `[db] wal_checkpoint(RESTART) failed: disk full`.

**Triage (≤ 2 min)**

```bash
# 1. Confirm disk full on the data volume.
df -h /home/$USER/work/sora/OmniSight-Productizer/data
df -h /   # root fs too — sometimes /tmp fills from scratch files

# 2. Identify the eater.
du -sh /home/$USER/work/sora/OmniSight-Productizer/data/*
du -sh /home/$USER/work/sora/OmniSight-Productizer/.artifacts/*
du -sh /home/$USER/work/sora/OmniSight-Productizer/workspaces/*
ls -lt data/backups/ | head -20   # backups often dominate
```

**Fix (preserves data)**

```bash
# A. Remove backups older than 7 days (always safe — we keep daily
#    via scripts/deploy.sh + the upcoming hourly cron).
find data/backups -name "*.db" -mtime +7 -delete

# B. Checkpoint + VACUUM the main DB to reclaim free pages.
sudo systemctl stop omnisight-backend   # brief downtime
sqlite3 data/omnisight.db 'PRAGMA wal_checkpoint(RESTART);'
sqlite3 data/omnisight.db 'VACUUM;'
sudo systemctl start omnisight-backend

# C. Rotate the /tmp scratch dir if `PrivateTmp=yes` filled up.
sudo journalctl --vacuum-size=500M
```

**Destructive fallback** (only if A+B+C don't free enough):

```bash
# D. Drop old audit_log rows past the retention window. The merkle
#    chain does not re-anchor, so exports tagged after this point
#    break offline verification — communicate before running.
sqlite3 data/omnisight.db \
  "DELETE FROM audit_log WHERE created_at < datetime('now', '-90 days');"
sqlite3 data/omnisight.db 'VACUUM;'
```

**Prevention**
* `/var` + `data/` → separate volume ≥ 50 GB.
* Prometheus alert (pending — tracked as H4 follow-up):
  ```yaml
  - alert: OmniSightDiskUsageHigh
    expr: node_filesystem_avail_bytes{mountpoint="/"} /
          node_filesystem_size_bytes{mountpoint="/"} < 0.10
    for: 5m
  ```
* Hourly backup rotation cron (tracked as audit M8).

---

## 2. Redis unreachable (shared state offline)

**Symptoms**
* Worker heartbeats silently skip; `omnisight_workers_active` = 0
  but workers are actually running.
* SSE fan-out stops for cross-replica events (same-replica still OK).
* `shared_state` bare-except blocks swallow the connection error —
  visible only at DEBUG log level.

**Triage**

```bash
redis-cli -u "$OMNISIGHT_REDIS_URL" ping   # should return PONG
redis-cli -u "$OMNISIGHT_REDIS_URL" info replication | head -20
```

**Fix**
* If Redis is down: `sudo systemctl restart redis-server`.
* If the URL changed: update `OMNISIGHT_REDIS_URL` in `.env`, reload
  systemd (`sudo systemctl daemon-reload && sudo systemctl restart
  omnisight-backend omnisight-worker@*`).

**Prevention**
* `shared_state` degrades gracefully to in-memory per-replica state
  — production stays up on Redis outage, but SSE + cross-replica
  dist-lock are disabled until Redis returns.
* Follow-up: `omnisight_redis_up` gauge so the bare-except fallback
  is observable (tracked as audit Medium).

---

## 3. Cloudflared tunnel down (external access lost)

**Symptoms**
* External users report "ERR_CONNECTION_TIMED_OUT" or Cloudflare's
  "530" error.
* `sudo systemctl status cloudflared` shows failed or restarting.
* Internal access (`curl http://127.0.0.1:8000/readyz`) works.

**Triage**

```bash
sudo journalctl -u cloudflared --since "10 min ago" | tail -100
# Common errors:
#   * "Quic: connection closed" — renegotiate, harmless
#   * "failed to serve tunnel" — credential rotation, see below
#   * "no route to host" — CF edge unreachable; check CF status page
```

**Fix**

```bash
# A. Transient disconnect: just restart.
sudo systemctl restart cloudflared

# B. Credential rotation (tunnel deleted from CF dashboard):
cloudflared tunnel login   # browser
cloudflared tunnel create omnisight
cloudflared tunnel route dns omnisight <hostname>
sudo systemctl restart cloudflared

# C. CF edge outage: check https://www.cloudflarestatus.com/
#    No local fix. Document in incident timeline; consider
#    temporarily exposing via direct IP + nginx if critical.
```

**Prevention**
* Dedicated `OmniSightCloudflaredDown` alert — not wired yet; add in
  the observability runbook follow-up row.
* Retention: keep `cloudflared.service` restart count low via
  `StartLimitBurst=5 / StartLimitIntervalSec=300`.

---

## 4. Stuck migration (alembic upgrade head never returns)

**Symptoms**
* `sudo systemctl start omnisight-backend` hangs with
  `alembic.runtime.migration.context` log lines but no progress.
* `OmniSightMigrationMismatch` alert (H2) fires after 5 min.
* `sqlite3 data/omnisight.db "SELECT * FROM alembic_version;"` shows
  the prior revision.

**Triage**

```bash
# 1. What migration is running?
sudo journalctl -u omnisight-backend -n 100 --since "5 min ago" | grep alembic

# 2. Is a long-running query holding a lock?
#    (SQLite is single-writer; aiosqlite won't show locks directly —
#    use fuser on the WAL file.)
sudo fuser /home/$USER/work/sora/OmniSight-Productizer/data/omnisight.db-wal
```

**Fix**

```bash
# A. Cancel the upgrade safely (SQLite txns roll back cleanly).
sudo systemctl stop omnisight-backend

# B. Inspect current state.
sqlite3 data/omnisight.db "SELECT version_num FROM alembic_version;"
ls backend/alembic/versions/ | tail -5

# C. If alembic_version is ahead of on-disk files (someone force-set
#    it during a rollback), coordinate with the DB owner — do NOT
#    try `alembic downgrade` on SQLite without a backup, since
#    downgrades are lossy (see docs/ops/db_failover.md).

# D. If stuck on a large CREATE INDEX: for SQLite the command is
#    atomic; let it finish or cancel. For Postgres hosts (G4 HA),
#    use CREATE INDEX CONCURRENTLY + psql pg_cancel_backend.
```

**Prevention**
* Never land a migration that does multi-minute work on the hot
  table without a staging dress rehearsal.
* For Postgres HA hosts, always use `CONCURRENTLY` + `IF NOT EXISTS`.
* `docs/ops/database_migration_policy.md` (pending — M follow-up)
  pins these rules.

---

## 5. LLM provider 401 / 429 / 5xx cascade

**Symptoms**
* Tasks start failing with `ProviderError` in audit log.
* `omnisight_llm_provider_fallback_total` rises sharply.
* Users report "agent never finishes".

**Triage**

```bash
# 1. Which provider?
sudo journalctl -u omnisight-backend --since "5 min ago" | \
  grep -E "provider=(anthropic|openai|google|ollama).*401|429|5"

# 2. Deep-check (C3 audit — requires OMNISIGHT_READYZ_DEEP_CHECK=1):
curl -sS http://127.0.0.1:8000/readyz | jq .checks.provider_chain
```

**Fix**

```bash
# A. Temporary: flip the fallback chain order. 'ollama' last or
#    first depending on whether local inference is acceptable.
OMNISIGHT_LLM_FALLBACK_CHAIN=ollama,anthropic,openai
sudo systemctl daemon-reload
sudo systemctl restart omnisight-backend

# B. Quota exhausted (429): rotate to the secondary key or provider
#    until the quota window resets.

# C. Permanent key compromise: use scripts/codesign_manage.py to
#    rotate the stored API key, then restart.
```

**Prevention**
* `OMNISIGHT_READYZ_DEEP_CHECK=1` (C3) in production surfaces
  rotated/revoked keys within 60 s of readyz probe cadence.
* Fallback chain ordered by cost/reliability — have `ollama` as the
  zero-config tail so the system never fully fails LLM-wise.

---

## 6. Host CPU / memory exhaustion

**Symptoms**
* Host load > 80 % sustained for 5 min.
* `omnisight_tenant_cpu_percent` gauge shows one tenant near 100 %.
* Task queue grows; workers slow.

**Triage**

```bash
# Per-sandbox view.
curl -sS http://127.0.0.1:8000/api/v1/tenants/metrics | jq .

# System view.
top -b -n 1 -o %CPU | head -20
systemd-cgtop --iterations=1 --order=cpu | head -20
```

**Fix**
* DRF (Dominant Resource Fairness) will auto-throttle the noisy
  tenant after `omnisight_tenant_dominant_resource` breach — verify
  this is happening (M6 fairness is built in).
* Scale workers down (`sudo systemctl stop omnisight-worker@3
  omnisight-worker@4`) temporarily to let the queue drain.

**Prevention**
* Per-tenant CPU cap via cgroup (I6 — see
  `docs/ops/orchestration_selection.md`).
* Alert when `omnisight_host_load_1m > 0.8 * cpu_count` for 5 min
  (not yet wired).

---

## 7. General triage order

When multiple things go wrong at once, use this priority:

1. **Data integrity first**: is the DB reachable + not corrupt?
   (`sqlite3 data/omnisight.db 'PRAGMA quick_check;'`)
2. **Backend availability**: can a single replica serve /readyz?
3. **External access**: is cloudflared delivering?
4. **Queue drain**: are workers making progress on the backlog?
5. **LLM provider**: is the fallback chain healthy?

Always preserve a backup **before** running destructive commands
(section 1 step D, section 4 step D). `sqlite3 data/omnisight.db
".backup 'data/backups/incident-$(date +%Y%m%d-%H%M%S).db'"` takes
2 seconds and has saved more bacon than any alert ever did.

---

## 8. After any incident

1. Write up a 5-line timeline in an incident Slack channel.
2. Update this file with the new failure mode + procedure if a gap
   was exposed.
3. File an "observability gap" row in TODO.md if the alert didn't
   fire or fired late.
4. Consider whether the SLO budget (docs/ops/slo.md §3) was burned.
