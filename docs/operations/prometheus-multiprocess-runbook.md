# Prometheus multiprocess exposition — activation runbook (OP-2562)

Status: capability shipped **DORMANT** (v0.7.x, OP-2562). Nothing changes at
runtime until an operator performs the activation below.

## Why this exists

The backend serves with raw `python -m uvicorn backend.main:app --workers N`
(prod: N=2). Each worker is a separate process with its own in-process
`CollectorRegistry`, and Prometheus scrapes whichever worker the kernel routes
the connection to. Every ABSOLUTE counter/gauge in `/api/v1/metrics` therefore
reflects a single worker → totals undercount by ~1/N (≈2× low at 2 workers).
Ratios computed from two metrics in the same scrape stay correct (same
worker), which is why the Phase S content-SLO alerts were never affected —
but raw `*_total` dashboards and absolute-count alerting are silently low.

OP-2562 wires the stock `prometheus_client` multiprocess mode, gated entirely
on the `PROMETHEUS_MULTIPROC_DIR` env var:

- env **unset** (default): behaviour and `/metrics` bytes are identical to
  the pre-OP-2562 single-registry render. The multiproc code path is never
  imported.
- env **set**: metric values are written to per-PID mmap db files under the
  dir; `render_exposition()` (backend/metrics.py) aggregates them across ALL
  workers via `MultiProcessCollector` on a fresh registry per scrape.

## ⚠ The import-timing invariant (read before activating)

**`PROMETHEUS_MULTIPROC_DIR` is a launch-time variable, not a runtime
toggle.** `prometheus_client` selects its value-storage backend ONCE, at
first import, keyed on the env var being present in the environment AT THAT
MOMENT. Setting the env for an already-running process (or injecting it
after uvicorn imported the app) makes the exposition switch flip but the
metric values never reach the per-PID db files — you would scrape an empty
or partial surface. The env MUST be present in the container environment
before the process starts; activate only via compose env + container
recreate.

## Activation (deploy-config change, not code)

1. In `docker-compose.prod.yml`, on **BOTH** `backend-a` and `backend-b`,
   set the env to a `/tmp`-class path, e.g.:

   ```yaml
   environment:
     PROMETHEUS_MULTIPROC_DIR: /tmp/prom-multiproc
   ```

   `/tmp` is already a tmpfs mount on both backend services — reuse it.
   **Never point the dir at a persistent volume**: stale per-PID files from
   a previous boot would silently OVERcount counters (trading the undercount
   for a new silent-degrade bug).

2. Mirror the container-start wipe into **backend-b's** compose `command:`
   override. backend-a already gets it via the `Dockerfile.backend` CMD
   prefix:

   ```sh
   [ -n "$PROMETHEUS_MULTIPROC_DIR" ] && { rm -rf "$PROMETHEUS_MULTIPROC_DIR"/* 2>/dev/null; mkdir -p "$PROMETHEUS_MULTIPROC_DIR"; };
   ```

   Prepend the same gated prefix to backend-b's command chain so a full
   container restart is always a clean slate on both replicas. (Strict
   no-op while the env is unset.) The `mkdir -p` is boot-safety, not
   cosmetics: `prometheus_client` does NOT create the dir and every worker
   crashes at import if it is missing once the env is set.

3. Recreate both backend containers (env must be present at process launch
   — see the invariant above). `docker compose up -d --force-recreate
   backend-a backend-b` on the release host, one at a time per the rolling
   deploy runbook.

4. **Prod scrape A/B check**: drive/observe a known request volume, scrape
   `/api/v1/metrics` repeatedly, and confirm `http_requests_total` is STABLE
   across consecutive scrapes and roughly equals the driven count — before
   activation it fluctuates between per-worker values (~half). Record the
   numbers on the activation ticket.

## Rollback

Remove the env from both services and recreate the containers. The code path
is dormant again and `/metrics` returns the single-registry render — the fix
is reversible by config alone.

## Known residual gap (accepted, bounded)

Worker-death cleanup runs from the FastAPI lifespan-shutdown hook
(`backend/main.py` → `metrics.multiprocess_worker_shutdown()`), which uvicorn
triggers on the SIGTERM it sends workers during reload/scale-down. A worker
that is **SIGKILLed (e.g. OOM) and respawned inside a living container**
never runs lifespan-shutdown and leaves one stale per-PID db file until the
next container restart wipes the dir → a bounded transient overcount for the
metrics that worker had touched. This is explicitly accepted (honest, not
silent); a full container restart always starts clean via the CMD wipe.

## Semantics after activation

- Counters and Histograms sum across workers natively. Histogram `_created`
  timestamps and exemplars are not multiprocess-exported (acceptable).
- Every Gauge declares an explicit `multiprocess_mode` (enforced by
  `backend/tests/test_metrics_multiproc.py`): point-in-time snapshot gauges
  use `livemostrecent` (a dead worker's last value is dropped), per-worker
  additive counts (`omnisight_sse_subscribers`, `omnisight_worker_inflight`)
  use `livesum`, and `omnisight_process_start_time_seconds` uses `min`.
- `process` and `platform` collectors (python_info, process_cpu, etc.) are
  not exported in multiprocess mode — expected upstream behaviour.
