---
id: L-OP-798
ticket: OP-798
title: A long-lived clone needs its own freshness watchdog, separate from liveness
date: 2026-05-08
tags: [bridge, deploy, ops, watchdog, runner]
---

# A long-lived clone needs its own freshness watchdog, separate from liveness

**Situation**: The `gerrit-jira-bridge` daemon ran from a separate
working tree at `/home/user/sora-bridge/` to keep it isolated from the
runner / dev worktrees. Isolation worked — the daemon stayed up for
24h+ with `systemctl status active` — but with no automated sync the
clone drifted 233 commits behind `develop` and silently kept running
pre-OP-743 code for ~7 hours. Eight tickets were stranded at
`In Progress` / `Under Review` because every `change-merged` event hit
the old `APPROVED_STATUS_NAMES` gate and skipped the transition. The
OP-723 daemon-liveness watchdog was working perfectly the entire time:
heartbeat steady, unit loaded, no DEGRADED alert. Liveness ≠ freshness.

**Fix**: OP-798 Phase A added a second, independent watchdog —
`sora-bridge-sync.{service,timer}` — that fires every 5 min, fast-forwards
the clone, and restarts the daemon **only if** a file in a curated
relevant-files list changed (`backend/agents/gerrit_jira_bridge.py`,
`backend/agents/jira_dispatch.py`, `backend/db.py`,
`backend/git_accounts.py`, `scripts/run_gerrit_jira_bridge.py`). Dirty
working trees are a safe skip, not a failure, so operator debug edits
are preserved. Three consecutive sync failures escalate via
`operator_notifier` P0.

**Verification**: `scripts/sync_sora_bridge.sh` exercised end-to-end
against a sandbox clone (sync_no_change → no-op file commit →
sync_restarted → docs-only commit → sync_no_restart_needed → dirty tree
→ sync_skipped_dirty). The systemd units pass `systemd-analyze verify`
(local installs reproduce the production behaviour). Live install
verified by `systemctl --user list-timers | grep sora-bridge-sync`.

**Generalisation**: When a daemon runs out of a long-lived working
tree (any clone that the operator pulls into manually rather than
rebuilds from CI), liveness checks are necessary but not sufficient.
Pair every long-lived clone with a freshness watchdog that (a) runs on
a cadence equal to or shorter than the daemon's heartbeat, (b) only
restarts on changes that actually affect runtime behaviour, and (c)
never clobbers operator debug edits. The two signals — alive AND
fresh — both have to be green before the daemon can be trusted. This
applies to any future "shadow clone" deploys (staging mirrors, hotfix
branches, parallel runner stacks) as well as the bridge.
