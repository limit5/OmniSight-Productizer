# CostGuard Race Verification Report

**Ticket:** OP-1065  
**Date:** 2026-05-14  
**Scope:** Audit `backend/agents/cost_guard.py` for C6 race behavior. Production code is read-only for this ticket.

## Summary

CostGuard performs pre-submit budget checks by reading the current spend, adding the estimate, classifying the projected amount, saving alerts, and returning a `BudgetCheck`. The code path is audit-friendly and deterministic for single checks. It does not contain an explicit lock or compare-and-set boundary around `spend_in_period` plus alert emission, so concurrent callers rely on the backing `CostStore` for coherent spend reads.

## Code Path Audited

- `CostGuard.check(...)` computes candidate scopes, fetches each configured budget, reads current spend for daily/monthly caps, combines it with the estimate, classifies the level, persists alerts, invokes the optional alert sink, and blocks only when the level maps to `over_120`.
- Per-batch caps are checked only when the caller supplies `per_batch_observed_usd`.
- `record_actual(...)` updates actual usage after the call and uses actual cost for later spend calculations through the store.
- `InMemoryCostStore.spend_in_period(...)` is suitable for tests and dev, but it does not model timestamps; every estimate is treated as current-period spend.

## Verification Fixtures

`tests/test_costguard_race_audit.py` adds three audit fixtures:

| Fixture | What it verifies | Result |
| --- | --- | --- |
| `test_global_cap_race_allows_two_concurrent_cap100_checks` | Two concurrent global checks can both observe the same pre-write spend and both return `cap_100`/allowed. | Confirms store-level atomicity is required if `cap_100` should serialize starts. |
| `test_per_ticket_cap_mid_call_hit_blocks_next_check` | A call allowed before submit can record an actual overrun and make the next check block at `over_120`. | Confirms actual usage supersedes estimate for subsequent checks. |
| `test_wait_60s_recovery_allows_after_daily_spend_window_clears` | A mock store whose spend window clears after 60 seconds causes the first check to block and the later check to allow. | Confirms recovery behavior is store-window dependent and does not require CostGuard changes. |

## Findings

1. No direct CostGuard code change is required for OP-1065's audit-only scope.
2. The check path intentionally treats `cap_100` as throttle rather than block. Blocking starts at `over_120`.
3. Concurrent pre-submit checks are not serialized in `CostGuard.check`. If stricter race prevention is required, it should be implemented in the persistent `CostStore` transaction or filed as a separate behavior-change ticket.
4. Wait-window recovery cannot be proven with `InMemoryCostStore` because it has no created-at timestamp model; the audit fixture uses a mock store to exercise the contract.

## Follow-Up

No follow-up ticket was filed from this audit because no acceptance criterion requires changing runtime behavior. If operators want `cap_100` to block or require atomic reservation semantics for concurrent checks, file a new CostGuard behavior ticket before touching `backend/agents/cost_guard.py`.
