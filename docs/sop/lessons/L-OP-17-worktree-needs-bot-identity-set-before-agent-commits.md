---
id: L-OP-17
ticket: OP-17
title: Worktree needs bot identity set before agent commits
date: 2026-05-06
tags: [ci, events, gerrit, git, runner]
legacy_lesson: 15
---

# Worktree needs bot identity set before agent commits

**Situation**: OP-17 first auto-push run. After commit-msg hook fix (L14), the runner pushed to Gerrit `refs/for/develop` and Gerrit responded `email address row7-self-agent@omnisight.local is not registered in your account, and you lack 'forge committer' permission`. The codex commit's committer was the worktree's default git config user (the operator's env user `Agent-row7-self-agent`), not codex-bot's registered Gerrit email.

**Fix**: `set_bot_identity_in_worktree(worktree, agent_class)` runs `git config user.email` + `user.name` in the worktree before invoking the CLI. Identity is derived from the agent_class via `_GERRIT_AUTH_BY_CLASS` (claude-bot for `subscription-claude` / `api-anthropic`; codex-bot for `subscription-codex` / `api-openai`). Email convention: `rt3628+<bot-username>@gmail.com`. Idempotent — setting same value is a no-op. Commit `83e89baa`.

**Verification**: Post-fix, codex commits have `committer: codex-bot <rt3628+codex-bot@gmail.com>` which matches Gerrit's account. Push succeeds without manual `--reset-author` workaround.

**Generalisation**: Whenever an automated agent commits in a shared workspace, the workspace's identity must match the agent's principal in downstream gates. Setting identity on every agent invocation is cheaper (and idempotent) than relying on workspace-state being correct from prior setup.
