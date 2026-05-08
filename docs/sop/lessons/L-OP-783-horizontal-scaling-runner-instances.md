---
id: L-OP-783
ticket: OP-783
title: Horizontal-scale runners by giving each instance its own bot account, not its own process
date: 2026-05-08
tags: [runner, scaling, devops]
---

# Horizontal-scale runners by giving each instance its own bot account, not its own process

**Situation**: The 2026-05-08 30-min benchmark with 2 codex runner
processes sharing a single `codex-bot` account showed ~1.5–2× throughput
gain (6 merges in 31 min vs 3–4 single-instance baseline) but exposed
three structural risks: (a) a 5–10s pickup race window where both
processes pass `pre_pickup_ok` on the same ticket before the JIRA
atomic-update lands; (b) the shared subscription's 5h cap drains 2×
faster, pausing both processes simultaneously; (c) the per-bot-account
backpressure cap makes both processes pause at the same review-queue
depth even though each is independent. Audit-trail attribution was also
lost: every commit looked authored by the same bot.

**Fix**: Instance identity is the bot account, not the OS process.
`auto-runner-jira.py` reads `OMNISIGHT_RUNNER_INSTANCE_ID`; the
default value preserves the legacy `codex-bot` / `claude-bot`
single-instance setup byte-for-byte, while non-default IDs (`2`, `3`,
...) resolve to per-bot JIRA cred files (`jira-codex-bot-2.env`),
Gerrit SSH keys (`gerrit-codex-bot-2-ed25519`), worktrees
(`OmniSight-codex-bot-2-worktree`), backpressure state files, and
idempotency DBs. JIRA's single-valued `assignee` field becomes the
race-decider across distinct bot identities, eliminating the pickup
window structurally. Per-bot backpressure (`open_ps_count_for(bot)`
keyed on the per-instance username) means `codex-bot-2` saturating its
review queue does not pause `codex-bot` or `codex-bot-3`. The launcher
script `scripts/launch_runner_instance.sh <bot>` is idempotent (re-runs
short-circuit on `tmux has-session`); the systemd templates
`runner-codex@.service` / `runner-claude@.service` carry the same
contract for persistent supervised deployments.

**Verification**: 39 new tests in
`backend/tests/test_runner_multi_instance.py` pin (i) bot-username
resolution for default vs non-default instances, (ii) cred + state-file
path scoping (no two instances ever share a path), (iii) `backpressure_decide`
threading the right `bot_username` into Gerrit queries, (iv) the runner
honoring `OMNISIGHT_RUNNER_INSTANCE_ID` from env, (v) the launcher
script's idempotency guard + per-bot env propagation, and (vi) the
systemd unit templates wiring `%i → INSTANCE_ID`. Existing
`test_runner_backpressure.py` regression tests pass after lambda-arity
relaxation. Default-instance behaviour is provably backwards-compat:
`_cred_paths("subscription-codex", "default")` returns
`(jira-codex.env, jira-codex-token)` — byte-identical to pre-OP-783.

**Generalisation**: When you need to horizontally scale a singleton
worker with a JIRA / Gerrit / API auth boundary, **scale the
identity, not the process**. Two processes under one identity share
the identity's quota, race for the same atomic-claim, and produce an
indistinguishable audit trail. N identities under N processes have
none of those problems and the cost is the bot-account provisioning
runbook (~30 min one-time per identity). The backwards-compat trick:
reserve `INSTANCE_ID="default"` for the legacy bare-named identity so
existing single-instance setups need zero migration; new instances
opt in via the env var.
