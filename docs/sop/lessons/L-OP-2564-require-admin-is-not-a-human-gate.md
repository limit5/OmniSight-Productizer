---
id: L-OP-2564
ticket: OP-2564
title: require_admin is not a human gate — api-key principals hold role=admin
date: 2026-07-10
tags: [security, auth, prompt-injection, self-improve, backend]
---

# require_admin is not a human gate — api-key principals hold role=admin

**Situation**: `auth.current_user` assigns `role="admin"` to ANY valid
api-key bearer, so both learned-skill promote endpoints
(`routers/auto_skills.py::promote_auto_skill`,
`routers/skills.py::/pending/{name}/promote`) gated only on `require_admin`
were reachable by bot principals — including a prompt-injected runner. With
`OMNISIGHT_SELF_IMPROVE_LEVEL=l1` producers active, any api-key holder could
promote arbitrary distilled Markdown into the globally injected
`configs/skills/` prompt set with zero eval (memory-poisoning escalation).

**Fix**: Single chokepoint
`backend/learned_item_interlock.py::assert_promotion_allowed(user)` =
`_assert_human_operator` (403 `auth_refused` for `apikey:*`/`*-bot`/`ai-*`/
`ci-*`) first, then deny-by-default 403 `promotion_disabled` unless
`OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED` is explicitly truthy. Both
endpoints call it before any mutation.

**Verification**: `backend/tests/test_learned_item_interlock.py` — AC matrix
(human+flag allow; human+no-flag `promotion_disabled`; apikey-bot
`auth_refused` with/without flag) plus inventory guard
`test_every_skills_live_writer_routes_through_interlock` asserting every
non-test `_SKILLS_LIVE`-referencing module routes through the interlock.

**Generalisation**: Any endpoint whose effect feeds future agent prompts or
approves agent-proposed actions needs BOTH a human-only principal check
(role checks are satisfied by api keys by design) AND a deny-by-default
capability flag until its eval/review substrate exists. When you add such a
write surface, add an inventory guard test keyed on the surface's symbol so
the next ungated writer fails CI instead of shipping.
