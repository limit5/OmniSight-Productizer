# Durable Op-Queue — Operator SOP (OP-757 / H10 of OP-747)

The durable operation queue (`backend/agents/op_queue.py`) makes every
external-system mutation issued by the runner — JIRA transitions /
comments / labels, Gerrit pushes — crash-safe. The producer records
the *intent* in a local SQLite database before invoking the remote
API; a worker drains the queue serially. On crash, the next process
startup resets in-flight rows back to `pending` and replays them.
Idempotency keys make replay safe.

> META ticket: OP-747 (H10 wave). Foundation ticket: OP-757.

## 1. What it eliminates

The "partial-state on crash" class. Examples that used to corrupt
ticket state and require operator cleanup:

- Runner crashes after `transition_to_in_progress` but before posting
  the pickup comment → ticket is in `In Progress` with no audit trail.
- Runner crashes after Gerrit push but before posting the
  `[runner-pushed-to-gerrit]` comment → operator has no link to the
  patchset on the JIRA ticket.
- Runner crashes mid-`add_label` after writing one label but before
  the second → label set is partial.

With the op-queue, every one of these mutations is enqueued first,
committed to disk, then executed. The crash leaves an `in_progress`
row that recovery converts to `pending`; the next worker tick replays.

## 2. Where the DB lives

```
~/.config/omnisight/op-queue.db
```

WAL mode (`PRAGMA journal_mode=WAL`) — concurrent readers (a future
operator dashboard) can inspect the queue without blocking the
worker. Synchronous=NORMAL (durable on commit, not durable across
power loss — acceptable for a queue whose worst-case is replay).

## 3. Schema

| Column            | Notes                                                        |
|-------------------|--------------------------------------------------------------|
| `id`              | Primary key (autoincrement).                                 |
| `action`          | Handler name, e.g. `jira.transition_to_in_progress`.         |
| `args_json`       | Handler args, JSON-encoded.                                  |
| `idempotency_key` | UNIQUE. Same key on insert is a no-op (returns existing id). |
| `ordering_key`    | Ops with the same key drain in seq order (e.g. ticket key).  |
| `seq`             | Monotonic per ordering_key. Producer-assigned at enqueue.    |
| `status`          | `pending` / `in_progress` / `completed` / `failed`.          |
| `created_at`      | UNIX timestamp at enqueue.                                   |
| `completed_at`    | UNIX timestamp at terminal status (completed or failed).     |
| `last_error`      | Truncated to 2 KB. Surfaces in escalation alerts.            |
| `retry_count`     | Number of failed attempts so far.                            |
| `not_before`      | Earliest UNIX time the worker may claim this row.            |

## 4. Producer / Worker contract

**Producer** (the runner): call one of the helper enqueuers, e.g.

```python
from backend.agents.op_queue import (
    get_default_queue, enqueue_jira_transition, enqueue_gerrit_push,
)

q = get_default_queue()
enqueue_jira_transition(
    q, agent_class="subscription-claude", key="OP-757", target="in_progress"
)
enqueue_gerrit_push(
    q, agent_class="subscription-claude",
    worktree_path="/path/to/worktree", ticket_key="OP-757",
)
```

The transition op and the push op share `ordering_key="OP-757"` — the
worker will not start the push until the transition has reached
`completed`. This satisfies the AC "transition → push → comment must
run in order for the same ticket".

**Worker** (single-thread by contract):

```python
from backend.agents.op_queue import Worker, get_default_queue
Worker(get_default_queue()).run_forever(poll_interval=1.0)
```

`drain_once()` claims at most one op via `BEGIN IMMEDIATE` + a single
`UPDATE ... WHERE id=? AND status='pending'` — two workers cannot
claim the same row.

## 5. Retry policy

| Attempt | Wait before retry (backoff_base=2.0) |
|---------|--------------------------------------|
| 1 → 2   | 2 s                                  |
| 2 → 3   | 4 s                                  |
| 3 → 4   | 8 s                                  |
| 4 → 5   | 16 s                                 |
| 5       | terminal `failed` + operator alert   |

`MAX_ATTEMPTS=5`. After exhaustion the op moves to `status='failed'`
and `_default_escalate` calls
`backend.agents.operator_notifier.notify(severity=DEGRADED, code=op_queue.exhausted:<action>, ...)`.
The alert lands per the severity matrix
(`docs/sop/notifier-config.md`) — JIRA + email by default.

## 6. Recovery

On `OpQueue.__init__` the queue runs:

```sql
UPDATE ops SET status='pending', not_before=0 WHERE status='in_progress'
```

This guarantees that any op the worker had claimed but not finished
before a crash is replayed. Replay is safe because:

- The handler args carry a stable `idempotency_key` that
  `jira_dispatch._request_idempotent` honours via JIRA's
  `X-Idempotency-Key` header AND a local response cache
  (`backend/agents/idempotency.py`). A duplicate POST therefore
  returns the cached response, not a duplicate side effect.
- Gerrit dedupes pushes on `Change-Id` (commit-msg hook), so a
  re-pushed worktree creates a new patchset on the same change rather
  than a duplicate change.

## 7. Phasing (per AC)

| Phase | Scope                            | Status                    |
|-------|----------------------------------|---------------------------|
| A     | JIRA transitions                 | Shipped — OP-757          |
| B     | JIRA comments + Gerrit pushes    | Shipped — OP-757          |
| C     | All audit log writes             | Out of scope — Postgres   |
| D     | Bridge daemon events             | Out of scope — follow-up  |

Phase C is intentionally not wired through the op-queue: the audit log
already commits to Postgres on a separate transaction
(`backend/audit.py`) and recovery there is RDBMS-native. Phase D
(gerrit-jira-bridge) consumes Gerrit's stream-events which is itself a
durable source — wrapping its outbound JIRA transitions through the
same queue is a natural follow-up but not part of OP-757.

## 8. Operator playbook

**See pending / failed ops:**

```python
from backend.agents.op_queue import get_default_queue, STATUS_FAILED
q = get_default_queue()
q.stats()                                    # {'pending': 3, 'completed': 1242, 'failed': 1}
for op in q.list_by_status(STATUS_FAILED):
    print(op.id, op.action, op.last_error)
```

**Manually replay a failed op:**

```python
from backend.agents.op_queue import get_default_queue, STATUS_PENDING
q = get_default_queue()
import sqlite3
with sqlite3.connect(str(q.path)) as conn:
    conn.execute(
        "UPDATE ops SET status=?, retry_count=0, not_before=0, last_error=NULL "
        "WHERE id=?",
        (STATUS_PENDING, OP_ID),
    )
```

After this update, the next `Worker.drain_once()` picks the row back up
and retries from a clean slate.

**Drop an op (give up on it):**

```sql
UPDATE ops SET status='failed', completed_at=strftime('%s','now')
WHERE id=:op_id;
```

The handler will not be invoked again. Use this only after manually
reconciling state in the remote system.

## 9. Tests

`backend/tests/test_op_queue.py` covers:

- WAL mode + spec'd schema (`test_sqlite_uses_wal_mode_and_creates_ops_table`)
- Idempotent insert (`test_idempotent_insert_returns_same_id`)
- Producer-commits-before-handler-runs (`test_enqueue_persists_before_handler_runs`)
- Single-claim guarantee (`test_claim_next_marks_in_progress_atomically`)
- Recovery on construction (`test_recover_on_construction_resets_in_progress`)
- Ordering per ticket (`test_ordering_constraint_serialises_per_ordering_key`)
- Cross-key parallelism (`test_ordering_does_not_block_other_keys`)
- Exponential backoff up to MAX_ATTEMPTS (`test_retry_uses_exponential_backoff_up_to_max_attempts`)
- Transient-then-success (`test_transient_failure_then_success`)
- Phase A / Phase B handlers
  (`test_phase_a_jira_transition_in_progress_invokes_dispatch`,
  `test_phase_b_gerrit_push_invokes_dispatch`, etc.)
- End-to-end recovery (`test_recovery_survives_restart_with_pending_op`)

Run via `python3 -m pytest backend/tests/test_op_queue.py -v`.
