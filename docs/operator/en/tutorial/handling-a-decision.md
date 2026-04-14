# Tutorial · Handling a decision (8 minutes)

> **source_en:** 2026-04-14 · authoritative

Follow-on to [First Invoke](first-invoke.md). Here we look at a
decision end-to-end: what appears where, how to decide, and how to
recover if you pressed the wrong button.

## 1 · Force a decision to appear

Open the **Orchestrator AI** and type:

```
/invoke push workspace changes to origin/main
```

In SUPERVISED mode this will propose a **destructive** decision
(pushing to `main` is non-reversible without a force-push + hope).

## 2 · What you see

Three synchronised surfaces show the same decision:

- **Toast** top-right — red border, AlertOctagon icon, countdown
  pulsing red under 10 s.
- **Decision Queue** panel — the item appears at the top. Pending
  count badge increments. Panel row has a countdown column.
- **SSE log** (REPORTER VORTEX) — one line `[DECISION] dec-… kind=push
  severity=destructive`.

The default timeout is 60 s. Configurable at propose time, tuned at
the sweep loop (see `OMNISIGHT_DECISION_SWEEP_INTERVAL_S`).

## 3 · Decide

Three paths:

### Approve
Click APPROVE. Because severity is `destructive`, a
`window.confirm()` dialog pops up ("Approve DESTRUCTIVE decision?").
This is the B10 safeguard — you cannot accidentally greenlight prod
pushes with a stray `A` keypress.

Confirm → the agent continues, the decision moves to HISTORY, the
toast clears.

### Reject
Click REJECT. Same confirm dialog for destructive. Confirm →
the agent stands down. The decision moves to HISTORY with
`resolver=user, chosen_option_id=__rejected__`.

### Timeout
Do nothing. When the countdown hits 0, the sweep loop auto-resolves
to the decision's `default_option_id` (usually the safe option for
destructive severities). `resolver=timeout` is recorded.

## 4 · Undo

Open the Decision Queue, switch to the **HISTORY** tab (click
HISTORY or press → arrow from PENDING). Find the decision you just
resolved. Click **UNDO**.

What undo does *NOT* do: it does not reverse the real-world effect
(the git push already landed). It only flips the decision state to
`undone` and emits a `decision_undone` SSE event so your
process-of-record knows the operator changed their mind.

Interpret `undone` as "audit log: the operator regrets this" rather
than "the system undoes what it did". True reversal requires a
compensating action you initiate manually (e.g. `git push -f` with
the prior commit).

## 5 · Observe the SSE round-trip

Open a second browser tab on the same dashboard. You'll see all the
same events propagate in real time — the Decision Queue, the toast,
the mode pill — everything syncs via SSE `/api/v1/events`.

Close one tab. The other keeps working. This is the shared-SSE
manager added in Phase 48-Fix: one EventSource per browser shared
across all panels.

## 6 · Define a Rule to avoid this next time

If you *always* want to auto-approve pushes to a specific branch
pattern, open the **Decision Rules** panel:

```
kind_pattern: push/experimental/**
auto_in_modes: [supervised, full_auto, turbo]
severity: risky          # downgrade from destructive
default_option_id: go
```

Save. The next matching decision will auto-execute in any listed
mode. Rules persist to SQLite (Phase 50-Fix A1) and survive restarts.

## Related

- [Decision Severity](../reference/decision-severity.md) — why
  destructive gets a confirm and risky does not.
- [Operation Modes](../reference/operation-modes.md) — the severity ×
  mode auto-execute matrix.
- [Troubleshooting](../troubleshooting.md) — `[AUTH]` /
  `[RATE LIMITED]` banners and the "button seems to do nothing"
  family.
