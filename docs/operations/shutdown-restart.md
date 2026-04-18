# Safe shutdown & restart

Operator runbook for `scripts/shutdown.sh` and `scripts/restart.sh`.
Covers the drain semantics, ordering invariants, failure recovery, and
troubleshooting for both deployment topologies OmniSight supports on a
single host.

> **Scope:** this document covers only the long-running services the
> system itself spawns. `auto-runner.py` at the repo root is a personal
> scheduling helper and is **not** part of the system — neither script
> touches it.

## 1. Inventory — what actually runs

### 1.1 systemd topology (single-replica prod / WSL install)

| Unit | Process | Grace period | Drain behaviour |
|------|---------|--------------|-----------------|
| `omnisight-backend.service`     | `uvicorn backend.main:app` | `TimeoutStopSec=40` | `backend/lifecycle.py` — gate flips to 503, SSE subscribers flushed, wait ≤ 30 s for in-flight, SQLite WAL checkpoint + close |
| `omnisight-worker@N.service`    | `backend.worker run` (per-instance) | `TimeoutStopSec=60` | `backend/worker.py:734-806` — `_stop_event.set()`, drain in-flight, release file-path dist-locks, deregister from Redis `workers:active` |
| `omnisight-frontend.service`    | `npx next start -p 3000` | `TimeoutStopSec=15` | None — Next.js is stateless |
| `cloudflared.service`           | `cloudflared tunnel run` | (default) | None — tunnel drops immediately |

### 1.2 docker-compose prod topology (HA dual-replica)

| Service | Port | Healthcheck | Drain behaviour |
|---------|------|-------------|-----------------|
| `backend-a`    | 8000 | `/readyz` | same as systemd backend (30 s in-flight drain) |
| `backend-b`    | 8001 | `/readyz` | same as systemd backend |
| `caddy`        | 80/443 | `/` on :80 | reverse proxy — ejects unhealthy replicas via `health_uri /readyz` |
| `frontend`     | 3000 | `/` | none |
| `prometheus`   | 9090 | — | `--profile observability` only |
| `grafana`      | 3001 | — | `--profile observability` only |

### 1.3 docker-compose dev topology

| Service | Port | Drain |
|---------|------|-------|
| `backend`   | 8000 | same as prod |
| `frontend`  | 3000 | none |
| `worker`    | —    | `stop_signal: SIGTERM`, `stop_grace_period: 60s` (via `--profile workers`) |

## 2. Shutdown — `scripts/shutdown.sh`

### 2.1 Default flow (auto mode)

The script prefers systemd when `omnisight-backend.service` is
installed, otherwise falls back to docker-compose. Override with
`--mode systemd|compose`.

```
scripts/shutdown.sh                     # auto-detect, no DB backup, stop everything
scripts/shutdown.sh --backup-db         # add a WAL-safe sqlite3 .backup before stopping the backend
scripts/shutdown.sh --skip-ingress      # leave cloudflared/caddy running (used by rolling restart)
scripts/shutdown.sh --dry-run           # print the plan, change nothing
scripts/shutdown.sh --mode compose --compose-file docker-compose.staging.yml
```

### 2.2 Ordering invariants

Shutdown is the reverse of startup. The invariants are:

1. **Ingress stops first** (cloudflared / caddy) — no new external
   traffic is accepted while in-flight requests drain.
2. **Frontend stops before backend** — any SSR request in flight from
   the Next.js process is resolved before the backend goes away.
3. **Backend drain completes before workers stop** — a request the
   backend delegated to the queue must still have a worker to pick it
   up; killing workers first would orphan the request.
4. **DB backup runs before the backend stops** — the WAL is hot, so
   `sqlite3 .backup` produces a consistent snapshot.

### 2.3 Exit codes

| Code | Meaning | Next step |
|------|---------|-----------|
| 0 | All services down. | Done. |
| 1 | A service exceeded its grace period. | Check `journalctl -u <unit>` / `docker compose logs`. |
| 2 | Prerequisite missing. | Install `systemctl` or `docker`. |
| 3 | Invalid CLI arguments. | `scripts/shutdown.sh --help`. |

### 2.4 What happens under the hood

- `systemctl stop <unit>` sends `SIGTERM`, waits `TimeoutStopSec`, then
  escalates to `SIGKILL`.
- `docker compose stop -t <N>` sends `SIGTERM`, waits `N` seconds, then
  `SIGKILL`. The script passes 40 s to backends and 60 s to workers to
  match their drain budgets.
- The backend's signal handler lives in
  `backend/lifecycle.py:109-147`; the worker's in
  `backend/worker.py:787-806`. Both are idempotent — a duplicate signal
  from Ctrl+C + `systemctl stop` is harmless.

## 3. Restart — `scripts/restart.sh`

### 3.1 Mode matrix

| Mode | When to use | Downtime |
|------|-------------|----------|
| `systemd`    | single-replica host; config or code change | full (≤ 2 min) |
| `compose`    | dev or prod docker-compose stack, full cycle | full (≤ 2 min) |
| `rolling`    | prod HA dual-replica (`backend-a` + `backend-b` + `caddy`) | **zero** (Caddy routes to surviving replica) |
| `blue-green` | major dependency upgrade; needs standby primed | seconds (symlink flip) |

`auto` resolves to `systemd` if the backend unit is installed, else
`compose`. `rolling` / `blue-green` must be explicit.

### 3.2 Default flow

```
scripts/restart.sh                          # auto-detect, backs up DB first
scripts/restart.sh --skip-backup            # skip DB backup (e.g. already backed up manually)
scripts/restart.sh --mode rolling --env prod      # HA rolling — zero downtime
scripts/restart.sh --mode blue-green --env prod   # blue-green cutover
scripts/restart.sh --dry-run                # print plan
```

`--mode rolling` and `--mode blue-green` `exec` into
`scripts/deploy.sh --strategy <mode>` — that path is already covered by
the G2/G3 test suite. `restart.sh` doesn't reimplement it.

### 3.3 Start order (mirror of shutdown)

1. **Backend first** — poll `http://127.0.0.1:8000/readyz` until 200
   (timeout `--timeout`, default 90 s).
2. **Workers** — only the `omnisight-worker@N` units marked `enabled`
   in `systemctl list-unit-files` are restarted. Running-but-disabled
   instances are treated as operator experiments and left alone.
3. **Frontend** — poll `http://127.0.0.1:3000/` for any 2xx.
4. **Cloudflared last** — the tunnel opens only after the app is
   verified ready, so external clients never hit a partially-booted
   backend.

### 3.4 Readiness polling

Uses `curl -sSf` against `/readyz`. A 5xx, connection-refused, or
unresolved DNS is treated as "not ready yet" and retried every 2 s
until the timeout elapses. On timeout the script exits 4 so a CI
pipeline can distinguish "start failed" (1) from "start succeeded but
health probe did not confirm" (4).

### 3.5 Exit codes

| Code | Meaning |
|------|---------|
| 0 | Services up, health probes green. |
| 1 | Shutdown or start step failed. |
| 2 | Prerequisite missing. |
| 3 | Invalid args. |
| 4 | Readiness poll timed out. |

## 4. Recovery

### 4.1 Backend stuck draining past its grace period

`systemctl stop omnisight-backend` will SIGKILL at 40 s. The startup
cleanup in `backend/main.py:13-41` detects and resets:

- Agents with `status='running'` older than 1 h → `status='idle'`.
- Simulations with `status='running'` → `status='error'`.
- Orphaned Docker containers → cleanup.
- Stale git lock files → cleanup.
- Workflow runs left in-flight → surfaced via `/workflow/in-flight`.

So a hard-killed backend is **recoverable on next start**, but
**in-flight HTTP requests are dropped** (no 503 sent). This is the
cost of force-kill; avoid it in prod.

### 4.2 Worker stuck past its 60 s grace period

`backend/worker.py:734-786` abandons each in-flight task with
`_abandon(mid, tid, "worker stop timeout")`. The message's visibility
timeout (default 5 min, `backend/worker.py:82`) makes it re-deliverable
to another worker. Dist-locks auto-expire after 30 min
(`DEFAULT_LOCK_TTL_S`). No manual cleanup is needed.

### 4.3 Readiness probe never goes green

Symptoms: `restart.sh` exits 4 after 90 s of polling `/readyz`.

Check:

1. `journalctl -u omnisight-backend -n 200` (systemd) or
   `docker compose logs backend` (compose) — look for
   `ConfigValidationError` from `validate_startup_config()`.
2. `backend/main.py:56-61` raises on missing critical env
   (`OMNISIGHT_DECISION_BEARER`, provider API keys) in prod mode.
3. `/readyz` checks DB + Redis (when configured). A failed Redis means
   `/readyz` returns 503 even though the process is running. See
   `backend/routers/health.py`.

### 4.4 Rolling restart aborts mid-flight

`scripts/deploy.sh --strategy rolling` stops `backend-a`, recreates it,
polls `/readyz`, then does the same for `backend-b`. If the first
replica never comes back, Caddy continues routing 100% of traffic to
the surviving replica — no user-visible outage. Inspect logs for the
failed replica and re-run `scripts/deploy.sh --strategy rolling` once
the issue is fixed.

### 4.5 Blue-green post-cutover failure

`scripts/deploy.sh --strategy blue-green` observes `/readyz` for 5
minutes after the symlink flip. If the new color fails that window, it
exits 7; the operator runs:

```
scripts/deploy.sh --rollback
```

which does a single `rename(2)` symlink flip back to the previous
color (kept warm for 24 h). Documented at length in
`scripts/deploy.sh:55-69`.

## 5. Troubleshooting

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| `shutdown.sh` hangs 40 s on backend | long-running request not respecting cancellation | check for synchronous DB queries; shorten HTTP timeouts upstream |
| Worker shows up as still-registered after stop | process was SIGKILLed before deregister | Redis TTL on `workers:active` is 90 s; entry self-expires |
| `/readyz` returns 200 but `/livez` fails | liveness check sees stuck event loop | inspect `backend/routers/health.py`; likely a blocking call in an async handler |
| `sudo` prompts during shutdown | non-root operator | allow passwordless `systemctl stop omnisight-*` via `sudoers.d/omnisight`, or run as root |
| compose shutdown leaves `prometheus` running | observability profile not enabled when stopping | `docker compose -f … --profile observability stop prometheus grafana` |

## 6. Validation checklist

Before relying on this flow in prod, confirm:

- [ ] `scripts/shutdown.sh --dry-run` prints the expected command list.
- [ ] `scripts/restart.sh --dry-run` prints start+stop+poll plan.
- [ ] `shellcheck scripts/shutdown.sh scripts/restart.sh` clean.
- [ ] `curl -sSf http://127.0.0.1:8000/readyz` returns 200 after
      `scripts/restart.sh`.
- [ ] DB backups land in `data/backups/` with the `shutdown-*.db` /
      `restart-*.db` prefix.
- [ ] After a forced shutdown (`kill -9`), `/workflow/in-flight`
      surfaces the interrupted runs and `agents` stuck in `running`
      get reset on next start (audit `backend/main.py:13-41`).

## 7. Related

- `docs/operations/deployment.md` — full install / first-boot guide.
- `docs/ops/blue_green_runbook.md` — blue-green cutover deep dive.
- `scripts/deploy.sh` — rolling + blue-green + rollback primitives.
- `backend/lifecycle.py` — drain coordinator implementation.
- `backend/worker.py:734-806` — worker graceful shutdown.
