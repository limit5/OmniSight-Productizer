---
id: L-OP-689
ticket: OP-689
title: Gerrit webhooks plugin v3.13.5 has zero auth surface; OP-689 daemon never deployed
date: 2026-05-07
tags: [ci, events, gerrit, jira, runner]
legacy_lesson: 24
---

# Gerrit webhooks plugin v3.13.5 has zero auth surface; OP-689 daemon never deployed

**Situation**: After OP-714 deploy, every patchset-created event from Gerrit returned 401 to the backend even with the new dual-header (Bearer + X-Jira-Webhook-Secret) auth model in place. Caddy log header dump showed Gerrit was sending NO Authorization header, NO X-Jira-Webhook-Secret header, NO X-Gerrit-Signature header — just the body. Cross-referenced the plugin's docs at `/plugins/webhooks/Documentation/config.html`: ZERO occurrences of `header`, `auth`, `bearer`, `signature`, `secret`, or `algorithm`. The plugin literally cannot send credentials. So OP-708's merger webhook (`header = Authorization: Bearer ...` lines) and OP-714's auth refactor were both structurally impossible to satisfy.

While evaluating OP-715 (the migration plan to stream-events), a second silent failure surfaced: `systemctl status gerrit-jira-bridge` → "Unit could not be found." The OP-689 daemon's systemd unit (`deploy/systemd/gerrit-jira-bridge.service`) was checked into the repo but NEVER INSTALLED on the prod host. No tmux session running it either. So the intended `change-merged → JIRA Approved → 承認済み` transitions had never actually fired automatically — every JIRA ticket marked as published was transitioned manually (operator) without realising the automation was a no-op.

**Fix**: OP-715 replaces the webhook path with stream-events (already an SSH-authenticated channel) for the proactive merger trigger:
- `backend/agents/gerrit_jira_bridge.py:process_stream_event` extended to dispatch on `patchset-created` in addition to `change-merged`.
- New `_handle_patchset_created` spawns a daemon thread that runs `_proactive_merger_check` (kept in webhooks.py so a future webhook-auth fix and the daemon can share one implementation).
- OP-714's `Depends(require_operator)` + `_verify_jira_signature` removed from `/webhooks/gerrit` (auth was structurally unsatisfiable; the security boundary moves to Caddy/Cloudflare).
- Operator action: install the systemd unit (`sudo cp deploy/systemd/gerrit-jira-bridge.service /etc/systemd/system/` + `systemctl enable --now`) — this also lights up OP-689's pre-existing path that was silently dead.

**Verification**: 21 existing bridge tests + 6 new patchset-created tests + 14 OP-714 proactive-check tests all green (41 total). Live verification deferred until daemon is installed and a test patchset triggers the proactive flow.

**Generalisation**:
- "The plugin probably supports X like adjacent feature Y" is wishful thinking. Treat plugin docs' enumerated config keys as the EXHAUSTIVE list — undocumented keys are silently dropped.
- Don't trust that a daemon "exists in the repo" means it's running. Add a `systemctl is-active <unit>` line to deploy verification scripts; flag any service whose unit file is checked in but not installed.
- For Gerrit specifically: if you need authenticated event ingest, prefer `gerrit stream-events` over the webhooks plugin. The SSH transport handles auth at the protocol layer with no plugin-level config gymnastics.
- Webhook plumbing must always be verified by a real Gerrit-driven test event, not a synthetic curl — the latter only proves the receiver works, not the sender → receiver chain. (Repeated from L22.)
