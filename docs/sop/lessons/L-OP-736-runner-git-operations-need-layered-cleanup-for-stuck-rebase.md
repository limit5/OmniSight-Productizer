---
id: L-OP-736
ticket: OP-736
title: Runner git operations need layered cleanup for stuck rebase state
date: 2026-05-08
tags: [ci, git, jira, runner]
legacy_lesson: 26
---

# Runner git operations need layered cleanup for stuck rebase state

**Situation**: OP-167 left an interactive rebase state in the codex worktree after `ensure_change_ids()` rebased an empty verification commit with `git rebase <base> --exec "git commit --amend --no-edit"`. Modern git pauses on empty commits in that mode. The exception path posted a JIRA failure comment but did not quit the rebase, so later runner ticks repeatedly failed at `git switch -C ...` with "cannot switch branch while rebasing".

**Fix**: Treat runner worktree state leaks as defense-in-depth, not a single flag fix. Preserve empty commits during Change-Id rebases with `--keep-empty`, quit any failed rebase before re-raising, and run a pre-sync guard before branch operations that detects stale `rebase-merge`, `rebase-apply`, `CHERRY_PICK_HEAD`, `MERGE_HEAD`, `BISECT_LOG`, and `REVERT_HEAD` artifacts and invokes the matching abort/reset command.

**Verification**: OP-736 added `backend/tests/test_jira_dispatch.py` coverage for the rebase command shape, failed-rebase cleanup, all six artifact cleanup mappings, clean-worktree no-op behavior, unrecoverable cleanup failure, and a synthetic regression where a manually created `.git/rebase-merge/` is cleared before `git switch -C` succeeds.

**Generalisation**: Any daemon that reuses a git worktree across ticks must clear operation state before branch-changing commands. A local fix in the operation that caused the state leak is necessary but insufficient; the next tick also needs a guard at the sync boundary because the leak may come from a previous process, manual intervention, hooks, signing failures, or a different git operation.
