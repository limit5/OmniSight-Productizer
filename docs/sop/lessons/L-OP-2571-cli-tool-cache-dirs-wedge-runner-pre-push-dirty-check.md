---
id: L-OP-2571
ticket: OP-2571
title: CLI tool-cache dirs wedge the runner pre-push dirty-check — .gitignore them
date: 2026-07-10
tags: [runner, git, gerrit, ci, jira]
---

# CLI tool-cache dirs wedge the runner pre-push dirty-check — .gitignore them

**Situation**: The claude CLI leaves an untracked `.cache/` (and, on broad
sweeps, `.ruff_cache/` / `.pytest_cache/`) in the ephemeral runner worktree
after a completely green build. On the pre-push path,
`backend/agents/jira_dispatch.py::ensure_change_ids` runs `git status
--porcelain` and, finding those untracked paths, raises `WorktreeDirtyError`
(`jira_dispatch.py:1765`) — same OP-827 lineage that distinguishes "wrote
files but never committed" from "made commits, rebase them". Because the
rebase-with-`--exec` refuses to run, a finished, tests-passing change never
gets its Change-Id and never pushes; the caller routes the "dirty" branch to a
§11-revert. The tell in the runner log is the message from
`WorktreeDirtyError.__init__` (`jira_dispatch.py:1690-1691`): `worktree has N
uncommitted path(s); rebase refuses to run. First few: ['.cache/']` with rc=1
arriving immediately after a clean CLI report — the work is real, only the
tool cache is dirty.

**Fix**: `.gitignore` the tool-cache directories so `git status --porcelain`
never sees them. Gerrit #2050 added `.cache/` at `.gitignore:169-170` with the
provenance comment `# OP-2571: claude CLI session cache — untracked .cache/
wedged the runner dirty-check pre-push`; `.pytest_cache/` was already ignored
(`.gitignore:101`). The dirty-check is deliberately fail-closed and must stay
that way — the correct move is to stop generating tracked-looking noise, not to
soften the gate.

**Verification**: After the ignore, the same broad-pytest ticket that reverted
re-picked and pushed cleanly (`.cache/` no longer appears in `git status
--porcelain`, so `ensure_change_ids` reaches the rebase and stamps the
Change-Id). The pre-existing `backend/tests/test_jira_dispatch.py` coverage of
the dirty-vs-committed routing (the OP-827 lineage that `WorktreeDirtyError`
docstring cites) is unchanged — this fix removes an input to that gate rather
than altering the gate.

**Generalisation**: A fail-closed "is the worktree clean?" pre-push gate treats
any untracked file as lost work, so it is only as trustworthy as the repo's
ignore hygiene. Every tool a runner or CLI can invoke — linters, test runners,
package managers, the agent CLI itself — that scribbles a cache into CWD is a
latent revert waiting for the first build that happens to trigger it. Ignore
tool caches (and confirm it with `git status --porcelain` on a throwaway
worktree after a real run), and be aware that runners doing broad `pytest` /
`ruff` sweeps are the most exposed because they materialise the most cache
directories. Do not loosen the dirty-check to tolerate junk; keep the invariant
"clean worktree = safe to push" and make the junk invisible.
