---
id: L-OP-743
ticket: OP-743
title: Terminal events should permissively converge workflow state
date: 2026-05-08
tags: [ci, gerrit, jira, runner]
legacy_lesson: 29
---

# Terminal events should permissively converge workflow state

**Situation**: Gerrit `change-merged` is the source-of-truth terminal
event for code landing, but the bridge originally required JIRA to be
exactly `Approved` before moving the ticket to `Published`. Real
operational paths left merged tickets at `In Progress` or `Under Review`
when an intermediate transition was skipped, retried, or performed
manually out of order.

**Fix**: OP-743 changed the bridge's merge handler and startup catchup
to force-walk known forward states (`In Progress` → `Under Review` →
`Approved` → `Published`) while keeping `Published` idempotent and
`Archived` protected from automatic unarchive.

**Verification**: `backend/tests/test_gerrit_jira_bridge.py::test_change_merged_force_walks_five_source_states`
pins the five-state matrix, and
`test_catchup_force_walks_merged_under_review_ticket` proves startup
catchup reuses the same force-walk path.

**Generalisation**: Automation that reacts to an authoritative terminal
event should converge workflow state from any safe predecessor, not only
from the ideal predecessor. Preserve explicit terminal/operator override
states, but do not strand work because a best-effort intermediate event
was missed.
