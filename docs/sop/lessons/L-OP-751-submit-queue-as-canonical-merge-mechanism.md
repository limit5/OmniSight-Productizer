---
id: L-OP-751
ticket: OP-751
title: Submit queue as the canonical merge mechanism
date: 2026-05-08
tags: [gerrit, runner, merge, submit-queue]
---

# Submit queue as the canonical merge mechanism

**Situation**: With N parallel PSes touching the same hot file, the
manual `rebase → +2 → wait → maybe-conflict-again → re-+2` loop is
O(N²) work for the operator. Each merge invalidates every still-open
sibling, the auto-rebase sweeper (OP-733) lights up, half the rebases
conflict, and the operator chases tail conflicts back into review. The
class of bug is "concurrent merge of overlapping changes" — exactly
what GitHub Merge Queue and Google's Rosie were built to remove.

**Fix**: OP-751 introduces a serialised submit queue. The operator
votes a new `Submit-Ready=+1` label (separate from `Code-Review`) and
the `submit_queue_worker.py` daemon picks the change up, rebases onto
the current `develop` tip via Gerrit's REST `/rebase` endpoint, runs
CI when OP-739 wires that hook, and submits atomically through
`/submit`. Per-change `fcntl.flock` lock files prevent double-process
between concurrent daemon instances, and a configurable inter-merge
sleep (default 30s) rate-limits the merges so the auto-rebase sweeper
and notifier have headroom to drain. On any failure (rebase conflict,
submit HTTP error, CI not-ready) the worker votes `Submit-Ready=-1`
with a human-readable comment; the operator fixes manually and
re-marks `+1` to retry. The label ACL allows both `non-ai-reviewer`
(operator marks `+1`) and `ai-reviewer-bots` (worker votes `-1`) to
score `-1..+1`.

**Verification**:
- `backend/tests/test_submit_queue_worker.py::test_synthetic_five_ps_burst_all_merge_in_order_no_manual_rebase`
  pins the AC#4 invariant: 5 PSes on the same file all merge in
  operator-mark order, exactly one rebase per still-stale change, no
  manual intervention.
- `test_rebase_conflict_votes_minus_one_with_comment` and
  `test_after_minus_one_operator_can_retry_with_plus_one` pin the
  AC#5 failure-path round-trip.
- `test_change_lock_blocks_concurrent_acquisition` and
  `test_process_change_skips_when_lock_held` pin AC#3 (per-change
  lock prevents double-process).
- `test_project_config_defines_submit_ready_label` pins AC#1 (label
  defined in `project.config`).
- `test_skips_change_that_is_not_submittable` pins the contract that
  the queue defers to Gerrit's existing `submitRecords` (Human-Plus-2
  / No-Veto / Verified) rather than overriding policy.

**Generalisation**: When the same race-condition class shows up
repeatedly in operator workflow (sibling merge invalidates open work,
operator chases conflicts), serialise the merge step rather than
patching individual races. The submit queue is the canonical merge
mechanism for OmniSight — once OP-751 ships, manual `+2 → submit`
should be reserved for hotfixes that explicitly bypass the queue,
and the queue's `Submit-Ready` label becomes the operator's "land
this" verb. The same pattern applies to any pipeline where the
output of one step is the input of the next: serialise the
mutation, parallelise everything around it.
