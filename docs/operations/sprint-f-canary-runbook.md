# Sprint F Canary Runbook — F13 (OP-911)

**Owner:** Sprint F launch operator · **Spec:** `docs/audit/2026-05-11-sprint-c-readiness-for-sprint-f.md` §1-2 · **Ticket:** OP-911

This runbook is the procedure for rolling out the F7 (OP-905) feature
flag `OMNISIGHT_PROJECT_STATE_INJECT` through a staged 5% → 25% → 100%
canary controlled by `scripts/sprint_f_canary_orchestrator.py` on top
of the D12 (OP-884) feature flag service. The flag gates the runner
prompt-builder's project-state injection — see
`backend/agents/feature_flags.py` for the read path and
`backend/agents/prompt_builder.py` for the consumer.

It reuses the D10 (OP-882) `CanaryOrchestrator` shape so operators
familiar with `docs/operations/canary-runbook.md` can transfer their
muscle memory — the differences are: (a) the per-stage observe window
is **24h** rather than minutes, (b) the gate evaluates three signals
(SLO + runner success rate + revert-loop pattern) rather than just
SLO, and (c) rollback is a single D12 `state='disabled'` PATCH rather
than a Caddy weight rewrite.

## 0. Pre-flight

| Check | Command / location |
| --- | --- |
| F7 prompt-builder gate landed | `git log --oneline --grep='OP-905'` shows the F7 merge into develop |
| D12 row exists for the flag | `curl -sf $OMNISIGHT_API/api/v1/feature-flags \| jq '.feature_flags[] \| select(.flag_name=="OMNISIGHT_PROJECT_STATE_INJECT")'` returns one row with `state="disabled"` |
| F14 SLO source wired | The `MetricsSource` injection points at the F14 SLO client (see §2.1) |
| Baseline runner success rate captured | Note the value from the prior 7-day rolling window in Grafana panel `omnisight-runner-success-rate` — passed as `baseline_runner_success_rate=` |
| Operator paged in `#sprint-f-canary` Slack | Manual gates require an attending operator across 48h+ |
| No competing Sprint F canary in progress | `sprint_f.canary.*` SSE channel quiet for ≥ 1h |

## 1. Stages and gate signals

| Stage | rollout_pct | Observe window | Manual gate |
| --- | --- | --- | --- |
| `5`   | 5   | 24h | Yes |
| `25`  | 25  | 24h | Yes |
| `100` | 100 | n/a (terminal) | Yes — completes the rollout |

At each transition the orchestrator's gate refuses promotion (and
rolls back) when **any** of the following are true for the past observe
window:

* `MetricsSource.snapshot().slo_breach_reason` is non-empty (F14
  signals an SLO breach).
* `MetricsSource.snapshot().runner_success_rate <
  baseline_runner_success_rate − 0.05` (5 percentage-point drop).
* `MetricsSource.snapshot().revert_loop_count > 0` — a new
  `[runner-no-commits-from-cli]` pattern fired during the window
  (OP-827 post-mortem signal; see `auto-runner-jira.py` for the
  emission site).

Either signal is a **`CanaryGateFail`** — see §3.

## 2. Happy-path procedure

### 2.1 Build the orchestrator

The orchestrator is dependency-injected: the operator supplies a
`FlagClient` (default `HttpFlagClient` against `/feature-flags`) and a
`MetricsSource` (the F14 SLO client). A canonical wrapper:

    from datetime import datetime, timezone
    from scripts.sprint_f_canary_orchestrator import (
        SprintFCanaryOrchestrator,
        HttpFlagClient,
        StageMetrics,
    )

    flag_client = HttpFlagClient(
        base_url="https://omnisight.internal/api/v1",
        auth_header="Basic " + os.environ["OMNISIGHT_API_AUTH"],
    )

    class F14Metrics:
        def snapshot(self, since: datetime) -> StageMetrics:
            slo = f14_client.slo_breach(since=since)
            return StageMetrics(
                slo_breach_reason=slo,
                runner_success_rate=runner_metrics.success_rate(since=since),
                revert_loop_count=runner_metrics.revert_loop_count(since=since),
            )

    orch = SprintFCanaryOrchestrator(
        rollout_id="op-911-sprint-f-canary",
        flag_client=flag_client,
        metrics=F14Metrics(),
        baseline_runner_success_rate=0.953,
    )

### 2.2 Drive the rollout

1. **Start at 5%.** `orch.start()` PATCHes the D12 flag to
   `state='enabled', rollout_pct=5`. `sprint_f.canary.stage.started`
   fires with `stage="5"`. Operators MAY now monitor.

2. **Wait 24h.** Watch the `sprint_f.canary.*` SSE feed and the
   Grafana panel `omnisight-sprint-f-canary`. Do **not** call
   `orch.advance()` before the observe window closes — the gate will
   reject with `CanaryGateFail: observe window open`.

3. **Advance to 25%.** `orch.advance()` pulls a fresh `StageMetrics`
   snapshot, verifies the gate, and PATCHes to `rollout_pct=25`.
   `sprint_f.canary.stage.transitioned` fires with `from_stage="5"`.

4. **Wait 24h**, then `orch.advance()` again to promote to 100%.
   `sprint_f.canary.stage.transitioned` fires with `from_stage="25"`.

5. **Promote to terminal.** One final `orch.advance()` emits
   `sprint_f.canary.completed`. No PATCH is needed since 100% is
   already in place.

## 3. Failure modes and rollback

### 3.1 `CanaryGateFail` — gate refused promotion

The orchestrator has **already** emitted `sprint_f.canary.gate.failed`
(carrying the offending `StageMetrics`) and issued
`rollback(reason=…)`, which PATCHes the flag to `state='disabled'`.
`sprint_f.canary.rolled_back` is the all-clear signal.

Post-incident actions:

* File a follow-up ticket on the F13 META; attach the SSE event log
  and the `StageMetrics` snapshot.
* Analyse runner / SLO logs to root-cause the breach (a `slo_breach`
  reason is F14's call; a `runner_success_rate` drop usually points at
  a prompt-builder regression; a `revert_loop_count > 0` is the
  OP-827 signal that the F7 injection is producing zero-commit runs).
* Hold a 24h soak with the flag off before retrying with a corrected
  baseline or after a fix lands.

### 3.2 `RolloutPercentMisapplied` — D12 service bug

`set_rollout_pct(name, pct)` returned a FlagState whose
`rollout_pct` does not equal the requested value. The orchestrator
treats this as a D12 service bug, issues an immediate rollback, and
raises so the operator files a follow-up.

Recovery:

1. Manually verify the D12 row via `GET /feature-flags`.
2. File a follow-up against the D12 OP-884 component with the audit
   trail of the PATCH that produced the mismatch.
3. Do **not** retry the canary until D12 is patched; the gate cannot
   trust its own writes.

### 3.3 `RollbackFailed` — operator paged

The D12 disable PATCH itself failed (typically: D12 host down or the
operator's auth token expired). This is the only error that **pages**
rather than just alerts. Procedure:

1. SSH to the prod backend host.
2. Update the row manually via psql:

       UPDATE feature_flags
       SET state='disabled', rollout_pct=0, updated_at=NOW()
       WHERE flag_name='OMNISIGHT_PROJECT_STATE_INJECT';

3. Restart any runner process so the registry cache invalidates within
   30s (per OP-773 cache contract).
4. File a SEV-2 incident; include the orchestrator log dump.

## 4. SSE event catalog

| Event | Stage | When |
| --- | --- | --- |
| `sprint_f.canary.stage.started`      | `5`        | After `start()` applies 5% |
| `sprint_f.canary.stage.transitioned` | `25` / `100` | After `advance()` promotes |
| `sprint_f.canary.gate.failed`        | breaching stage | Before rollback fires |
| `sprint_f.canary.rolled_back`        | `rollback` | After the disable PATCH succeeds |
| `sprint_f.canary.completed`          | `100`      | After the terminal `advance()` |

All payloads carry `rollout_id`, `flag_name`, `stage`, `rollout_pct`.
Gate-aware events additionally carry `slo_breach_reason`,
`runner_success_rate`, `revert_loop_count`.

## 5. Recovery / rollback checklist

* The D12 `feature_flags` row is the authoritative state — always
  inspect `state` and `rollout_pct` first; the orchestrator re-derives
  its state machine from those columns on every invocation.
* `orch.rollback(reason=…)` is idempotent: a second call writes the
  same `state='disabled'` payload.
* Re-running `orch.start()` after a rollback is safe — the rollback
  path leaves `state='disabled'` so `status()` reports "not started".

## 6. References

* `scripts/sprint_f_canary_orchestrator.py` — implementation
* `tests/test_sprint_f_canary_orchestrator.py` — contract tests
* `backend/agents/feature_flags.py` — D12 read path (OP-884)
* `backend/routers/feature_flags.py` — D12 PATCH endpoint (OP-884)
* `backend/orchestrator/canary.py` — D10 sibling (OP-882) the shape mirrors
* OP-905 — F7 prompt-builder gate that consumes the flag
* OP-827 lesson — `[runner-no-commits-from-cli]` revert-loop signal
