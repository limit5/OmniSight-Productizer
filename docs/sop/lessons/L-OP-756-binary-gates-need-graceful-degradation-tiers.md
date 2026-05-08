---
id: L-OP-756
ticket: OP-756
title: Binary auto-action gates need graceful degradation tiers
date: 2026-05-08
tags: [ai-reviewer, gerrit, runner, trust]
---

# Binary auto-action gates need graceful degradation tiers

**Situation**: OP-735 R5 implemented per-(bot, file_class) trust scoring
as a binary on/off gate (`trust_score_ok` returned True iff failure_rate
was below 10%). Crossing the threshold permanently disabled auto-+1 for
that pair; recovery required enough additional successes to drop the
running rate back under 10%, with no operator override path. A single
late-failing patch could mute a bot for an entire file class until the
counter caught up.

**Fix**: OP-756 replaces the binary gate with a 4-tier ladder
(`AUTO` >= 0.95, `GLANCE` >= 0.80, `COMMENT` >= 0.50, `DISABLED` < 0.50).
A failure now slides the pair to a lower-automation tier instead of
flipping the bot off completely: GLANCE keeps the +1 vote with a
`runner-glance-required` hashtag for one-click operator confirmation;
COMMENT mutes the +1 but still posts the LLM verdict for context.
DISABLED only triggers when the success rate falls below 50%.
Operators can short-circuit the score-derived tier with a
`runner-trust-tier=<tier>` Gerrit hashtag so a known-bad pair can be
pinned to COMMENT/DISABLED for debugging without poisoning the counters.

**Verification**: `backend/tests/test_ai_reviewer_auto_plus_one.py`
adds 28 OP-756 tests pinning tier boundaries, ramp-up default, operator
pin override, hard-gate downgrade behaviour, recovery semantics, and
the anti-fragility invariant that a single failure can never push a
pair to DISABLED.

**Generalisation**: Whenever an automated agent has a confidence-based
auto-action, design the failure mode as a degradation ladder rather
than a binary kill-switch. The right number of rungs is small
(2–4) and each rung should still produce *some* useful signal so the
bot remains observable while it earns its score back. Always provide
an operator override hashtag/label so debugging doesn't require
manipulating the underlying counters.
