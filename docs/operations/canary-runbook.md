# Canary Rollout Runbook — D10 (OP-882, re-pointed by OP-1694)

**Owner:** Deploy on-call · **Spec:** META OP-761 §Phase 3 · **Tickets:** OP-882, OP-1694

This runbook is the procedure for shifting production traffic onto a
new image via the staged 5% → 25% → 100% canary. It is invoked *after*
the D9 prod-deploy orchestrator (`backend/orchestrator/prod_deploy.py`)
has placed the new image behind the canary upstream — D10 only governs
the traffic shift.

> **OP-1694 — real SLO path.** This runbook drives the **real,
> SLO-gated** controller `backend.canary_rollout.CanaryController` with
> `backend.canary_rollout.RollingDeploySloMonitor`. The earlier
> `backend.orchestrator.canary.CanaryOrchestrator` is **deprecated**: its
> default `_StubMonitor` always reports a healthy snapshot, so the SLO
> half of its gate never fired (deploy-pipeline audit findings #9/#10).
> Do **not** instantiate `CanaryOrchestrator` for a production rollout.
> Operators normally drive the controller through the admin API
> (`backend/routers/canary_rollout.py`); the raw Python below documents
> the same calls for break-glass use.

## 0. Pre-flight

| Check | Command / location |
| --- | --- |
| D9 prod-deploy succeeded for the release | `prod_deploy_audit.status='completed'` for the release_id |
| Canary upstream is live and `/readyz` 200 | `curl -sf $OMNISIGHT_CANARY_UPSTREAM/readyz` |
| Stable allocation is currently 100% of traffic | `deploy/blue-green/canary_state.json` shows `status` `completed`/`aborted` from a prior rollout, or no rollout in flight |
| Rolling SLI is live | The backend exports `omnisight_rolling_deploy_5xx_rate` and is feeding the rolling p95 window (`ha_observability.record_http_latency`, via the HTTP middleware) — the monitor reads both in-process |
| Operator paged in `#deploy-canary` Slack | Manual gates require an attending operator |

## 1. Stages and SLO thresholds

| Stage | Canary % | Observe window | Gate |
| --- | --- | --- | --- |
| `5` | 5 | 10 minutes | `evaluate()` advances on pass |
| `25` | 25 | 15 minutes | `evaluate()` advances on pass |
| `100` | 100 | n/a (terminal) | `evaluate()` completes the rollout |

The SLO gate (`backend.canary_rollout.SloThresholds`) **aborts** the
rollout the moment any threshold is breached:

* `error_rate ≥ 0.5%` (`max_error_rate=0.005`) — rolling 5xx share
* `p95_latency_ms > 1000 ms` (`max_p95_latency_ms=1000.0`) — rolling p95
* `availability < 99%` (`min_availability=0.99`)

The `error_rate` and `p95_latency_ms` signals come from the in-process
rolling window (`backend/ha_observability.py`): `current_5xx_rate()` and
`current_p95_latency_ms()` over the trailing 60 s of real traffic the
canary cohort served.

> **⚠ Security-sensitive (human +2 required):** any change to these
> thresholds must be reviewed and +2'd by a human reviewer. They are the
> only thing standing between a bad image and 100% of production traffic.

A breach is an **auto-abort** — see §3.

## 2. Happy-path procedure

The controller is **poll-based**: `start()` applies the 5% stage and
writes the state file; `evaluate(state)` is then called periodically
(the admin dashboard polls `POST /canary-rollout/evaluate`). Each
`evaluate()` pulls a fresh SLO snapshot and returns a `CanaryDecision`
whose `action` is one of `hold` / `advance` / `complete` / `abort`.

1. **Start the rollout.** Break-glass Python:

       from backend.canary_rollout import CanaryController, RollingDeploySloMonitor

       controller = CanaryController(monitor=RollingDeploySloMonitor())
       state = controller.start(
           rollout_id="<release_id>",
           stable_color="blue",
           canary_color="green",
       )

   (The defaults already wire `RollingDeploySloMonitor`, the
   `CaddyWeightWriter`, and `SloThresholds`; passing the monitor
   explicitly documents intent.) Or via the API:
   `POST /canary-rollout/start` (admin-only) with
   `{"rollout_id": "...", "stable_color": "blue", "canary_color": "green"}`.

   The writer renders `deploy/blue-green/canary-weighted.caddy` with
   `lb_policy weighted_round_robin 95 5` and persists
   `deploy/blue-green/canary_state.json`. Trigger `caddy reload` (or
   `systemctl reload caddy` on production).

2. **Observe for 10 min.** Watch the `omnisight-canary-rollout` Grafana
   panel and the rolling SLI gauges. Calling `evaluate()` before the
   observe window closes returns `action="hold"`,
   `reason="observe_window_open"` — no state change.

3. **Advance to 25%.** Call `controller.evaluate(state)` (or poll
   `POST /canary-rollout/evaluate`). With the observe window elapsed and
   the SLO snapshot within thresholds it returns `action="advance"`,
   rewrites the snippet with `lb_policy weighted_round_robin 75 25`, and
   persists the new state. Reload Caddy.

4. **Observe for 15 min**, then `evaluate()` again. On pass it returns
   `action="advance"` and renders the 100% snippet (a single canary
   upstream — `OMNISIGHT_UPSTREAM_B` when `canary_color="green"`).

5. **Promote to terminal.** One final `evaluate()` at the `100` stage
   returns `action="complete"` and flips `status` to `completed`. No
   further snippet rewrite is needed.

### Manual gate overrides

`controller.manual_control(state, command, reason=...)` (admin API:
`POST /canary-rollout/control`) supports `pause`, `resume`, `advance`,
and `abort` for operator-driven control independent of the SLO poll —
e.g. `advance` to skip a clean stage early, or `pause` to freeze the
current allocation while investigating.

## 3. Failure modes and rollback

### 3.1 SLO breach — auto-abort

When `evaluate()` sees a breached threshold it returns
`action="abort"`, sets `status="aborted"`, stamps the offending signal
into `reason` (e.g. `slo_p95_latency:1450.0`, `slo_error_rate:0.0123`,
`slo_availability:0.9810`), and rewrites the snippet to a **single
stable upstream** (`OMNISIGHT_UPSTREAM_A` when `stable_color="blue"`),
restoring 100% stable. Trigger `caddy reload`.

Post-incident actions:

* File a follow-up ticket on the META; attach the `canary_state.json`
  and the `reason` string.
* Hold a 15-minute soak on the stable allocation before retrying.

### 3.2 Manual abort

`controller.manual_control(state, "abort", reason="...")` (or
`POST /canary-rollout/control` with `{"command": "abort"}`) writes the
same single-stable-upstream snippet and sets `status="aborted"`. Use it
when the operator decides to roll back on a signal the SLO gate does not
cover (e.g. a downstream alert). Reload Caddy.

### 3.3 State file missing / corrupt

`load_state()` (and the `/evaluate`, `/control`, `/assignment`
endpoints) read `deploy/blue-green/canary_state.json`. A missing file
surfaces as `FileNotFoundError` (HTTP 404 from the API). Re-run
`start()` to re-initialise — `start()` always writes a fresh state and
snippet and re-acquires the allocation.

## 4. Observability

The real controller is **state-file based**, not SSE-based: there is no
event bus publish. Operators inspect:

* `deploy/blue-green/canary_state.json` — `status`, `stage_index`,
  `reason`, `updated_at` (authoritative rollout state).
* The `CanaryDecision` returned by each `evaluate()` /
  `POST /canary-rollout/evaluate` call — `action`, `stage`, `reason`,
  and the `snapshot` (`error_rate`, `p95_latency_ms`, `availability`,
  `source`).
* Prometheus: `omnisight_rolling_deploy_5xx_rate` and the rolling p95
  latency window that back the SLO snapshot.

## 5. Recovery / rollback checklist

* `deploy/blue-green/canary_state.json` is the authoritative state file;
  `deploy/blue-green/canary-weighted.caddy` is the rendered upstream
  snippet Caddy imports (`canary_weighted_upstream_rp`).
* An abort (auto or manual) writes the single stable upstream — a second
  abort is idempotent (same snippet).
* Re-running `start()` is safe — it re-initialises state and re-acquires
  the allocation at 5%.

## 6. References

* `backend/canary_rollout.py` — **real** SLO-gated controller
  (`CanaryController` + `RollingDeploySloMonitor`) — implementation
* `backend/routers/canary_rollout.py` — admin API (start / evaluate /
  control / assignment)
* `backend/ha_observability.py` — rolling 5xx + p95 SLI the monitor reads
* `backend/tests/test_canary_rollout_op771.py` — controller contract tests
* `backend/orchestrator/canary.py` — **deprecated** stub-monitor
  orchestrator (OP-1694); retained for tests/docs/ADR references only,
  a later cleanup ticket deletes it
* `backend/tests/test_canary.py` — deprecated-orchestrator contract tests
* META OP-761 §Phase 3
