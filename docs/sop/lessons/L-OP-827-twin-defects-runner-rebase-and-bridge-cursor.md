---
id: L-OP-827
ticket: OP-827
title: Twin-defect post-mortem — runner rebase precondition + bridge cursor file
date: 2026-05-11
tags: [bridge, runner, gerrit, jira, post-mortem, reliability, state-machine]
related_tickets: [OP-811, OP-813, OP-829, OP-831]
---

# Twin-defect post-mortem — runner rebase precondition + bridge cursor file

**Situation**: OP-811 (A3 — Skill/Agent wired) and OP-813 (A5 — MCP-JIRA integration) sat at "In Progress / claude-bot" for 2 days 16 hours (2026-05-09 08:39 → 2026-05-11 01:24). Both tickets had AC-verification comments — the work *appeared* done. Audit on 2026-05-11 found 0 commits on either feature branch (`feature/OP-811-runner-fresh` + `feature/OP-813-runner-fresh` both still pointing at base SHA `f5046897`); the worktree had been overwritten by a later ticket pickup, so the actual files claude wrote were gone.

Root cause analysis turned up TWO independent defects in two layers, sharing a structural pattern:

* **Defect A (runner)**: claude CLI implemented + tested + posted AC, then exited without making a commit. Runner's `ensure_change_ids` ran `git rebase <base> --exec 'git commit --amend --no-edit'` against an empty branch with a dirty worktree; git's failure shape is non-deterministic in that case (rc=1 with a generic `CalledProcessError` message). The runner's catch-all `except Exception` at the post-CLI Gerrit-prep step posted `[runner-gerrit-setup-fail]` with the raw exception text and exited rc=1 — leaving the ticket as orphan In Progress with no recovery hook. Subsequent runner ticks skipped the ticket because the JQL filters on `status = "To Do" AND assignee is EMPTY`, so the ticket stayed wedged silently.

* **Defect B (bridge)**: while diagnosing A, found the bridge daemon was in a hidden crash loop. `CURSOR_FILE = Path("/var/lib/omnisight-bridge/event-cursor.json")` was hard-coded; user-level systemd lacks permission to `mkdir` under root-owned `/var/lib/`. Each restart cycle: catchup → enter `stream_forever()` → first event arrives → `save_event_cursor()` → `mkdir` raises `PermissionError` → process dies → `Restart=always` respawns. ~7 seconds per cycle. Live `change-merged` events streamed past unprocessed for ~16 minutes (01:08–01:24); JIRA tickets stayed at "Under Review" even though their Gerrit changes had merged. The catchup pass at startup was stateless (no cursor write needed) and so succeeded every cycle, masking the inner failure behind a log of repeated `catchup_done` entries.

The two defects are independent in cause but share a pattern: **non-deterministic FS/git error at a state boundary, swallowed by a catch-all, with no recovery primitive to restore known-good state**. They also share a discovery shape: "ticket *appears* stuck even though work is done" — defect A causes the wedge upstream (runner side), defect B causes the wedge downstream (bridge side). One incident, two fixes needed.

**Fix**:

* **OP-827** — typed exceptions in `backend/agents/jira_dispatch.py::ensure_change_ids`:
  * `NoCommitsOnBranchError(base_ref, head)` — raised when `git rev-list base..HEAD --count` returns 0
  * `WorktreeDirtyError(dirty_files)` — raised when `git status --porcelain` returns non-empty
  * Two preconditions checked *before* `git rebase` runs, so the recovery path can route by structural cause rather than parsing rebase output.
  * `auto-runner-jira.py` catches each typed exception specifically: posts `[runner-no-commits-from-cli]` / `[runner-dirty-worktree]` comment + calls `transition_back_to_todo` to revert + clear assignee, then returns rc=1. Generic `except Exception` catch remains as fallback for genuinely unknown paths.

* **OP-831** — `OMNISIGHT_BRIDGE_CURSOR_FILE` env override on `CURSOR_FILE` in `backend/agents/gerrit_jira_bridge.py`. The legacy `/var/lib/` path remains the fallback default so root-installed deployments stay backward compatible. `deploy/systemd/gerrit-jira-bridge.service` ships with the override pre-set to `~/.local/state/omnisight-bridge/event-cursor.json` (XDG-compliant, user-writable for user-level systemd). Live operator-side mirror applied first to `/home/user/sora-bridge/...` so the production bridge stopped crash-looping immediately, then committed canonically to the main repo for future deploys.

**Verification**:

* OP-827 — `backend/tests/test_jira_dispatch.py::test_ensure_change_ids_*` (5 new + 2 updated): empty-branch raises `NoCommitsOnBranchError`, dirty-worktree raises `WorktreeDirtyError`, happy path runs rebase, diagnostic text mentions the CLI-without-commit cause, dirty-file-list truncates at 5 entries. Existing rebase-shape + cleanup tests updated to mock the precondition probes via `_fake_passing_preconditions_run`.
* OP-831 — `backend/tests/test_gerrit_jira_bridge.py::test_cursor_file_*` (2 new): env override honoured + default preserved when env unset. All 39 existing bridge tests still pass.
* Live runtime confirmation: bridge alive 30+ minutes after restart with the env override (was crashing every 7 seconds). On next runner tick after Phase 1 children were filed, codex CLI self-detected a scope conflict on OP-829 and reverted via the JIRA helper before the runner's post-CLI step ran — when the runner *then* called `ensure_change_ids`, the new `NoCommitsOnBranchError` fired and the runner's typed-exception handler issued a defensive second revert (idempotent with codex's own revert). Both fixes verified in sequence on a real ticket without operator intervention.

**Generalisation**:

1. **Stateful daemons whose persistence file is unwritable are silent killers.** `Restart=always` masks the crash as continuous uptime; a stateless catchup pass succeeds every cycle and prints reassuring `catchup_done` logs. Mitigation: every daemon's startup should write a smoke token to its persistence path *before* entering its main loop, and abort loudly (non-restart-able exit code) if the write fails. systemd should not be the only thing that knows the daemon is broken.

2. **Generic `except Exception` at a state-boundary mutation is a wedge breeder.** The runner had one catch-all that posted "operator review needed" — but no automated recovery (no revert, no clear assignee, no retry). Result: 2.5-day orphan In Progress. Replace each catch-all at a state boundary with: typed-exception classification per known failure mode + an explicit "unknown" path that *still* does state restore (revert to known-good) before exiting rc=1. The recovery action is the contract; the diagnostic message is secondary.

3. **Workflow primitives that mutate external state must validate their preconditions structurally, not via the underlying tool's error.** `ensure_change_ids`'s pre-fix shape was "try `git rebase`, catch error". `git rebase`'s error shape is non-deterministic — depends on which precondition violated, on git version, on whether stdout/stderr was captured. The fix shape is "assert each precondition independently with deterministic shell commands, raise typed exception if any fail, *then* run the mutating command". FS/git/network commands cannot serve as the validation layer for the workflow above them.

4. **Two-layer "appears stuck" failure modes need TWO fixes, not one.** Don't scope a fix to only the visible symptom. The runner fix (OP-827) alone would leave bridge deaf to merges; the bridge fix (OP-831) alone would leave orphan-In-Progress wedges. Catalogue the failure mode by *all* layers it touches before scoping the fix — for "stuck ticket" the layers are: (a) runner state machine, (b) JIRA state, (c) Gerrit change state, (d) bridge consumer health. Any one defective and the system "looks stuck" identically. Triage by surface symptom is necessary but not sufficient.
