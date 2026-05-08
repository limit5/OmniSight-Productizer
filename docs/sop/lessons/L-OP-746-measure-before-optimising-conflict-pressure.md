---
id: L-OP-746
ticket: OP-746
title: Measure before optimising conflict pressure
date: 2026-05-08
tags: [observability, runner, conflict, process]
---

# Measure before optimising conflict pressure

**Situation**: On 2026-05-08 the runner pipeline hit the same conflict
on `docs/sop/lessons-learned.md` for three different patchsets twice
within an hour. The team had been shipping rate-suppression knobs (R1
backpressure, R3 worktree fix, R4 lesson restructure) one after another
on the assumption "more conflicts = bad, fewer = good," but with no
signal to confirm whether each fix actually moved the underlying rate.
Every fix was hopeful — none measured.

**Fix**: OP-746 introduced a three-layer observability stack:

1. `ps_merged_metrics` log line emitted by `gerrit_jira_bridge` on
   every `change-merged` event (lifetime, patchset count, diff size,
   verified -1 count).
2. `conflict_observations` Postgres/SQLite table (alembic 0203) populated
   on Verified -1 votes (bridge `comment-added` handler) and on
   sibling-merged rebase conflicts (auto-rebase sweeper).
3. Daily `scripts/conflict_report.py` that aggregates 24h totals,
   medians, and hotspot files; dispatches three alert thresholds via
   the OP-722 operator notifier; powers the new operator dashboard
   tile (`/admin/conflict-trend`).

**Verification**: `backend/tests/test_conflict_report.py` covers the
acceptance criteria — `ps_merged_metrics` extraction, observation
inserts, daily-report shape, alert envelopes, and a synthetic 10-PS
tally that pins the totals/medians/hotspot ordering.

**Generalisation**: Before shipping the next "fix" to a recurring
process problem, ship the *measurement* first. A fix without a baseline
metric is hopeful intervention; with one, it becomes engineering. The
cost of one extra ticket for observability is far smaller than the cost
of N speculative fixes whose aggregate effect nobody can decide on.
