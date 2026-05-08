---
id: L-OP-729
ticket: OP-729
title: Runner bot identity must be worktree-local
date: 2026-05-08
tags: [ci, gerrit, git, jira, runner]
legacy_lesson: 25
---

# Runner bot identity must be worktree-local

**Situation**: Claude and Codex runners use sibling worktrees under one main repository. Bare `git config user.email/name` writes from either worktree land in the shared `.git/config`, so the last runner to write wins. On 2026-05-08 this made the other runner commit with the wrong bot email and Gerrit rejected the push with `email address ... is not registered`, leaving tickets stuck in `進行中`.

**Fix**: Enable per-worktree config in the main repo before runner startup:
```bash
git -C /home/user/work/sora/OmniSight-Productizer config core.repositoryformatversion 1
git -C /home/user/work/sora/OmniSight-Productizer config extensions.worktreeConfig true
```
Runner startup now fails fast if this prerequisite is missing, and `set_bot_identity_in_worktree()` writes `user.email/name` via `git config --worktree` so each runner's identity is isolated.

**Verification**: `backend/tests/test_jira_dispatch.py::test_interleaved_worktrees_commit_and_push_with_correct_email` creates two real git worktrees, seeds each bot identity, overwrites shared config, commits in both worktrees, and pushes both branches to a local bare remote with the correct author/committer email. `test_assert_worktree_config_enabled_fails_when_disabled` pins the startup remediation message.

**Generalisation**: Any runner setting per-agent git identity in a shared worktree topology must write to the worktree config layer and fail closed when that layer is disabled. Shared repo config is acceptable for common hooks/remotes, but not for mutable per-agent identity.
