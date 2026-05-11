# Canary Rollout Runbook — D10 (OP-882)

**Owner:** Deploy on-call · **Spec:** META OP-761 §Phase 3 · **Ticket:** OP-882

This runbook is the procedure for shifting production traffic onto a
new image via the staged 5% → 25% → 100% canary controlled by
`backend.orchestrator.canary.CanaryOrchestrator`. It is invoked
*after* the D9 prod-deploy orchestrator (`backend/orchestrator/prod_deploy.py`)
has placed the new image behind the canary upstream — D10 only governs
the traffic shift.

## 0. Pre-flight

| Check | Command / location |
| --- | --- |
| D9 prod-deploy succeeded for the release | `prod_deploy_audit.status='completed'` for the release_id |
| Canary upstream is live and `/readyz` 200 | `curl -sf $OMNISIGHT_CANARY_UPSTREAM/readyz` |
| Stable allocation is currently 100% of traffic | `head -2 deploy/caddy/canary.caddy` shows `canary-stage: rollback` or stage `100` of a prior rollout |
| Baseline p95 latency captured | Note the value used for `SloBaseline(p95_latency_ms=…)` — usually the 7-day rolling median from the metric-baseline comparator (`backend/staging_validation.py`) |
| Operator paged in `#deploy-canary` Slack | Manual gates require an attending operator |

## 1. Stages and SLO bounds

| Stage | Canary % | Observe window | Manual gate |
| --- | --- | --- | --- |
| `5` | 5 | 5 minutes | Yes |
| `25` | 25 | 10 minutes | Yes |
| `100` | 100 | n/a (terminal) | Yes — completes the rollout |

The SLO gate at each transition enforces:

* `error_rate < 0.5%` over the observe window
* p95 latency within **±20%** of the captured baseline

A breach on either signal is a **CanaryGateFailed** — see §3.

## 2. Happy-path procedure

1. **Start the rollout.** Call:

       orch = CanaryOrchestrator(
           rollout_id="<release_id>",
           stable_color="blue",
           canary_color="green",
           baseline=SloBaseline(p95_latency_ms=<baseline_ms>),
       )
       orch.start()

   The writer renders `deploy/caddy/canary.caddy` with
   `lb_policy weighted_round_robin 95 5`. Trigger `caddy reload` (or
   `systemctl reload caddy` on production). An SSE event
   `canary.stage.started` fires with `stage="5"`.

2. **Observe for 5 min.** Watch the `canary.stage.*` SSE feed and the
   Grafana panel `omnisight-canary-rollout`. Do **not** call
   `orch.advance()` before the observe window closes — the manual gate
   will reject with `CanaryGateFailed: observe window open`.

3. **Advance to 25%.** Call `orch.advance()`. The orchestrator pulls
   a fresh SLO snapshot, verifies the bounds, rewrites
   `deploy/caddy/canary.caddy` with `lb_policy weighted_round_robin
   75 25`, and emits `canary.stage.transitioned` with
   `from_stage="5"`. Reload Caddy.

4. **Observe for 10 min**, then `orch.advance()` again. The 100%
   snippet (`weighted_round_robin 0 100`) is rendered and
   `canary.stage.transitioned` fires with `from_stage="25"`.

5. **Promote to terminal.** One final `orch.advance()` flips the
   internal state to `completed` and emits `canary.completed`. No
   snippet rewrite is needed since 100% is already in place.

## 3. Failure modes and rollback

### 3.1 `CanaryGateFailed` — SLO breach

The orchestrator has *already* emitted `canary.gate.failed` (carrying
the offending snapshot) and triggered `rollback(reason=…)` which
restores the 100% stable allocation by writing
`lb_policy weighted_round_robin 100 0`. Trigger `caddy reload`. The
SSE event `canary.rolled_back` is the all-clear signal.

Post-incident actions:

* File a follow-up ticket on the META; attach the SSE event log.
* Hold a 15-minute soak on the stable allocation before retrying.

### 3.2 `TrafficShiftRaceCondition` — concurrent writers

Two orchestrators wrote to `canary.caddy` between read and write
(typically: the operator re-ran `start()` while a prior rollout still
held the file). The orchestrator retries once automatically. If the
race persists, the error propagates and the operator must:

1. Inspect `head -1 deploy/caddy/canary.caddy` for the
   `# canary-owner:` marker — that is the holding `rollout_id`.
2. Confirm with the holding operator that their rollout is finished
   or aborted (their `canary.completed` / `canary.rolled_back` event
   must have fired).
3. Manually overwrite the marker (`echo "# canary-owner: <new>" |
   sudo tee deploy/caddy/canary.caddy.head…`) or call
   `orch.rollback(reason="manual_reset")` from a new orchestrator with
   the previous `rollout_id` to clear ownership.

### 3.3 `RollbackFailed` — operator paged

The rollback path itself could not write the snippet (typically: a
second race fires during the rollback). This is the only error that
**pages** rather than just alerts. Procedure:

1. SSH to the prod Caddy host.
2. Manually replace `/etc/caddy/canary.caddy` with the prior
   100%-stable allocation:

       (omnisight_canary_upstream) {
           reverse_proxy $OMNISIGHT_STABLE_UPSTREAM {
               health_uri /readyz
           }
       }

3. `caddy reload`.
4. File a SEV-2 incident; include the orchestrator log dump.

## 4. SSE event catalog

| Event | Stage | When |
| --- | --- | --- |
| `canary.stage.started` | `5` | After `start()` applies 5% allocation |
| `canary.stage.transitioned` | `25` / `100` | After `advance()` promotes a stage |
| `canary.gate.failed` | breaching stage | Before rollback fires |
| `canary.rolled_back` | `rollback` | After the rollback writer succeeds |
| `canary.completed` | `100` | After the terminal `advance()` |

All payloads carry `rollout_id`, `stage`, `canary_percent`,
`stable_color`, `canary_color`. SLO-aware events additionally carry
`error_rate`, `p95_latency_ms`, and `snapshot_source`.

## 5. Recovery / rollback checklist

* `deploy/caddy/canary.caddy` is the authoritative state file —
  always inspect its `# canary-owner:` and `# canary-stage:` headers.
* `orch.rollback(reason=…)` is idempotent: a second call writes the
  same 100%-stable snippet.
* Re-running the entire D9 + D10 pipeline is safe — the prod-deploy
  audit ledger short-circuits idempotent replays, and the canary
  controller re-acquires ownership of the snippet on the next
  `start()`.

## 6. References

* `backend/orchestrator/canary.py` — implementation
* `backend/tests/test_canary.py` — contract tests
* `deploy/caddy/canary-template.caddy` — Caddy template
* META OP-761 §Phase 3
* OP-771 (legacy progressive controller — superseded for D10 but
  retains the per-tenant hashing helper used by sticky-canary cohorts)
