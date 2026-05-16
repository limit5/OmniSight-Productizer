# OP-1282 debuff registry refactor proposal

## Context

`backend/agents/debuff_registry.py` is a stateless W15 registry: callers pass
all runtime inputs through `DebuffContext`, and the module returns deterministic
debuff IDs or multipliers. The module already follows the sibling
`buff_registry.py` shape closely and does not need a broad rewrite.

Two small refactors would reduce coupling inside the module without changing
the public API:

1. Add a private multiplier helper shared by
   `xp_multiplier_for_debuff_ids()` and
   `routing_weight_multiplier_for_context()`. Both functions currently repeat
   the same "start at 1.0, filter by kind, multiply definitions" loop. A helper
   like `_multiplier_for_debuff_ids(debuff_ids, kind)` would keep unknown-ID
   validation in `get_debuff_definition()` and make future debuff kinds less
   likely to duplicate loop code.
2. Represent active debuff checks as a private ordered rule table. Today
   `active_debuffs_for_context()` knows each debuff ID and predicate directly.
   A private tuple of `(debuff_id, predicate)` pairs would keep stable ordering
   explicit while isolating the per-debuff activation rules. This makes adding
   the next debuff a one-row change plus a predicate, instead of editing the
   orchestration body.

## Non-goals

- No behavior change to existing debuff thresholds, IDs, summaries, or
  multiplier values.
- No persistence, database schema, or per-agent debuff state.
- No public API rename; existing imports from `xp_engine.py` and
  `routing_policy.py` should remain valid.
- No docs rewrite outside this proposal.

## Follow-up implementation ticket draft

Summary: Refactor `backend/agents/debuff_registry.py` multiplier and active-rule
helpers

Areas: backend, tests

Tier: S

Description:

Implement the two OP-1282 proposal refactors in
`backend/agents/debuff_registry.py`:

- Add a private shared multiplier helper for explicit debuff IDs and use it
  from `xp_multiplier_for_debuff_ids()` and the routing-weight multiplier path.
- Add a private ordered active-rule table for Burnout and Stale Memory, and use
  it from `active_debuffs_for_context()` without changing stable output order.
- Preserve the public API, constants, `__all__`, and current exception behavior.
- Add focused tests under `backend/tests/` for active debuff ordering, multiplier
  preservation, and unknown debuff validation through the refactored helper.

Acceptance criteria:

1. `backend/agents/debuff_registry.py` keeps the same public exports and
   existing callers do not need import changes.
2. Burnout and Stale Memory activation order and thresholds are unchanged.
3. XP and routing-weight multipliers are unchanged for empty, known, mixed-kind,
   and unknown debuff ID inputs.
4. Relevant pytest coverage passes in the project venv.

Filed follow-up: OP-1338.
