---
id: L-OP-1534
ticket: OP-1534
title: A carve-out's applicability must be keyed identically to the rule that re-imposes the gate
date: 2026-05-20
tags: [gerrit, submit-requirements, security, audit, release]
---

# A carve-out's applicability must be keyed identically to the rule that re-imposes the gate

**Situation**: The OmniSight `project.config` release-cut policy is a *set* of
co-existing submit-requirements, not one rule: `release-cut-promote` (quad-keyed,
merger +2 AND human +2) re-imposes the human gate for a genuine cut, while
`Human-Plus-2` carves that same shape *out* of the unconditional human gate and
`MainFastForwardMergerPlus2` lets a merger-bot +2 alone satisfy it. The carve-out
SRs were keyed more loosely than the re-imposing rule — only `branch:main AND
hashtag:"milestone:R3-fastforward"` (the original OP-962 two keys) — while
`release-cut-promote` was keyed on four. That left an **over-scope window**: a
`branch:main` change that merely carried the hashtag (but had no release-cut
topic and was not owned by sora) fell *out* of `Human-Plus-2` and *into*
`MainFastForwardMergerPlus2` (merger-only), while `release-cut-promote` did NOT
apply — so a single merger-bot +2 could submit it with no human +2, defeating
the CLAUDE.md L1 hard gate. A second, separate trap: the topic atom used
`topic:` (Gerrit **exact-match**), so the intended "topic is a release cut"
predicate silently never fired for any real `release-cut-…` topic.

**Fix**: Re-key all three SRs onto a SINGLE canonical four-key release-cut
signature so the carve-out region, the merger-or-human region, and the
merger-and-human region are the *same set* of changes:
`branch:main AND (hashtag:"milestone:R3-fastforward" OR hashtag:R3-fastforward)
AND intopic:release-cut AND owner:sora`. `Human-Plus-2.applicableIf` is the exact
De Morgan negation of that signature. Use `intopic:` (substring) — not `topic:`
(exact) — for "topic contains release-cut". Patch both `.gerrit/project.config`
and `.gerrit/project.config.example` identically (the C4 sync test asserts it).

**Verification**: `pytest backend/tests/test_merger_bot_main_promote.py` — the
static config-parse + tiny-evaluator suite, extended with
`test_carveout_applicability_set_is_consistent`,
`test_op1534_bare_hashtag_on_main_no_longer_carved_out` (the over-scope closure),
`test_bare_r3_fastforward_hashtag_spelling_is_accepted` (the OR transition form),
the `old release-v topic` quad-key knockout (proving the exact-vs-substring fix),
and `test_project_config_example_keeps_op962_block_in_sync`. With no live Gerrit
in the runner sandbox, the parsed-config evaluator is the CI-equivalent of the
read-only `gerrit query` atom validation the ticket calls for.

**Generalisation**: **When a "carve-out" SR removes a gate and another SR
re-imposes it, their `applicableIf` predicates must cover exactly the same set —
or the carve-out over-scopes.** Any key that the re-imposing rule requires but
the carve-out omits becomes a window where the gate is removed and never put
back. Derive the carve-out's applicability as the literal negation of the
re-imposing rule's applicability, and pin both with a "partial-key knockout"
test. Separately, in Gerrit query language `topic:` is exact-match and `intopic:`
is substring — a `topic:` predicate meant as "contains" is a silent no-op; the
same exact-vs-substring split bites elsewhere (file:, message:). Validate every
atom of a submit-requirement against a known positive AND a known negative before
trusting it.
