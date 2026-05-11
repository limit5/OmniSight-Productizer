---
id: L-OP-837
ticket: OP-837
title: A long-lived runner checkout needs its own freshness watchdog, not just per-ticket fresh-sync
date: 2026-05-11
tags: [ops, runner, watchdog, deploy, sync]
---

# A long-lived runner checkout needs its own freshness watchdog, not just per-ticket fresh-sync

**Situation**: On 2026-05-11 the runner's main checkout at
`/home/user/work/sora/OmniSight-Productizer/` was stuck at `0f52bb9e`
(5/8 D15). The runner code therefore lacked OP-827 typed exceptions +
OP-832 area validation **even though both had been merged on develop
for hours**. Result: OP-829 spun through a 5x revert loop (area:runner
mislabel was not caught because runner code had no
`NoCommitsOnBranchError`, and area-validation rejection wasn't enforced
because runner code had no unknown-label guard). Operator manually
`git reset --hard FETCH_HEAD` + popped untracked stash to recover.
L-OP-17 (per-ticket fresh-sync to Gerrit develop) covers the *worktree*
side correctly — but `auto-runner-jira.py` itself and the
`backend.agents.*` import path are loaded once at runner start from the
main checkout, not refreshed per ticket. Per-ticket fresh-sync fixed
the worktree but never touched the running code.

**Fix**: OP-837 adds an OP-798-style freshness watchdog for the main
checkout: `omnisight-main-sync.{service,timer}` (5-min cadence) +
`scripts/sync_omnisight_main.sh`. Key differences from the
sora-bridge sibling:

1. No daemon to restart — the runner is a bash loop in tmux. Instead
   the script touches a sentinel file and emits a grep-friendly
   `[main-repo-sync] runner-code-changed` log line.
2. Untracked-file preservation. The runner's main checkout routinely
   carries audit docs + drafts; default policy is skip-on-dirty.
   Auto-stash is opt-in via `OMNISIGHT_MAIN_SYNC_AUTO_STASH=1`, with
   stash-pop-conflict treated as a recoverable error (stash preserved,
   exit 5).

**Verification**: `tests/test_sync_omnisight_main.sh` covers all seven
AC #7 scenarios + two auto-stash bonus cases against a temp bare
remote. Install-verify: `systemctl --user list-timers | grep
omnisight-main-sync` shows the timer counting down; first firing emits
a `sync_no_change` JSON line under
`/home/user/work/sora/logs/main-sync/sync.log`.

**Generalisation**: Per-ticket fresh-sync is correct but
*insufficient* when the runtime code itself lives in a long-lived
checkout. Any project where the daemon / runner / agent is loaded once
from a working tree, and where the source of truth (Gerrit develop)
advances independently, needs **two** complementary safeguards:

1. **Per-ticket / per-job fresh-sync** for the workspace where work
   happens (L-OP-17).
2. **Long-lived-checkout freshness watchdog** for the tree where the
   runtime code is loaded from — same shape as OP-798 / OP-837. The
   cadence and relevant-files filter must be tuned per host, but the
   shape (skip-on-dirty default + opt-in auto-stash + consecutive-failure
   notifier) generalises.

Liveness + freshness must both be green before the runtime can be
trusted, regardless of whether the runtime is a daemon (bridge) or a
loop (runner). And the workspace + the runtime each need their own
freshness signal — fresh-syncing only the workspace leaves the runtime
on yesterday's code.
