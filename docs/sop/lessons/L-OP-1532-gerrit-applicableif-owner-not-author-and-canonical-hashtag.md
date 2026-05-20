---
id: L-OP-1532
ticket: OP-1532
title: A Gerrit submit-requirement keyed on author: (git author, forgeable) never matches the real workflow — verify owner: against a live change
date: 2026-05-20
tags: [gerrit, submit-requirements, release, security, adr]
---

# A Gerrit submit-requirement keyed on `author:` never matches the real workflow — use `owner:`, and verify against a live change

**Situation**: ADR-0020's `release-cut-promote` submit-requirement (OP-982) shipped
a quadruple-keyed `applicableIf` whose identity term was
`author:^auto-promote-bot$|^claude-bot$|^codex-bot$`. It looked careful — four
keys, a `SubmitRuleOverScopes` fallback to `Human-Plus-2` — but it was wrong in
three independent layers and, decisively, **never matched a single real release
cut**. Layer 1 (the dominant one): Gerrit's `author:` predicate matches the
*git commit author* string (free text, forgeable with `git commit --author=`),
not the authenticated change *owner*; and real release cuts are owned by the
operator `sora`, not by any of the three bot identities. The 2026-05-16
post-mortem records the live cut — Change #711 (OP-1182) "Merged with human +2
from `sora`". So the four-way `AND` short-circuited to false on every real cut →
the rule was permanently `NOT_APPLICABLE` → the merger-bot machine attestation
ADR-0020 promised was dead on arrival. Layer 2: a version-anchored `topic:^release-v…$`
regex de-gates on any SemVer variant. Layer 3: the bare (`R3-fastforward`) vs
namespaced (`milestone:R3-fastforward`) hashtag form was never made canonical, so
emitter and gate could silently disagree. Nothing merged unsafely (the
`Human-Plus-2` fallback held), but `project.config` advertised a gate that did
not exist.

**Fix**: ADR-0039 (OP-1532) corrects the key to
`branch:main AND (hashtag:"milestone:R3-fastforward" OR hashtag:R3-fastforward) AND intopic:release-cut AND owner:sora`:
`owner:sora` (authenticated, non-forgeable change owner — matches reality *and*
is stronger than the forgeable `author:`); `intopic:release-cut` (the stable
`release-cut` topic token already in the merge-commit subject, not the brittle
version regex); and `(… OR …)` accepting both hashtag forms during a transition
window with `milestone:R3-fastforward` declared canonical. The canonical-hashtag
decision deliberately did **not** introduce a new protected `release-cut`
*hashtag* (the ticket's literal recommendation): a hashtag is mutable by anyone
with `editHashtags`, so it can never be the unforgeable anchor — `owner:` is. The
config + code edits (the `.gerrit/project.config` line and the `auto_promote_main`
topic emission) are downstream consumers C2/C3a that cite ADR-0039.

**Verification**: `docs/adr/ADR-0039-release-cut-canonical-hashtag-and-corrected-applicableif.md`
records the three-layer analysis, the canonical-hashtag decision, and the
corrected `applicableIf` verbatim; ADR-0020 §3 carries a "Superseded-in-part by
ADR-0039" banner. Layer-1 evidence:
`docs/retrospectives/2026-05-16-prod-deploy-attempt-post-mortem.md:113` (Change
#711 owned by `sora`). Layer-3 origin:
`docs/audit/2026-05-12-audit-26-phase-0-verification.md:144,154-160` (the
bare-vs-namespaced contract flag that was never ratified in an ADR).

**Generalisation**: **In a Gerrit `applicableIf` / `submittableIf`, an identity
key must use `owner:` (the authenticated change owner), never `author:` (the git
commit author).** `author:` is free-text and forgeable, and for any change built
by merging (release cut, rebase, squash) the git author is often not who you
think. The deeper trap: a multi-key carve-out that *looks* strict can be
*permanently inapplicable* and therefore inert — and an inert gate is invisible
unless you test it against a live change. Before trusting any keyed
submit-requirement: (1) push a sandbox change that *should* match and confirm the
requirement shows up `UNSATISFIED` (not `NOT_APPLICABLE`); (2) confirm the
predicate semantics (`owner:` vs `author:`, `intopic:` substring vs `topic:`
exact/regex, exact-string `hashtag:`) against Gerrit's docs, not intuition; (3)
when a token has two spellings in the tree (`milestone:R3-fastforward` vs
`R3-fastforward`), make one canonical *in an ADR* and accept both via `OR` during
a transition — never let emitter and gate diverge by accident.
