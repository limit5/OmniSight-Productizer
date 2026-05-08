---
id: L-OP-785
ticket: OP-785
title: When a hand-rolled shim ships before its framework ADR, evaluate alternatives as if the shim doesn't exist
date: 2026-05-08
tags: [adr, docs, decision-sequencing]
---

# When a hand-rolled shim ships before its framework ADR, evaluate alternatives as if the shim doesn't exist

**Situation**: OP-792 had to ship the docs-site publish workflow before
the framework-decision ADR (OP-785, ADR-0012) had been written, so it
landed a 240-line hand-rolled Python markdown renderer
(`backend/docs_static_site.py`) as a stop-gap. When OP-785 then opened to
formally choose the framework, the natural framing was "should we keep
what we have or migrate?" — which biases the evaluation against any
alternative whose payoff is in features the hand-rolled renderer doesn't
yet have (search, ToC, syntax highlighting, theming).

**Fix**: ADR-0012 deliberately re-evaluates the custom-static option on
its long-term trajectory (LOC scaling per feature, no plugin ecosystem,
hand-written markdown matcher with known edge cases on nested lists /
blockquotes / footnotes), not on its current 240-LOC footprint. Same
move for the other three alternatives — each scored against the OP-785
criteria as a fresh choice, not against "what would replacing the shim
cost". MkDocs (Material) wins on the criteria; the shim stays in tree
only as the publish-pipeline implementation until a follow-up ticket
swaps it for `mkdocs build`.

**Verification**: ADR-0012 §"Custom Python static (status quo from OP-792)
— rejected as long-term" rejects the shim explicitly on long-term
trajectory grounds. Spike at `docs-site/` builds in ~0.27s real-time
(3 timed runs) and validates the JIRA-link hook, frontmatter parsing,
and per-file lesson rendering required by the OP-785 acceptance criteria.

**Generalisation**: When a stop-gap implementation lands ahead of the
ADR that is supposed to choose the long-term solution, the ADR has to
evaluate alternatives *as if the stop-gap did not exist* — otherwise
"already shipped" becomes an unstated criterion that outweighs the
explicit ones. The check: would this alternative win against a clean
slate? If yes, then the migration cost from the stop-gap is a separate
question, owned by a separate ticket, with its own ROI calculation.
Conflating the two collapses the framework decision into a sunk-cost
defence.
