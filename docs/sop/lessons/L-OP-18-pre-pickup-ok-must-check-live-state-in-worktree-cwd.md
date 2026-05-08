---
id: L-OP-18
ticket: OP-18
title: `pre_pickup_ok` must check live_state in worktree cwd
date: 2026-05-06
tags: [ci, gerrit, git, jira, runner]
legacy_lesson: 17
---

# `pre_pickup_ok` must check live_state in worktree cwd

**Situation**: After Phase 1.5 shipped (commit `83e89baa`, Change #28), launching codex on OP-18 failed at pre-pickup with `file_exists: backend/agents/provider_adapters/__init__.py: MISSING`. The file existed on Gerrit develop (shipped via OP-17 = Change #27) but main repo's local `main` branch hadn't been merged yet. `live_state_check.evaluate` ran in `REPO_ROOT` cwd (the runner host's main repo), saw stale state, blocked pickup.

The `sync_to_gerrit_develop` shipped in Phase 1.5 fresh-syncs the *worktree* but not the runner-host repo. They serve different roles: worktree is where codex commits land; main repo is where `auto-runner-jira.py` runs from. Pre-pickup checks were running against the wrong cwd.

**Fix**: `live_state_check.evaluate(requirements, cwd=Path | None)` — handlers (`alembic_head`, `file_exists`, `command_succeeds`) accept a `cwd` parameter. Backward-compatible default (`cwd=None`) falls back to `REPO_ROOT`.

`jira_dispatch.pre_pickup_ok(client, snapshot, worktree_path=None)` — optional `worktree_path` propagates to `evaluate(cwd=worktree_path)`.

`auto-runner-jira.py` reorders main loop:
```
1. select ticket
2. resolve worktree_path
3. sync_to_gerrit_develop(worktree_path)  ← MOVED UP from after pre_pickup
4. pre_pickup_ok(client, snapshot, worktree_path=worktree_path)
5. transition + invoke CLI
```

Sync now runs BEFORE pre-pickup, so when pre-pickup queries the live state it sees the worktree just-fresh-synced from Gerrit develop tip.

**Verification**: 7 new tests in `test_live_state_check.py` + `test_jira_dispatch.py`. Pinned that `evaluate(cwd=tmp)` resolves `file_exists` against tmp; `evaluate(cwd=None)` keeps REPO_ROOT default. Pinned `pre_pickup_ok` signature. Empirical: OP-18 retry after the merge fix (before this refactor) succeeded — but ONLY because operator manually merged `origin/develop` into local main. After this refactor, that manual step is unnecessary.

**Generalisation**: When tooling has multiple file-system "vantage points" (runner host, agent worktree, Gerrit develop, GitHub mirror, GitLab mirror), every check needs to specify which vantage it queries. Defaulting to "the runner's cwd" is correct for runner self-introspection but wrong for "what's the agent's actual workspace state". The cwd parameter makes this explicit, bypasses the silent ambiguity.
