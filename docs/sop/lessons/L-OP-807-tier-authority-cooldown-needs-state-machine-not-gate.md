---
id: L-OP-807
ticket: OP-807
title: Per-agent cooldown needs a state machine, not a gate
date: 2026-05-09
tags: [governance, gerrit, jira, classifier, adr-0005]
---

# Per-agent cooldown needs a state machine, not a gate

**Situation**: ADR-0005 §4 layer 4 calls for "30-day cooldown if an agent
misclassifies > 3 times in 30 days." The instinctive read is "boolean
gate: count → toggle." That model breaks the moment the spec adds the
two follow-up rules: 2 cooldowns in 90 days → promote to 90d, and 3
cooldowns in 365 days → revoke. A boolean gate has no memory; the
escalation rules need history per agent. We almost shipped a single
column (`is_in_cooldown`) before realising the audit columns the
operator dashboard tile needs (`previous_level`, `last_evaluation_at`,
`cooldown_history`) are exactly what a state-machine row carries.

**Fix**: OP-807 split the layer into two tables that match the cron's
two read shapes:

* `tier_cooldown_observation` — append-only event log keyed on
  `(agent_id, classification_date)`. The trailing-window scan (the hot
  path) hits this with one composite index and never re-counts old rows.
* `tier_cooldown_state` — one row per agent with `cooldown_level`,
  start/expiry timestamps, and a `cooldown_history` JSON array. Sweep
  consults history to decide 30→90→revoked transitions; the classifier
  consults the active level + expiry to decide tier promotion.

The classifier integration (`apply_cooldown_to_tier`) reads the state
row only — never the observation table — so the per-classification
overhead is one indexed primary-key lookup. The state machine is
expressed in pure code (`evaluate_agent`), making the four threshold
constants (`THRESHOLD_30D_OVERRIDES`, `THRESHOLD_90D_COOLDOWNS`,
`THRESHOLD_365D_COOLDOWNS`, plus the 30/90/365-day window lengths) the
only ADR-0005 numbers a future spec change has to touch.

**Verification**: `backend/tests/test_tier_cooldown.py::TestSweepStateMachine`
pins all four state transitions (none → 30d, 30d → 90d, 90d → revoked,
expired → none) plus the synthetic AC-fixture flow under
`TestSyntheticAcFixture::test_full_flow`. Constants are pinned
separately (`TestThresholdConstants`) so a tweak to the cooldown
numbers breaks the test before it breaks the cron in production.

**Generalisation**: Any per-agent rate-limit / penalty rule with
escalation tiers should be modelled as `(observation table, state
table)` from day one — even when the first rule (the 30-day gate)
looks single-column. Adding the state row late means rewriting the
sweep, the classifier hook, AND the dashboard query at the same time;
adding it up front is one extra `CREATE TABLE` in the same migration.
