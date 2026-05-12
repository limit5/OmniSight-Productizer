---
id: L-OP-966
ticket: OP-966
title: ADR-number pre-check overrides the number a ticket pre-assigned
date: 2026-05-12
tags: [adr, docs, jira-conventions, sibling-discipline]
---

# ADR-number pre-check overrides the number a ticket pre-assigned

**Situation**: OP-966 (AUDIT-18a) asked for `ADR-0018` and said "ADR-0017 is
reserved for AUDIT-17". By the time the ADR was written both numbers were
already in use on the integration branch — ADR-0017 = "Release conductor
half-automation" (OP-944) and ADR-0018 = "Event-driven release pipeline"
(OP-954) — because two later Sprint G/H tickets had landed their ADRs in the
gap between when OP-966 was filed and when it was worked. The ticket's own AC
carried the L-OP-870 pre-check (`ls develop:docs/adr/ADR-001[78]*`) precisely
to catch this; running it surfaced the collision.

**Fix**: Treat the ADR number written in a ticket as a *hint*, not a binding.
Run the `ls develop:docs/adr/ADR-00NN*` pre-check immediately before committing;
if the slot is taken, take the next free number, and (a) state the renumber and
its rationale in a "Numbering note" section of the new ADR, (b) call out the new
number in the JIRA AC-verification comment so reviewers aren't surprised, and
(c) make sure any blocked implementation tickets reference the *actual* number.
Here the design shipped as ADR-0019 and AUDIT-18b/18c must reference ADR-0019.

**Verification**: `docs/adr/ADR-0019-release-force-promote-operator-override.md`
exists and contains a "Numbering note" section explaining the 0018→0019
renumber; `git ls-tree HEAD -- docs/adr/` shows no ADR-number collision.

**Generalisation**: Any ticket that pre-assigns an artifact identifier (ADR
number, migration revision, sprint letter, RFC number) is asserting a fact about
the repo *at filing time*. Identifiers are first-come-first-served on the branch,
so always re-derive the next free one at commit time, record the delta where the
artifact lives, and propagate it to dependents — never trust the number the
ticket text remembers.
