# CostGuard Race Recovery Runbook

**Ticket:** OP-1065  
**Scope:** CostGuard audit-only recovery guidance. Do not edit `backend/agents/cost_guard.py` during this procedure.

## 1. When to Use

Use this runbook when CostGuard emits budget alerts around concurrent LLM calls, a per-ticket cap is hit after a call starts, or operators need to wait for a spend window to clear before retrying.

## 2. Triage

1. Identify the alert scope: `global`, `workspace`, `priority`, `task_type`, or `model`.
2. Confirm the alert level:
   - `warn_80`: notify only; work may continue.
   - `cap_100`: throttle; avoid starting more work for that scope.
   - `over_120`: block; do not retry until spend drops or the budget is deliberately raised.
3. Compare the affected ticket's latest estimate and actual usage. A mid-call actual overrun can make the next pre-submit check block even if the original estimate was allowed.

## 3. Recovery Paths

### Global Cap Race

1. Pause new runner pickup for the affected class or scope.
2. Let in-flight calls finish and record actual cost.
3. Re-run the blocked ticket only after `alerts_since` no longer shows a fresh `over_120` event for the same scope, or after an operator raises the budget.

### Per-Ticket Mid-Call Cap Hit

1. Do not resubmit immediately.
2. Inspect the actual usage that pushed the scope over cap.
3. If the call produced useful output, preserve it and avoid replay.
4. If replay is required, wait for the spend window to clear or ask the operator to adjust the cap.

### 60-Second Wait Recovery

1. Wait at least 60 seconds after the last blocked check.
2. Re-run one pre-submit check for the same estimate.
3. Proceed only if the check returns `allowed=True` and no `over_120` alert is emitted.
4. If it still blocks, repeat triage from §2 instead of looping retries.

## 4. Operator Notes

- This audit found no production code change in scope for OP-1065.
- Race-sensitive enforcement depends on the backing `CostStore` providing coherent spend reads under concurrent checks.
- File a follow-up ticket before changing CostGuard behavior.
