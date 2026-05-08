---
id: L-OP-713
ticket: OP-713
title: Gerrit webhooks plugin reads `webhooks.config`, NOT `project.config`
date: 2026-05-07
tags: [ci, events, gerrit, runner]
legacy_lesson: 22
---

# Gerrit webhooks plugin reads `webhooks.config`, NOT `project.config`

**Situation**: OP-713 Phase 1 added a new `[remote "ai-reviewer-webhook"]` block to `project.config` on `refs/meta/config` (mirroring how OP-708 placed `[remote "merge-conflict-webhook"]` there). Pushed cleanly, plugin reload succeeded, projects cache flushed. Pushed 3 separate test patchsets (#80 patchsets 1-3) — **0 webhook calls reached the backend**. Caddy log was silent. Backend log was silent. Gerrit reported the patchsets as created normally.

Root cause: per Gerrit webhooks plugin docs at `/plugins/webhooks/Documentation/config.html`: *"The webhooks plugin's per project configuration is stored in the **webhooks.config** file in project's refs/meta/config branch."* `project.config` is for ACL / submit-rules / labels — the webhooks plugin never reads it. The `[remote "..."]` blocks in `project.config` from OP-708 + OP-713 v1 were silently no-op.

Implication: OP-708's verification (synthetic `curl` direct to `/api/v1/orchestrator/merge-conflict`) didn't actually exercise the Gerrit-delivery layer, so the OP-708 merger webhook had **never been confirmed working through real Gerrit events** prior to OP-713 — we just thought it had.

**Fix**: Move all `[remote "..."]` blocks out of `project.config` and into a new `webhooks.config` file on `refs/meta/config`. Single commit handled both:
1. Remove the OP-713 v1 ai-reviewer block from `project.config` (it was the orphan I just added)
2. Add `webhooks.config` containing both the merger block (preserved from OP-708) AND the new ai-reviewer block

Pushed as sora; webhooks plugin auto-reloaded.

**Verification**: Pushed test patchset #81 → Caddy logged `POST /api/v1/webhooks/gerrit ... User-Agent: Apache-HttpClient/4.5.14 (Java/21.0.10) ... X-Origin-Url: https://sora.services:29420/ ... bytes_read: 1181 ... status: 401`. Confirmed Gerrit IS now firing patchset-created events. The 401 is a separate auth issue (Lesson 23).

**Generalisation**: When a Gerrit plugin doesn't behave as configured, check whether the canonical config file is `project.config` or a plugin-specific file. Gerrit's plugin API allows plugins to read their own files from `refs/meta/config`. Always grep the plugin's `Documentation/` URL for the exact filename before assuming it's project.config. Webhook plumbing must always be verified by a real Gerrit-driven event (push a throwaway patchset), not by synthetic curl to the backend endpoint — the latter only proves the receiver works, not the sender → receiver chain.
