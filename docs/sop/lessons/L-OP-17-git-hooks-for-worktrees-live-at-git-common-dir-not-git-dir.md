---
id: L-OP-17
ticket: OP-17
title: Git hooks for worktrees live at `--git-common-dir`, not `--git-dir`
date: 2026-05-06
tags: [ci, gerrit, git, jira, runner]
legacy_lesson: 14
---

# Git hooks for worktrees live at `--git-common-dir`, not `--git-dir`

**Situation**: OP-17 first auto-push run via `auto-runner-jira.py`'s OP-247 Phase 1 logic. `install_commit_msg_hook` resolved the hook path via `git rev-parse --git-dir` and wrote the script there. But codex's commits had no Change-Id; Gerrit rejected the push with `missing Change-Id in message footer`. Manual debug: ran the hook script directly with a test message → it worked. So the script was correct; git just wasn't invoking it.

**Fix**: For Git worktrees, `--git-dir` returns the worktree-specific path (`.git/worktrees/<name>`), but git executes hooks from `--git-common-dir/hooks` (the parent's `.git/hooks`). Hooks installed at the worktree-specific path silently never fire. `install_commit_msg_hook` now uses `_git_common_dir()` which calls `git rev-parse --git-common-dir`. Commit `83e89baa` (Change #28).

**Verification**: Post-fix, `git commit --amend --no-edit` triggers the hook and adds Change-Id. Confirmed by manual hook-trigger test before-after the path correction.

**Generalisation**: Any tool that resolves git paths for worktree-shared resources (hooks, refs, config) needs to use `--git-common-dir`, not `--git-dir`. The two are interchangeable only for non-worktree repos. When integrating with worktrees, audit every git path lookup.
