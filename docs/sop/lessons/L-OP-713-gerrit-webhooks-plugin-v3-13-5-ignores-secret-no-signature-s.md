---
id: L-OP-713
ticket: OP-713
title: Gerrit webhooks plugin v3.13.5 ignores `secret = ...` (no signature support)
date: 2026-05-07
tags: [ci, events, gerrit, runner]
legacy_lesson: 23
---

# Gerrit webhooks plugin v3.13.5 ignores `secret = ...` (no signature support)

**Situation**: After Lesson 22, with `webhooks.config` correctly configured and Gerrit firing patchset-created events at `/api/v1/webhooks/gerrit`, every event still 401'd. Inspected Caddy log header dump for the inbound request — Gerrit sent NO `X-Gerrit-Signature` header and no other signature-shaped header. The `secret = <hmac>` line in the `[remote "..."]` block was being silently ignored by the plugin. Re-read the plugin's `Documentation/config.html` more carefully — the only documented `[remote "..."]` keys are `url`, `event`, `connectionTimeout`, `socketTimeout`, `maxTries`, `retryInterval`, `sslVerify`. **`secret` is not in the documented schema** of v3.13.5.

**Fix** (deferred to OP-713 Phase 2 — backend-side wiring): use the same `header = Name: Value` pattern that OP-708 used for the merger block (which IS supported per Lesson 21). Two viable shapes:
- `header = Authorization: Bearer <api_key>` → check via `require_operator` (matches merger pattern, mature path)
- `header = X-Gerrit-Webhook-Secret: <secret>` → constant-time compare in custom validator

Either way, drop the HMAC-signature path in `backend/routers/webhooks.py:240-287` for Gerrit since the plugin can't generate one. Patch will become OP-713 Phase 2's first commit.

**Verification**: To be measured in Phase 2 — same test patchset pattern (push throwaway change to `refs/for/develop`), expect 200 instead of 401 in Caddy log.

**Generalisation**: Don't trust documentation-by-analogy. The merger block's `header = ...` worked, and I assumed the obvious adjacent feature `secret = ...` would also work. It doesn't. When a plugin's docs explicitly enumerate config keys, treat that list as exhaustive — undocumented keys are silently dropped, no warning. For HMAC body signing on Gerrit webhooks, you need either a different plugin (e.g. `events-broker`) or a custom proxy in front of the backend.
