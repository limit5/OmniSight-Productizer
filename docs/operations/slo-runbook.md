# OP-883 D11 — Continuous SLO Monitor & Auto-Rollback Runbook

**Ticket:** OP-883 (D11, META OP-761 Sprint D §Phase 3) • **Status:** active
**Module:** `backend/orchestrator/slo_monitor.py`
**Config:** `config/slo_thresholds.yaml`
**Related runbooks:** `docs/operations/slo-monitor-rollback.md` (OP-772
deploy-window worker, narrower scope) • `docs/operations/prod-deploy-runbook.md`
(OP-881 D9 deploy orchestrator) • `docs/operations/as-rollout-and-rollback.md`
(OP-771 D10 canary controller).

---

## 1. What this monitor does

The D11 monitor is the **always-on** SLO watchdog above the production
stack. Every 30 s it samples Prometheus and asks two questions:

1. Is the **error rate** above 1% over the last minute? (AC #2)
2. Is the **p95 latency** above 500 ms over the last 5 minutes? (AC #2)

If either answer stays "yes" for **more than 2 minutes** (AC #3), the
monitor:

1. publishes an SSE frame `slo.breach` carrying the breached metric(s),
   the live numbers, and the rollback mode (`canary` or `full`);
2. pages the on-call operator (`NotificationLevel.critical`, severity
   `P1`, source `slo-monitor`);
3. invokes the rollback trigger — **D10 canary abort** when a canary
   rollout is currently `running` or `paused`, **D9 full prod
   rollback** otherwise;
4. arms a 10-min cooldown (AC #4) during which further ticks short-
   circuit before touching Prometheus.

When the metric backend itself goes dark the monitor **fails open**:
it logs a warning and refuses to auto-rollback. A Prometheus outage
is not evidence of a prod incident, and one of D11's hard rules is
that the measurement plane cannot trigger the control plane.

## 2. SLO thresholds (config/slo_thresholds.yaml)

| Key                          | Default | Spec |
|------------------------------|--------:|------|
| `error_rate_max`             |   0.01  | AC #2 — error rate < 1% |
| `p95_latency_ms_max`         |   500   | AC #2 — p95 < 500ms     |
| `error_rate_window_seconds`  |    60   | 1-min window            |
| `p95_window_seconds`         |   300   | 5-min window            |
| `sample_interval_seconds`    |    30   | AC #1 — sample every 30s|
| `breach_sustain_seconds`     |   120   | AC #3 — >2min sustain    |
| `cooldown_seconds`           |   600   | AC #4 — 10-min cooldown  |

Edit the YAML, then `systemctl restart omnisight-slo-monitor` to pick
up the new numbers. Partial YAML is supported: any unspecified key
falls back to its dataclass default.

## 3. Operator override — suppress flag (AC #5)

For **known transient** incidents (planned partner outage, scheduled
chaos drill, third-party CDN cutover) the monitor must not fight the
operator. Toggle the suppress flag two ways — the monitor re-reads
both every tick, no restart needed:

```bash
# File-based (preferred — survives daemon restart):
touch deploy/blue-green/slo_monitor_suppress.flag
# … incident resolved …
rm  deploy/blue-green/slo_monitor_suppress.flag
```

```bash
# Or env-based (process-scoped, useful in tmux for short windows):
export OMNISIGHT_SLO_MONITOR_SUPPRESS=1
```

While suppressed, every tick returns `suppressed` and the in-flight
breach streak is cleared. Removing the flag mid-incident does **not**
instantly retrigger — the 2-min sustain window restarts from scratch,
giving the operator time to verify the prod stack is actually healthy
before the auto-rollback re-arms.

## 4. SSE frame schema (frontend contract)

```
event: slo.breach
data: {
  "error_rate":               0.05,
  "p95_latency_ms":           120.0,
  "error_rate_threshold":     0.01,
  "p95_latency_ms_threshold": 500.0,
  "breached_metrics":         ["error_rate"],
  "rollback_mode":            "canary",
  "reason":                   "SLO breach sustained >120s on error_rate",
  "timestamp":                "2026-05-11T…Z"
}
```

The frontend renders this as a red-card incident banner on the
deploy dashboard. The `rollback_mode` field decides whether the
banner links to the canary controller or the prod-deploy audit row.

## 5. Rollback decision table

| Canary state (`deploy/blue-green/canary_state.json`) | Mode    | What fires |
|------------------------------------------------------|---------|------------|
| `running` or `paused`                                | canary  | `CanaryController.manual_control(state, "abort")` — Caddy snippet drops back to the stable upstream. |
| `aborted` / `completed` / file missing               | full    | `ProductionDeployOrchestrator._rollback(<current tag>)` — i.e. `scripts/deploy.sh --rollback`. |

The mode is picked at trigger time, not at monitor start, so a
canary that completes mid-incident transitions cleanly to the D9
full path on the next sustained breach.

## 6. Failure modes

| Symptom                                  | Monitor behaviour                                                              | Operator action |
|------------------------------------------|--------------------------------------------------------------------------------|------------------|
| Prometheus 5xx / timeout                 | logs `metric source unavailable`, returns `metric_unavailable`, no rollback    | Investigate the metric backend; do **not** disable the monitor — fail-open is by design. |
| Rollback trigger raises                  | logs the exception, returns `RollbackOutcome(status="failed", …)`              | Run the rollback manually per the canary / prod runbook; check the on-call page. |
| Cooldown active during manual rollback   | `maybe_rollback()` raises `CooldownInEffect`                                   | Wait out the 10-min window or call `set_engine_for_tests` from a shell — DO NOT bypass in prod. |
| Flag file present but env unset (or vice-versa) | `is_suppressed()` returns `True` (either source wins)                    | Both sources are intentional; document which one you flipped in the incident log. |

## 7. Manual rollback (operator-driven)

When the auto path is unsafe (e.g. a region-specific incident the
monitor cannot see) drop to the shell:

```python
from backend.orchestrator import slo_monitor as sm
monitor = sm.build_default_monitor()
outcome = monitor.maybe_rollback("region-eu-1 packet loss, manual drain")
print(outcome)  # RollbackOutcome(mode=…, status=…, detail=…)
```

`maybe_rollback` honours the AC #4 cooldown — if the auto-monitor
already fired in the last 10 minutes it raises `CooldownInEffect`
rather than racing the auto path. Wait it out and retry.

## 8. Synthetic-breach DoD on staging

The DoD calls for one synthetic breach on staging that triggers a
verified auto-rollback. Drive it via:

```bash
# Stage 1: arm the suppress flag so a real breach can't fire
# while we set up.
touch deploy/blue-green/slo_monitor_suppress.flag

# Stage 2: in another shell, point a load generator at the staging
# mirror with a 5xx-injection knob (e.g. vegeta + ENABLE_FAULT_5XX=1).
# Confirm Prometheus shows error_rate >= 0.05 on a 1-min window.

# Stage 3: drop the flag. Within breach_sustain_seconds + sample_interval
# the monitor should emit `slo.breach` and call the canary abort.
rm deploy/blue-green/slo_monitor_suppress.flag

# Stage 4: verify
#   * `deploy/blue-green/canary_state.json` is now `aborted`
#   * an SSE `slo.breach` frame reached the dashboard
#   * the on-call PagerDuty received the P1
```

Sign off the drill in the OP-883 ticket comment with the timestamp of
the breach SSE and the rollback outcome.
