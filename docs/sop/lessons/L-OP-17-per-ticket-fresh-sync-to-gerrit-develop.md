---
id: L-OP-17
ticket: OP-17
title: Per-ticket fresh-sync to Gerrit develop
date: 2026-05-06
tags: [ci, gerrit, git, jira, runner]
legacy_lesson: 16
---

# Per-ticket fresh-sync to Gerrit develop

**Situation**: OP-17 launch. The codex worktree was on `feature/OP-11-mp-w1-1-orchestrator` from the previous ticket; pre-pickup live_state checks in main repo cwd failed because main repo lacked the OP-11 module (chain divergence between local `main` and Gerrit `develop`). Manual fix: `git checkout -B develop FETCH_HEAD` in worktree, then `git merge --no-ff origin/develop` in main repo. After codex completed work, `ensure_change_ids` rebased onto local `main` which now contained a merge commit with row7-self-agent committer → Gerrit rejected (separate from L15's same-error-different-cause).

**Fix**: `sync_to_gerrit_develop(worktree, agent_class, ticket_key)` runs at the start of every ticket pickup in `auto-runner-jira.py`:

```python
1. git fetch <gerrit-ssh-url> develop      # canonical source
2. develop_sha = git rev-parse FETCH_HEAD  # capture explicitly
3. git switch -C feature/<TICKET>-runner-fresh <develop_sha>
4. git clean -fdx                          # discard any partial state
```

Returns `WorktreeSyncResult(branch_name, develop_sha, detail)`. Caller passes `develop_sha` to `ensure_change_ids` so rebase base is the Gerrit develop tip (not local main). Commit `83e89baa`.

**Verification**: After Phase 1.5 ships, OP-18 (next codex run) is the first ticket expected to need zero manual recovery. Verification deferred to that run.

**Generalisation**: Local refs (especially `main`) drift from canonical refs (Gerrit develop) over time. Rather than tracking + repairing drift, automate a fresh-sync at every ticket boundary. The workflow's source of truth is Gerrit; any local repo state is throwaway. Aligns with ADR 0001 5-branch flow ("feature/* are throwaway, develop is integration trunk").
