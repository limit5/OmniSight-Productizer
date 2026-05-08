---
id: L-OP-708
ticket: OP-708
title: Webhook endpoints with `require_operator + signature` are dual-gate, not redundant
date: 2026-05-07
tags: [ci, events, gerrit, jira, runner]
legacy_lesson: 21
---

# Webhook endpoints with `require_operator + signature` are dual-gate, not redundant

**Situation**: OP-708 surfaced that backend's `/api/v1/orchestrator/merge-conflict` endpoint requires BOTH a valid Bearer api_key (passes `require_operator`) AND a separate `X-Jira-Webhook-Secret` header (passes `_verify_jira_signature`). Gerrit's webhooks plugin natively supports `secret` for HMAC body signing into `X-Hub-Signature`, but the backend doesn't verify HMAC of the body — it does a constant-time string compare against `settings.jira_webhook_secret`. So Gerrit needs to set TWO custom headers, not one HMAC signature.

**Fix**: Gerrit webhooks plugin supports `header = Name: Value` lines in `[remote "..."]` blocks (Gerrit 3.13 confirmed via project.config push). Set:
```
[remote "merge-conflict-webhook"]
  url = ...
  event = change-merge-failed
  header = Authorization: Bearer <api_key>          # require_operator
  header = X-Jira-Webhook-Secret: <jira_webhook_secret>  # _verify_jira_signature
```

Both go in refs/meta/config (admin-only readable), so secret-in-config is acceptable for this deployment topology.

**Verification**: External curl test (`curl -X POST -H Authorization -H X-Jira-Webhook-Secret https://ai.sora-dev.app/api/v1/orchestrator/merge-conflict`) returned HTTP 200 — endpoint accepted both headers, validated payload, dispatched to merge_arbiter. Direct in-container invocation of `merge_arbiter.on_merge_conflict_webhook` also confirmed the merger code path runs (with downstream LLM bug found, separate ticket OP-709).

**Generalisation**: When an endpoint name suggests "webhook" but auth requires user/operator credentials, the design intent is dual-gate (defense-in-depth: signature isolates abuse from external internet + user auth provides identity for audit). Don't simplify to "just signature" without team agreement — the operator identity is sometimes load-bearing for audit_log entries / per-user rate limits / etc. For Gerrit-side, configure custom headers; don't try to bend Gerrit's HMAC `secret` into the user-auth shape.
