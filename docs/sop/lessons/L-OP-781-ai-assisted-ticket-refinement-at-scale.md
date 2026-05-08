---
id: L-OP-781
ticket: OP-781
title: AI-assisted ticket refinement at scale (placeholder cleanup)
date: 2026-05-08
tags: [jira, llm, runner, operations, cost]
---

# AI-assisted ticket refinement at scale

**Situation**: 2026-05-08 OP-231 hit a 21-cycle infinite revert loop because
its description still contained the `_(operator: refine before pickup —
TODO.md source line is the seed)_` placeholder. An audit found ~509 sibling
Story tickets in the same shape — all migrated from `TODO.md` without
operator refinement. Bulk-pause moved them to Task + label
`runner-needs-refinement` to drain the time bomb, but that surfaced a new
problem: how does the operator unblock 500 tickets without spending ~125 h
on manual rewrite?

**Fix**: Three-stage CLI at `scripts/refine_seed_tickets.py`:

1. `--propose` — Anthropic Haiku drafts AC + Files + Prerequisites for each
   ticket (~$0.02/ticket × 500 ≈ $10 one-shot LLM spend).
2. `--review` — operator inspects + accepts/rejects per ticket
   (~30 sec/ticket × 500 ≈ 4 h, vs the original 125 h).
3. `--apply` — flips Task → Story, removes `runner-needs-refinement`,
   adds `refined-by:ai-assisted`, posts `[ai-refined]` audit comment.

**Verification**: 28 unit tests in `backend/tests/test_refine_seed_tickets.py`
cover the synthetic 5-ticket batch (AC #4), full apply flow (AC #5), cost
report (AC #6), audit-log shape (AC #7), confidence-tier classifier across
high/medium/low examples. Tests pass without network: the LLM proposer is
injectable, JIRA helpers are monkeypatched in-place.

**Generalisation**:

- **Goal preservation matters**. The renderer at `render_refined_description`
  preserves the original placeholder Goal verbatim. The LLM only proposes
  AC / Files / Prerequisites — never rewrites the operator's intent. When
  applying LLM-drafted refinements at scale, draw the line at "what does
  the ticket want" vs "how do we know it's done"; the latter is safe to
  auto-draft, the former isn't.

- **Confidence tiers are heuristics, not classifiers**. The classifier in
  `classify_confidence` uses a hard-coded keyword list (high) + spec-
  alignment hints (low) + short-input fallback (low). Cheap and
  explainable beats clever scoring — operators should be able to predict
  the tier from the summary alone.

- **Audit trail is JSONL**. One line per `propose` / `apply` action with
  ts + operator + token count, written to
  `data/refine-proposals/audit.log`. Greppable, jq-aggregatable, and
  doesn't require a database. Use this pattern for any low-volume
  operator-driven mutation log where SQL would be overkill.

- **`runner-needs-refinement` + `refined-by:ai-assisted` is the canonical
  refinement-state pair**. The first label gates pickup until refined;
  the second flags the refinement source so future analyses (drift
  detection, cost reports) can carve out the AI-touched cohort. Don't
  re-use either label for unrelated workflows.

- **argparse subparsers can't carry `--`-prefixed names**. The OP-781
  spec writes the CLI as `script.py --propose`, but argparse's
  `add_subparsers().add_parser("--propose")` silently fails to bind a
  positional after the subparser name. Use a mutually-exclusive flag
  group with `metavar="OP-XXX"` for `--review` / `--apply` keys
  instead. (Verified the failure mode 2026-05-08 — see
  `scripts/refine_seed_tickets.py::build_parser` docstring.)
