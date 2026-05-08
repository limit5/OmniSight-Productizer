---
id: L-OP-801
ticket: OP-801
title: When the trigger transport is broken, port ALL its consumers, not just the loudest one
date: 2026-05-09
tags: [gerrit, events, ai-reviewer, runner]
---

# When the trigger transport is broken, port ALL its consumers, not just the loudest one

**Situation**: OP-715 (公開済み 2026-05-07) discovered Gerrit's webhooks
plugin v3.13.5 has zero auth surface (no `secret`, no signature header
— see L-OP-713). It migrated the OP-714 proactive merger from the
broken `/webhooks/gerrit` HTTP path to the existing stream-events SSH
daemon (`backend/agents/gerrit_jira_bridge.py`). The merger went green
on every PS within hours. **But the AI Reviewer (OP-713 / 735 / 756),
which shared the exact same `patchset-created` trigger, was not in the
OP-715 scope and silently stayed on the dead webhook path.** Result:
two days of merged PSes (#266–#270 on develop) with no `Code-Review +1`
from any bot — only the human `sora` casting +2. The gap was
symptomless from the operator dashboard's POV (PSes were merging fine
because human +2 doesn't depend on the AI signal), but the entire
"AI signal-only review" feature was effectively dark in production.

**Fix** (this ticket, OP-801): extend
`gerrit_jira_bridge.py::_handle_patchset_created` to ALSO spawn the AI
Reviewer pipeline in a fire-and-forget daemon thread, mirroring the
OP-715 merger pattern. The shared decision logic lives in a new
`backend/routers/webhooks.py::_ai_reviewer_check` coroutine — same
loop-prevention (`_is_merger_uploader`), same 24 h
`(change_id, revision)` throttle, same L2 notify, same risk-tier
routing into `_run_ai_review`. The two pipelines (merger + AI
Reviewer) run in independent threads so a merger crash doesn't
strand a review and vice versa. The `/webhooks/gerrit` HTTP
endpoint is preserved as-is for synthetic curl smoke tests + as a
landing pad for any future plugin-auth fix.

**Verification**:

- `backend/tests/test_gerrit_jira_bridge.py::test_patchset_created_spawns_ai_reviewer_thread`
- `backend/tests/test_gerrit_jira_bridge.py::test_patchset_created_ai_reviewer_thread_swallows_exception`
- `backend/tests/test_gerrit_jira_bridge.py::test_patchset_created_ai_reviewer_independent_of_merger_failure`
- `backend/tests/test_webhooks.py::TestAiReviewerCheckBridgePath` (4 tests)
- The original `TestAIReviewerWiring` suite still passes against the
  HTTP path, so the `/webhooks/gerrit` endpoint has no regression.
- Live verification: push a synthetic markdown-only PS to
  `refs/for/develop`; bridge log emits
  `ai_reviewer_thread_spawned change=N ps=M` then
  `ai_reviewer_invoked change=I... model=haiku loc=N` within 60 s,
  followed by a `+1` from `claude-bot` on the patchset.

**Generalisation**: When you discover a transport is broken (OP-715
on the webhooks plugin) and migrate one consumer off it, **enumerate
every other feature that subscribes to the same trigger and migrate
them in the same ticket** (or open a fast-follow ticket *immediately*
and attach it to the original retro). It is much cheaper to port two
consumers in one motion (shared pattern, shared review) than to
discover the second consumer is silently broken weeks later.

A simple scan rule: when you migrate code AWAY from an interface, grep
the codebase for every subscriber to that interface (here:
``patchset-created`` event handlers anywhere in
`backend/routers/webhooks.py` — `_on_patchset_created` AND any
companion async task it spawns) and either (a) port them in scope, or
(b) annotate their stale path with a `# TODO(OP-XXX)` linked to the
follow-up ticket so the next reader can't miss it. OP-703 ("Webhook
URL config — Caddy route") had exactly this shape: it survived the
OP-715 cleanup as a structurally obsolete ticket because nobody
re-evaluated it after the auth-gap discovery. We close OP-703 under
this ticket's resolution; future migrations should sweep these
"orphan" tickets pre-emptively.
