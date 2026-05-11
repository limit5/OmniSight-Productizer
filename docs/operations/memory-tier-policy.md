# Memory tier policy (C6 / OP-856)

This runbook describes how the OmniSight runner gates Anthropic Memory
Tool recall calls. It is the operator-facing companion to
`backend/agents/memory_tool_handler.py` and complements the C1 (OP-851)
basic tier filter with the finer-grained S/M/L/X policy specified in
`docs/audit/2026-05-11-sprint-abc-master-plan.md` §3.7.

## Why a tier filter

Memory Tool recall surfaces past lessons, prior decisions, and
runner-side scratch notes back into the model's context. Some of
those records are safe for any runner to see; others encode
privileged or high-blast-radius knowledge (production credentials
shape, post-incident root-cause notes) that must not flow into a
sandbox dev-fleet runner unsupervised. The tier label on each record
encodes "how much trust does recall need" and the policy gate
enforces it on every read.

Tier labels are the same `tier:S/M/L/X` taxonomy used on JIRA tickets
(see `docs/sop/jira-ticket-conventions.md`). Memory records inherit
the tier of the ticket that produced them.

## Policy table

| Tier | Same-fleet recall | Cross-fleet recall | Audit row | Operator notify |
|------|-------------------|--------------------|-----------|-----------------|
| `S`  | allow             | allow              | yes       | no              |
| `M`  | allow             | allow only when target fleet appears in `OMNISIGHT_MEMORY_FEDERATION` | yes | no |
| `L`  | allow only when `OMNISIGHT_MEMORY_TIER_L_OPTIN=1` | additionally requires the target fleet in `OMNISIGHT_MEMORY_FEDERATION` | yes (escalate flag set so audit appears in the operator-review queue) | escalate to operator-review queue |
| `X`  | refuse            | refuse             | yes (refusal row) | page operator immediately |

"Same-fleet" means the request's `query_fleet` equals its
`target_fleet`; "cross-fleet" is anything else. Both labels are taken
from the `RecallRequest` passed by the C1 memory tool handler when it
loads a record from the `/var/omnisight/memory/<fleet-id>/` namespace.

## State transitions

```
memory.recall(query, tier, fleet) → policy_check
    ├── tier:S                                 → allow + audit
    ├── tier:M same-fleet                      → allow + audit
    ├── tier:M cross-fleet + federation        → allow + audit
    ├── tier:M cross-fleet no federation       → refuse (CrossFleetRecallRefused)
    ├── tier:L + opt-in (and federation if x-fleet) → allow + audit + escalate
    ├── tier:L no opt-in (or x-fleet w/o fed)  → refuse (TierViolationUnauthorizedRecall)
    └── tier:X                                 → refuse + escalate
                                                  (TierViolationUnauthorizedRecall, escalate=True)
```

The decision FSM is implemented as a pure function
(`evaluate_recall`); the side-effect-bearing variant
(`enforce_recall`) wraps it with the audit emit + raise-on-refuse
behaviour.

## Environment knobs

* `OMNISIGHT_MEMORY_TIER_L_OPTIN` — `1` (or `true`/`yes`/`on`) opts
  this runner in to `tier:L` recalls. Anything else, including unset,
  refuses. Inherited from C1 — keep set on prod-fleet runners that
  carry the L-clearance, leave unset on dev-fleet runners.
* `OMNISIGHT_MEMORY_FEDERATION` — comma-separated list of fleet ids
  this runner is allowed to recall *across*. Whitespace and blank
  entries are ignored. Example: `OMNISIGHT_MEMORY_FEDERATION=fleet-prod,fleet-staging`.
  The runner's *own* fleet does not need to appear in the list —
  federation governs foreign-fleet access only.

Neither knob is required for normal `tier:S/M` same-fleet operation.

## Error catalog

| Exception | Trigger | Operator action |
|-----------|---------|-----------------|
| `TierViolationUnauthorizedRecall` (escalate=False) | tier:L recall without opt-in | none — expected on dev fleets |
| `TierViolationUnauthorizedRecall` (escalate=True)  | tier:X recall attempted, OR a permitted tier:L recall (decision-level escalate) | review the audit row; tier:X attempts indicate a buggy ticket label or a mis-classified record |
| `CrossFleetRecallRefused`                         | tier:M / tier:L cross-fleet recall, federation env not set | confirm fleet-pair federation is intended; if so, set `OMNISIGHT_MEMORY_FEDERATION` |
| `MemoryAuditWriteFailed`                          | audit-row persistence failed (DB outage, buffer error) | recall still succeeds (fail-open). Investigate audit DB; sustained failures will leave a hole in `runner_incidents` |
| `UnknownMemoryTier`                               | record carries a tier label outside S/M/L/X | fix the record's tier label; this is an indexing bug, not a runtime policy decision |

## Audit row shape

Every recall (permit or refuse) writes one row to the `runner_incidents`
table (planned in C2; until that migration lands the row buffers in a
bounded in-memory ring — see `backend/agents/incident_recorder.py`):

* `failure_class` = `MEMORY_RECALL_AUDIT`
* `summary` = `recall query=<query> tier=<S|M|L|X> fleet=<src>-><dst> outcome=<permitted|refused> note=<reason>`
* `escalate` = matches the policy decision's escalate flag
* `permitted` = boolean
* `tier`, `query_fleet`, `target_fleet` — denormalised for fast filter
* `recorded_at` — wall-clock at write

Fail-open: if the audit-row insert raises, the recall result still
returns (or the refusal still raises) and a `WARNING` line lands in
the runner log. Sustained audit-write failures do *not* gate recall
— a missing audit row is logged but never escalated to the policy
gate.

## Cross-fleet bench verification

Per the C6 DoD, the federation refusal path is verified on a
dev/prod federation test bench. The deterministic case lives in
`backend/tests/test_memory_tier_policy.py::test_tier_m_cross_fleet_without_federation_refuses`,
which runs in CI with no environment dependencies. Operator-side
soak-test instructions:

1. Stand up two runner pods with distinct `OMNISIGHT_FLEET_ID`
   values (`fleet-dev`, `fleet-prod`).
2. Seed `tier:M` records into `/var/omnisight/memory/fleet-prod/`.
3. From the `fleet-dev` pod, issue a memory recall against the
   `fleet-prod` namespace without `OMNISIGHT_MEMORY_FEDERATION` set
   — expect `CrossFleetRecallRefused` and one refusal row in
   `runner_incidents`.
4. Set `OMNISIGHT_MEMORY_FEDERATION=fleet-prod` and re-run — expect
   the recall to succeed and one `permitted=true` audit row.

## Rollback

Policy-only change. Rollback path: revert this row, leaving C1's
basic tier filter in place. Audit rows already persisted in
`runner_incidents` survive rollback because the table itself is owned
by C2.

## Cross-references

* Spec source: `docs/audit/2026-05-11-sprint-abc-master-plan.md` §3.7
* C1 base implementation: `backend/agents/memory_tool_handler.py`
  (`tier_filter`, `enforce_recall`)
* C2 audit table: `backend/agents/incident_recorder.py` (in-memory
  ring buffer; Postgres surface lands with C2)
* JIRA tier conventions: `docs/sop/jira-ticket-conventions.md` §6
