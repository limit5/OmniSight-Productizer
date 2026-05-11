---
audience: operator
ticket: OP-854
---

# Replay Deprecation (OP-854)

> **Spec source**: `docs/audit/2026-05-11-sprint-abc-master-plan.md` §3.3
> **Replaces**: OP-820 + OP-830 `scripts/replay_*.py` CLI re-run mechanism
> **Replaced by**: `backend/agents/incident_recorder.recall_similar_incidents` (C2)
> **Removal date**: 30 days post-OP-854 merge

---

## What changed

Before OP-854, recovering from a runner failure meant the operator
running `python scripts/replay_<scenario>.py <TICKET-KEY>` to re-pick
the ticket with the prior failure context loaded into the prompt.
That step is now redundant: the runner queries the
`runner_incidents` Postgres table at pickup time, filters priors by
`(area, failure_class)`, and injects the top-3 as a system message
before the model sees the ticket.

The new pickup-time path is governed by:

| Component | File |
|---|---|
| Closed `failure_class` taxonomy | `backend/agents/failure_class.py` |
| Write path (tag + persist) | `backend/agents/incident_recorder.record_runner_incident` |
| Recall path (top-3 priors) | `backend/agents/incident_recorder.recall_similar_incidents` |
| Tier policy gate (S/M/L/X) | `backend/agents/memory_tool_handler.enforce_recall` |
| Schema | `backend/alembic/versions/0206_runner_incidents.py` |

## What you should do

1. **Stop calling `scripts/replay*.py` from cron / systemd / operator
   notebooks.** The shim at `scripts/replay.py` emits a
   `DeprecationWarning` and exits with code `2` so dependent automation
   fails loudly during the deprecation window.
2. **Verify the recall path is active for your runner fleet** —
   `OMNISIGHT_MEMORY_TIER_L_OPTIN=1` is required for tier:L recalls;
   tier:S/M are on by default; tier:X is refused (operator
   authorization required, per C6 policy).
3. **For incident review**, use the Failure-Graph runbook
   (`docs/operations/failure-graph-runbook.md`) instead of running a
   replay to "see what happened" — the graph is derived from the same
   `runner_incidents` rows.

## Recovery: re-classification from `raw_traceback`

The migration is forward-only (per the OP-854 recovery section). If
the failure-class column drifts (e.g. an operator hand-edited a row,
or a new enum member was added without backfilling), re-classify
existing rows idempotently:

```python
from backend.agents.failure_class import classify_from_traceback
from backend.agents.incident_recorder import (
    RunnerIncidentRecord,
    record_runner_incident,
)

# Pseudo: for each row in runner_incidents,
#   record_runner_incident(
#       ticket_key=row.ticket_key,
#       failure_class=classify_from_traceback(row.raw_traceback),
#       summary=row.summary,
#       raw_traceback=row.raw_traceback,
#       runner_class=row.runner_class,
#       mutex_label=row.mutex_label,
#       area=row.area,
#       incident_id=row.incident_id,  # idempotent on PK
#   )
```

`classify_from_traceback` is the same regex table used by the live
runner, so the result of an offline replay matches what a fresh
incident would receive.

## Timeline

| Day | Event |
|---|---|
| T+0 | OP-854 merged. `scripts/replay.py` shim live, emits `DeprecationWarning`. |
| T+7 | Operator review: any remaining callers? Reach out to runner-leads. |
| T+30 | `scripts/replay.py` deleted. Cron / systemd should be migrated by then. |

## Error catalog quick reference

| Exception | Meaning | Operator action |
|---|---|---|
| `FailureClassUnregistered` | Incident wrote with class outside enum | Coerced to `OTHER`; file follow-up to expand enum if pattern recurs. |
| `FailureClassRecallEmpty` | No priors match `(area, failure_class)` | Informational — fresh ticket, no preamble injected. |
| `FailureClassMigrationFailed` | Alembic apply errored | Rollback via `alembic downgrade -1`, fix DDL, re-apply. |
| `MemoryToolUnavailableForC2` | C1 Memory Tool backend down | Recall falls back to direct Postgres query — operator alert only if both paths fail. |

## Why we kept the shim instead of deleting the family outright

A few production crons still reference `replay_*` by name. Returning a
nonzero exit with a clear stderr message is louder than `ModuleNotFoundError`
and gives the operator a one-line search target
(`replay-deprecation`) that lands here. The shim costs ~40 lines and
buys 30 days of grace; once those expire, delete `scripts/replay.py`
in the same PR that lands the C2 production cut-over.
