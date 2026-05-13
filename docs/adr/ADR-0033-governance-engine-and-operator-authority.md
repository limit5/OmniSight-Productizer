---
id: ADR-0033
title: Governance Engine + Operator Authority Hierarchy
status: Proposed
date: 2026-05-13
relates_to:
  - ADR-0023 (Foundation Rebuild — references OP-1042 hook 50+ times)
  - Sprint S12: Bedrock (11-phase ticket specs)
  - Sprint S12.G: Governance Engine (this ADR's implementation)
---

# ADR-0033 — Governance Engine + Operator Authority Hierarchy

## Status

Proposed (2026-05-13).

## Context

Sprint S12: Bedrock (11 phases, ~306 tickets) consistently references **OP-1042 filing-time hook** to enforce ticket-level governance rules. Over the course of writing 11 phase specs, OP-1042 has accreted 12+ rule categories:

1. boundary-schema validation (10+ fields per ticket)
2. context_hint preamble (6 lines) for all tier:S tickets
3. path canonicalization (e.g., GitLab lowercase per L-OP-247)
4. key-material class enforcement (operator-* required when handling cryptographic keys)
5. mixed-scope `scope_components` validation
6. dynamic-ticket preflight (31.I-7-Template filing-script local validation)
7. semantic blocker audit (31.J-ReadinessGate checks specific evidence fields per phase, not just status)
8. jira-write / jira-read / meta-closure side-effect classification
9. cross-phase blocker JQL preflight (verify prior phase Closed + commit SHA)
10. tag_type: annotated enforcement
11. repo-content-delete vs git-ref-rewrite distinction
12. secret-leak patterns in CI YAML

Per codex's cross-phase review (Q3d 2026-05-13):
> "OP-1042 has grown from schema validation into a policy engine. It needs versioned schema modules and fixture tests per phase. Without that, OP-1042 becomes a silent single point of governance failure."

Concurrently, **operator availability** has emerged as the single biggest schedule risk:
- 11 phases × ~7 operator-window tickets = ~75 operator-touch events
- Concentrated on a single human operator (sora) for "highest-stakes" decisions
- Cutover sessions (31.J 5-step) require unbroken 2-4 hours
- Some tickets are operationally simple but currently require top-tier operator authority (e.g., 31.D per-ref replication enablement)

Operator's directive (2026-05-13):
> "讓 副操作員具有僅次於最高權限的等級，必要時可直接代行。除非是最高級別任務須由操作員指示或動作，不然可以有條件的向下授權。"

This requires a **3-level authority hierarchy** baked into the ticket schema, enforced by OP-1042.

## Decision

### 1. Extract OP-1042 from a single ticket into Sprint S12.G: Governance Engine

OP-1042 is too complex for a single ticket; it's effectively a **policy engine**. Sprint S12.G (sibling to S12: Bedrock) implements it across 6 sub-phases:

| Sub-phase | Scope |
|---|---|
| **G.A** | Canonical ticket-contract schema models (Pydantic + JSON Schema); covers all v2 boundary fields |
| **G.B** | Phase plugin architecture — each S12 phase (31.A-K) registers its own rule module |
| **G.C** | Filing-time hook enforcement — rejects POST violating schema / cross-phase preflight |
| **G.D** | Runtime cron audit — ongoing compliance check on existing tickets (not just filing-time) |
| **G.E** | Governance dashboard / reporting — violations / waivers / coverage |
| **G.F** | Back-migration — bring legacy tickets up to v2 schema where feasible |

**G.A is a hard blocker for filing any S12 ticket**: without canonical schema, every spec's references to OP-1042 are non-load-bearing. G.B+ can run concurrently with S12 phases 31.A onwards.

### 2. 3-Level Operator Authority Hierarchy

| Level | Identity | Authority |
|---|---|---|
| **L1: Top Operator (sora)** | Single human (project owner) | All operations, including highest-tier exclusive ones |
| **L2: Deputy Operator** | Designated trusted humans (operator authorizes via roster file) | Second-highest authority; can act on L1's behalf except for L1-exclusive operations |
| **L3: Runner (claude-bot / codex-bot)** | AI agents | Runner-doable operations per existing capability matrix; refuses operator-* class tickets |

**L1-exclusive operations** (Deputy CANNOT do; only sora can):
- Cosign private key generation + storage (31.F-6a)
- GA tag promotion (31.K-9a — v0.5.0 release governance)
- Sprint META Closure (31.K-Integration)
- GPG private key custody for backups (31.H operator action log entries)
- Gerrit admin SSH key rotation (post-cutover authority shifts)
- Any ticket carrying `external_payload_class: key-material` AND `destructive_op_scope: system`
- Any ticket with `external_side_effect: meta-closure`
- Override of cross-phase preflight failures (forced filing despite blockers fail)
- Adding/removing Deputy from roster

**L2 (Deputy) authorized operations** (can act on L1's behalf):
- All `operator-prepare-only` class tickets
- Most `operator-window` tickets EXCEPT those tagged L1-exclusive per rules above
- Operator-witnessed smoke tests (31.B-LegacySmoke, 31.F-SignSmoke, 31.G-StackSmoke, etc.)
- Per-runner cutover scripts (31.B-10a)
- Backup DR Drill (31.H-BackupDRDrill)
- Daily verification triggers
- AuditReviewGate decisions (31.I) — files dynamic fix tickets
- Per-ref replication enablement (31.D-6a/6b/6c/6d)
- 31.J cutover **steps 1-4** (6a-6d); but 6e (rc2 tag) is L1-only
- StabilityCheckpoint confirmations (24-72h windows; L2 can confirm)

### 3. Schema additions (encoded in G.A)

```yaml
# Class enum (replaces single `operator-window` with hierarchy)
class:
  - subscription-{claude,codex}   # L3 runner
  - operator-prepare-only         # L3 prepares; L2 or L1 approves
  - operator-window-deputy        # L2 (or L1) can execute
  - operator-window-top           # L1-exclusive
  - operator-rehearsal            # L1 or L2 witnesses; smoke/test scope

# NEW field: explicit reason if L1-exclusive
l1_exclusive_reason: str | null   # required when class includes operator-window-top
                                  # values: 'key-material' | 'meta-closure' | 'release-tag-governance' | 'override' | 'roster-mutation' | <custom>

# Deputy roster file
~/.config/omnisight/operator-deputy-roster.yaml   # signed by L1; contains active Deputy identities + SSH key fingerprints; rotated quarterly
```

### 4. Authority enforcement points

- **Filing-time hook (G.C)**: rejects tickets whose `class` claims L2 authority for L1-exclusive operations (per rule table above)
- **Pickup-time runner refusal**: runner refuses tickets with `operator-window-*` class; runners only pickup `subscription-*` or `operator-prepare-only` (where they prepare; operator deploys)
- **Audit log**: all L2 executions logged with `acting_on_behalf_of: L1` + deputy identity + L1's authorization timestamp
- **Roster file is L1-managed**: only L1 can add/remove Deputy identities; roster mutations are L1-exclusive

### 5. Cross-phase preflight (G.B + G.C integration)

Existing pattern from 31.F v2 §0 (JQL/Gerrit preflight) becomes formalized:
- G.B per-phase plugins each declare their `external_blockers: [ticket_keys]`
- G.C filing hook queries JIRA + Gerrit to verify:
  - blocker is Closed
  - blocker's commit SHA matches recorded evidence
  - semantic evidence field (per blocker spec) is correct value
- Failure → reject filing + emit governance alert to 31.C P2

### 6. Versioned schema modules

Per codex Q3d: schema cannot be one monolithic validator. G.A produces:
- `governance_engine/schema/v1.py` — Pydantic models for base contract
- `governance_engine/schema/phase_plugins/{31a,31b,...,31k}.py` — per-phase extensions
- `governance_engine/schema/migrations.py` — version-bump migrations for legacy tickets

When a phase adds new fields (e.g., 31.F adds `key-material`), the phase plugin is updated; base schema remains stable.

## Consequences

**Positive**:
- OP-1042 is no longer a single point of failure (versioned modules + plugins)
- Sub-operator authority unblocks operator availability bottleneck (L2 absorbs ~60% of operator-window load per estimate)
- Cross-phase preflight is mechanically enforced (no stale-blocker drift)
- Filing-time + pickup-time + audit-log = defense in depth for governance

**Negative / Tradeoffs**:
- S12.G is a NEW concurrent sprint (~115 tickets per draft); adds project complexity
- Deputy roster mutation is L1-exclusive — initial setup requires L1
- Pickup-time refusal needs runner-side implementation (touches auto-runner-jira.py + jira_dispatch.py)
- Schema migration of legacy tickets (G.F) is best-effort; some may never migrate cleanly

**Risks**:
- If Sprint S12.G G.A is not done before S12 31.A filing, the cross-phase governance promise is paper-only
- Deputy authority misuse: L2 acts beyond authority → governance audit + roster removal procedure (must be defined in G.A roster spec)
- OP-1042 plugin bugs blocking valid filings: G.C must have explicit override path (L1-only)

## Alternatives Considered

- **Keep OP-1042 as single ticket**: rejected — codex's "policy engine" framing + 50+ references makes it untrackable
- **No deputy operator**: rejected — operator availability is biggest schedule risk per codex Q6a
- **Cosign-style keyless authority via OIDC**: deferred — requires OIDC issuer setup; not foundational to current scope

## Open Questions

1. Initial Deputy candidate identities? (operator decision; not in ADR)
2. Roster rotation cadence: quarterly vs annual? (recommended quarterly; operator decides)
3. Should G.D runtime audit alert on schema-drift in real-time, or daily digest? (G.D spec decides)
4. Cross-sprint application: does S12.G apply ONLY to S12 tickets, or all future tickets? (recommended: applies to all S12 + future; legacy migration via G.F is best-effort)
