---
id: L-OP-1032
ticket: OP-1032
title: An ADR's "Consequences" prose describes intent, not what shipped — re-verify split-across-tickets redesigns against the code
date: 2026-05-13
tags: [audit, architecture, adr, release, runner]
---

# An ADR's "Consequences" prose describes intent, not what shipped — re-verify split-across-tickets redesigns against the code

**Situation**: AUDIT-29d-4 (OP-1032) re-ran the AUDIT-26a / OP-980 §4
linear-history-consumer audit against the *as-shipped* code. ADR-0020
§"Consequences/History" states "the only two [linear-history consumers] that
break are `auto_promote_main.evaluate_fast_forward` (→ AUDIT-26d redesigns it
away) and `auto_tag_release._update_release_branch`'s cherry-pick fallback
(→ needs `-m 1`; AUDIT-26a-3)". Reading that, you'd assume both fixes are (or
will be) in the tree. Neither is: **AUDIT-26a-3 was never filed** (it exists only
as a forward-reference in OP-980's doc and ADR-0020), and **OP-983 (AUDIT-26d)
delivered only half of its assignment** — its commit message, "Rewrite auto
promote main as single merge change", is accurate but narrower than ADR-0020
§"Decision" 1, which assigned AUDIT-26d *both* the single-merge-commit push side
*and* the `evaluate_fast_forward` redesign ("drop the FF pre-check"). OP-983 did
the push side and left `evaluate_fast_forward` (`backend/agents/auto_promote_main.py:303-323`)
and its `main_only`-non-empty → `blocked` routing (`:435-481`) untouched, so the
*second* release cut after `MERGE_ALWAYS` goes live still wedges
(`git log develop..main` is non-empty — it holds the prior cut's merge commit —
so `status="blocked"` → `critical` alert, no promote). The ADR's prose had
quietly drifted from "this is what will happen" to "this is what happened" in
readers' heads, including across two follow-up tickets (OP-984, OP-985) that
cited ADR-0020 as settled.

**Fix**: This ticket recorded the drift explicitly — `docs/audit/2026-05-13-linear-history-consumers.md`
re-verifies all 14 consumers against the as-shipped code (12 OK, 2 still
`needs-fix`), and ADR-0020 gained a §"Follow-up — linear-history consumer
re-audit (OP-1032)" section that flags the §"Consequences" line as "aspirational,
not yet true" and names the two follow-up fixes (finish the `evaluate_fast_forward`
redesign + the `auto_tag_release` `-m 1` one-liner) that must land before
AUDIT-26e flips `MERGE_ALWAYS` on.

**Verification**: the wedge was reproduced locally — a throwaway repo with two
simulated `MERGE_ALWAYS` cuts shows `git log develop..main` returning the two
prior-cut merge commits, so `evaluate_fast_forward`'s
`status = "ff_possible" if develop_only and not main_only else "blocked"` returns
`blocked`. The cherry-pick failure was reproduced the same way
(`git cherry-pick <merge_sha>` → `is a merge but no -m option was given`). The
single-cut path is the only one covered by `backend/tests/test_release_cut_e2e.py`;
the second-cut path has no test (gap noted for the follow-up).

**Generalisation**: **When a redesign is split across an ADR/spec ticket and one
or more implementation tickets, the ADR's "Consequences" / "Decision" prose is a
statement of *intent*; the implementation ticket's *diff* is the only record of
what shipped — and a ticket's commit subject can be a true description of a
narrower-than-assigned change.** Before treating a "→ ticket Y fixes this"
forward-reference as resolved: (1) confirm ticket Y exists and is closed; (2) read
ticket Y's actual diff and check it covers *everything* the ADR assigned it, not
just the part the commit subject names; (3) if a later ticket cites the ADR as
settled, that citation does not re-verify the implementation — re-verify it
yourself. A cheap habit that catches this: when you write "→ AUDIT-NN redesigns
it away" in an ADR, also write the *test name* that will prove it, so a future
auditor greps for the test rather than trusting the prose. (Adjacent patterns:
the "shipped-but-not-deployed" anti-pattern #13 — code merged, never activated —
and L-OP-985 — the release-cut/code-review category error this whole ADR family
is untangling. This lesson is the "shipped-but-incomplete-vs-the-spec" sibling.)
