---
id: L-OP-876
ticket: OP-876
title: Grep the tracking ticket in backend/agents/ before adding a webhook subscription
date: 2026-05-11
tags: [ci, events, gerrit, webhooks, runner]
---

# Grep the tracking ticket in `backend/agents/` before adding a webhook subscription

**Situation**: OP-876 spike was scoped around a hypothesis that
OP-733's `change-merged` consumer had never fired in production
because the matching webhook subscription was missing from
`project.config`. Static reading revealed OP-733 had been migrated to
the SSH stream-events daemon as part of the OP-715 cleanup (the same
cleanup that disclaimed all webhook-side auth in
`backend/routers/webhooks.py:218-234`). The Q2 follow-up would have
added a `[remote "auto-rebase-webhook"]` entry to staging
`webhooks.config`, triggering a double-fire (daemon + webhook both
running the sweep on every `change-merged`) — a real production
hazard masked by the convincingly-worded ticket.

**Fix**: before authoring any webhook-subscription change for a
feature with a tracking ticket, run `grep -rn "OP-<n>" backend/agents/
backend/routers/` to surface every existing wiring of that ticket.
If a daemon module already owns the trigger, the webhook
subscription is redundant at best and a double-fire at worst.
Specifically for Gerrit `change-merged` / `patchset-created` /
`change-merge-failed`, check
`backend/agents/gerrit_jira_bridge.py` first — the SSH stream-events
daemon is the canonical consumer post-OP-715, and the HTTP webhook
is a secondary/legacy path.

**Verification**: `scripts/spike_merger_proactive_trigger.py
--mode=static` runs the three Q2 wiring checkpoints and
`tests/integration/test_merger_spike.py::test_q2_static_finds_op733_in_ssh_bridge`
pins them. The smoke test fails if a future refactor moves OP-733
back into the webhook handler without updating the docs.

**Generalisation**: A ticket's literal phrasing reflects the author's
mental model at write-time, which may already be obsolete by the
time the agent picks it up. Treat ticket hypotheses as **leads, not
specifications**. The first action on any "add subscription" /
"missing wiring" ticket is a 30-second grep for the feature's
tracking ID across the agent + router layer — cheap insurance
against shipping a duplicate consumer.
