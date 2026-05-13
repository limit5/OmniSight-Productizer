---
id: SPRINT-S12G-SPEC
version: v2 (post codex review)
title: Sprint S12.G — Governance Engine + Operator Authority · Sprint Spec
scope: G.A v0/v1 split + expanded L1-exclusive + L2 8-category taxonomy + 11 NEW schema fields + 8 NEW forbidden combinations + plugin base 11 capabilities + cross-phase preflight 7 checks + Override Review Lifecycle (ADR-0034) + Core vs Plus delivery split
status: Draft (2026-05-13)
relates_to:
  - ADR-0033 (Governance Engine + Operator Authority)
  - ADR-0034 (NEW: Override Review Lifecycle + Separation of Duties)
  - Sprint S12: Bedrock (11-phase ticket specs; this sprint enforces S12 governance)
  - S12.G v1 (pre codex review, superseded)
  - Codex S12.G review (2026-05-13, /tmp/s12g-codex-review-final-retry.txt)
---

# Sprint S12.G · Governance Engine + Operator Authority — Spec v2

## §0. v2 Changelog (vs v1 — driven by codex review)

**Codex bottom line**: "Ship a small enforceable kernel first, then expand. The dangerous outcome is not delay; it is a governance engine that appears authoritative before it is actually complete."

**P0 BLOCKING fixes:**

| Item | v1 problem | v2 fix |
|---|---|---|
| **G.A hard-blocker too big** | G.A 2-3 wk blocking S12 31.A → schedule pressure → AC-shrinkage temptation | v2: **G.A split into G.A-v0 (3-5d) + G.A-v1 (2-3 wk)** + revalidation gate before 31.B |
| **L1-exclusive list incomplete + over-broad** | rule too narrow ("key-material + system"); missed 8 categories | v2: rule re-keyed on **authority-persistence + blast-radius + custody + irreversibility**; +8 categories (prod-secret-rotation / CI-secret-store / DNS / billing / SSO-IdP / release-signing-policy / persistent-authority-grant) |
| **L3 runner refusal incomplete bypass** | JQL only catches polling; target-override fetches bypass; pre_pickup_ok doesn't check class | v2: **runner-side refusal contract**: `any operator-window-* label TAKES PRECEDENCE over subscription-*` + `pre_pickup_ok` enforces authority class + target-override pickup also blocked |
| **L2 boundary unclear** | "Most operator-window except L1-exclusive" is principle, not rule | v2: **8-category machine-readable L2 reason taxonomy**: execution-only / witness-only / approval-only / credential-entry / production-touch / release-governance / roster-mutation / override-action / destructive-action |

**P1 STRUCTURAL fixes (schema gaps from codex Q4):**

NEW schema fields (11) added in G.A-v1:

```yaml
authority_required: L1 | L2 | L3 | none                    # separate from class
allowed_actors: [identity_fingerprint, ...]                # explicit roster constraints
approval_required_by: identity_or_role                     # who approves
execution_allowed_by: identity_or_role                     # who executes (separable from approver)
environment_scope: local | staging | prod | external-saas | idp | release-infra
reversibility: reversible | recoverable-with-backup | irreversible
credential_custody: none | read | write | generate | rotate | destroy
waiver_id: UUID | null                                     # ties to override audit log (per ADR-0034)
override_id: UUID | null                                   # if filing under override
schema_version: str                                        # what schema version validated this
phase_plugin_version: str                                  # what plugin accepted this
evidence_retention_path: str                               # durable evidence location
evidence_required_before_unblock: [field_name, ...]        # specific fields blocker must contain
external_systems: [Gerrit | JIRA | GitLab | GHCR | Discord | SMTP | DNS, ...]
separation_of_duties: bool                                 # per ADR-0034 rules apply
```

Expanded forbidden combinations (8 new):
```yaml
forbidden_combinations:
  # ADR-0033 v1 baseline
  - key-material + meta-closure
  - synthetic + l1_exclusive_reason (UNLESS execution_mode is structural-only OR test-only)   # SOFTENED
  - operator-window-deputy + l1_exclusive_reason
  # NEW v2 per codex Q4
  - operator-window-top WITHOUT l1_exclusive_reason
  - meta-closure WITHOUT operator-window-top
  - l1_exclusive_reason: roster-mutation WITHOUT signed roster mutation evidence
  - credential-material | key-material WITH class: subscription-*
  - destructive_op_scope: system WITH class: subscription-*
  - tag_type: lightweight WITH destructive_op_classes containing release-tag-create
  - cross_phase_blockers FAILED WITH non-L1 override
  - network-production WITH runtime_capability: unit-only
```

**P2 PLUGIN ARCHITECTURE (codex Q5):**

G.A plugin base class explicit interface (11 capabilities):
1. Declare: `phase_id`, `plugin_version`, `schema_versions_supported`, `ruleset_id`
2. Extension field definitions (NOT mutating base schema)
3. `validate(ticket) -> [Violation]` with **stable error codes**
4. Phase fixtures: valid / invalid / migration / cross-phase-blocker examples
5. Declare cross-phase blockers + semantic evidence requirements
6. Declare L1-exclusive conditions + deputy-allowed conditions
7. Declare forbidden_paths / required_paths / path canonicalization rules
8. Migration hooks (from older payload versions)
9. **DETERMINISTIC output** — no network calls inside `validate()`
10. **Separate pure validation from preflight** — preflight uses NetworkClient passed in
11. Plugin version policy — explicit semver

Cross-phase preflight expanded **3 → 7 checks** (codex Q5):
1. Blocker JIRA status = Closed
2. Blocker commit SHA matches recorded evidence
3. Semantic field at expected value
4. **NEW**: evidence file exists at pinned commit (NOT current workspace)
5. **NEW**: commit SHA reachable from expected branch/ref
6. **NEW**: JIRA recorded evidence path matches committed artifact
7. **NEW**: evidence schema/version (not just one semantic field)
8. **NEW**: blocker was not reopened/superseded after evidence recorded
9. **NEW**: phase plugin version used for blocker is compatible with current filing
10. **NEW**: cache + snapshot preflight result for later audits

**P3 SCOPE REDUCTION (codex Q6):**

Sprint split: **S12.G-Core (must) + S12.G-Plus (deferred to post-rc2)**

| Sub-phase | First delivery (Core) | Deferred (Plus / post-rc2) |
|---|---|---|
| G.A-v0 | ✓ (3-5d; blocker for S12 31.A) | — |
| G.A-v1 | ✓ (2-3 wk; before 31.B revalidation) | — |
| G.B | ✓ (11 plugins; per-phase) | — |
| G.C | ✓ (filing hook + audit log + override path) | — |
| G.D | **MINIMAL** (pickup refusal only) | **Full audit cron deferred** |
| G.E | — | **DEFERRED (until violations exist + audit log shape stable)** |
| G.F | **SCOPE-LIMITED** (S12-active enforcement tickets only) | **Broad legacy migration → post-rc2 sprint** |

**P4 ADR + ESCALATION:**

- **NEW ADR-0034**: Override Review Lifecycle + Separation of Duties (companion to ADR-0033)
- **Escalation queue (operator decides)**:
  - Roster crypto model (encrypted + git-tracked + decrypted runtime copy)
  - L1/L2 boundary for prod secrets / CI secrets / DNS / billing / IdP

**Ticket count**: v1 ~115 → v2 **~75 (Core) + ~30 (Plus, post-rc2)**

---

## §1. Pre-flight reading (updated)

1. ADR-0033 (foundation)
2. **NEW**: ADR-0034 (Override Review + SoD)
3. All 11 S12 phase v2 specs
4. Codex S12.G review (2026-05-13)
5. Existing OP-1042 ticket
6. SOP-S12-TICKET-DECOMP

---

## §2. Sprint META definition (unchanged structure; updated Plus deferred note)

[See v1 §2; Sprint META AC now reflects Core-only deliverables for rc2]

---

## §3. Schemas (v2 extended)

[See §0 above for 11 NEW fields + expanded forbidden_combinations + softened synthetic rule]

### §3.4 L1-exclusive rule v2 (codex P0 refinement)

L1-exclusive applies when ANY of:
- `external_payload_class: key-material` AND `credential_custody: generate|rotate|destroy`
- `external_side_effect: meta-closure`
- `l1_exclusive_reason` explicitly set (must justify)
- `authority_required: L1` explicitly set
- ticket grants `persistent_authority` to another identity (e.g., adding to roster)
- ticket touches `external_systems: DNS | billing | idp`
- `reversibility: irreversible` AND `environment_scope: prod | release-infra`
- ticket modifies release-signing policy (cosign key + release tag policy)
- ticket performs `credential_custody: rotate|destroy` on production secrets

Rule key: **authority persistence + blast radius + custody + irreversibility** (NOT just "key-material + system").

### §3.5 L2 8-category reason taxonomy (codex P0)

When `class: operator-window-deputy`, ticket MUST declare `l2_reason`:

| l2_reason | Semantics |
|---|---|
| `execution-only` | Deputy executes a pre-approved runbook step; no new judgment |
| `witness-only` | Deputy witnesses + records an operation (e.g., smoke test) |
| `approval-only` | Deputy approves a runner-prepared artifact (e.g., deploy script) |
| `credential-entry` | Deputy enters credentials at prompt (e.g., 31.A-2b operator-prompt) |
| `production-touch` | Deputy interacts with prod systems within pre-approved scope |
| `release-governance` | Deputy participates in release process (but NOT release tag itself — L1) |
| `roster-mutation` | **NOT ALLOWED for L2** — roster mutations are L1-exclusive |
| `override-action` | **NOT ALLOWED for L2** — override is L1-exclusive |
| `destructive-action` | Deputy executes a destructive op within pre-defined bounded scope |

Filing-hook rejects ticket if `class: operator-window-deputy` AND `l2_reason in [roster-mutation, override-action]`.

### §3.6 L3 runner refusal contract v2 (codex P0)

Runner pickup MUST refuse tickets per these precedence rules:
1. If ANY `class:operator-window-*` label present → refuse (regardless of subscription-* also present)
2. `pre_pickup_ok()` enforces authority class check (not just labels)
3. Target-override pickup (direct ticket fetch) MUST also apply rule 1+2 — NOT bypassed
4. Filing-hook rejects tickets with BOTH `subscription-*` and `operator-window-*` (no mixed-class)

---

## §4. Children — overview table (Core: ~75; Plus: ~30 post-rc2)

### S12.G-Core (immediate)

| Sub-phase | Tickets | Go-Live | Blocker for |
|---|---|---|---|
| **G.A-v0** | ~8 | 3-5d (MUST be S12 31.A blocker) | **S12 31.A filing** |
| **G.A-v1** | ~18 | 2-3 wk | **S12 31.B revalidation gate** |
| **G.B** | ~28 | concurrent w/ S12 31.A-31.B | preflight strict by 31.E |
| **G.C** | ~15 | concurrent w/ S12 31.B-31.E | filing strict by 31.G |
| **G.D-minimal** | ~6 | concurrent w/ S12 31.E onwards | pickup refusal LIVE |

Core total: ~75 tickets.

### S12.G-Plus (deferred to post-rc2 sprint)

| Sub-phase | Tickets | Note |
|---|---|---|
| G.D-full | ~10 | Full cron audit + drift detection |
| G.E dashboard | ~15 | Wait until violations exist + audit log shape stable |
| G.F broad migration | ~10 | Beyond S12-active tickets; legacy reach-back |

Plus total: ~35 tickets (post-rc2 sprint, separate planning).

---

## §5. G.A-v0 spec (NEW; codex P0 critical path)

```yaml
title: "AUDIT-31.G-A-v0: Minimum viable schema kernel (S12 31.A filing BLOCKER)"
tier: M (small but load-bearing)
prefer: claude
class: subscription-claude + operator-window-top
l1_exclusive_reason: governance-engine-foundation
context_hint:
  produces: |
    Minimum viable kernel that unblocks S12 31.A filing in 3-5 days:
    1. Canonical base schema (Pydantic) for 11 v2 boundary fields (loc_delta_max, files_touched_max, required_paths, forbidden_paths, non_goals, interface_contract, test_scope, destructive_op_classes, destructive_op_scope, scope_components, external_side_effect, external_payload_class, runtime_capability, dependency_artifacts, execution_mode, mutex_with, evidence_class, on_scope_creep, scope_summary_max_chars, tag_type)
    2. Class enum (5 values) + l1_exclusive_reason + l2_reason
    3. Forbidden-combinations validator (10 rules from §3 above)
    4. ONE 31.A fixture (golden ticket payload that validates)
    5. L3 runner-pickup refusal contract: any operator-window-* label triggers refusal; pre_pickup_ok enforces
    6. Manual override procedure (CLI-only; L1-fingerprint required; logs to ~/.config/omnisight/governance-overrides/)
  consumed_by: "S12 31.A filing (BLOCKING); G.A-v1 (extends)"
  outputs_guaranteed: "v0 schema covers the MUST-HAVE; v1 extends to full coverage"
  non_goals:
    - "DO NOT cover all 11 phase plugins (G.B owns)"
    - "DO NOT include JSON Schema export (G.A-v1)"
    - "DO NOT include preflight library (G.A-v1)"
    - "DO NOT include roster signature verification (G.A-v1)"
boundaries:
  loc_delta_max: 600
  files_touched_max: 8
  required_paths: [governance_engine/schema/v0.py, governance_engine/tests/test_v0_kernel.py, scripts/governance/manual-override.py, docs/sop/g-a-v0-kernel-spec.md]
  forbidden_paths: [governance_engine/schema/phase_plugins/, governance_engine/preflight.py]
  test_scope: inline
  destructive_op_classes: []
  destructive_op_scope: none
  external_side_effect: none
  external_payload_class: operational
  execution_mode: unit-testable
  runtime_capability: unit-only
  authority_required: L1
  reversibility: reversible
  environment_scope: local
  external_systems: []
  schema_version: v0
  phase_plugin_version: N/A
ac.code:
  - {desc: "Pydantic model importable + validates v2 boundary fields", verify: {command: "python3 -c 'from governance_engine.schema.v0 import TicketContract; t = TicketContract(loc_delta_max=80, files_touched_max=1, required_paths=[\"x\"], forbidden_paths=[], non_goals=[\"a\"], destructive_op_classes=[], destructive_op_scope=\"none\", external_side_effect=\"none\", external_payload_class=\"operational\", test_scope=\"defer-to-integration\")'", expect_exit_code: 0}}
  - {desc: "ONE 31.A fixture validates", verify: {command: "pytest governance_engine/tests/test_v0_kernel.py::test_31a_fixture_valid -v", expect_exit_code: 0}}
  - {desc: "synthetic bad payloads rejected (10 forbidden_combinations)", verify: {command: "pytest governance_engine/tests/test_v0_kernel.py::test_forbidden_combinations -v", expect_exit_code: 0}}
  - {desc: "L3 runner refusal: operator-window-top label causes pickup refusal", verify: {command: "pytest backend/tests/test_jira_dispatch_authority_refusal.py -v", expect_exit_code: 0}}
  - {desc: "Manual override CLI exists + requires L1 fingerprint", verify: {command: "test -x scripts/governance/manual-override.py && grep -cE 'l1_identity|fingerprint' scripts/governance/manual-override.py", expect_stdout_match: "[1-9]"}}
go_live: T+3-5d (HARD blocker for S12 31.A filing)
```

---

## §6. Filing batch order (v2 — Core then Plus)

```
S12.G-Core timeline:
  W0:   G.A-v0 starts (BLOCKER for S12 31.A)
  W0-1: G.A-v0 closes → S12 31.A filing begins (parallel)
  W1-3: G.A-v1 (full schema + plugins infrastructure)
  W3:   G.A-v1 revalidation gate (re-validate all filed 31.A tickets against v1; backfill failures)
  W3-7: G.B (11 phase plugins; concurrent with S12 31.A-31.E)
  W5-9: G.C (filing hook + override audit log; concurrent with S12 31.E onwards)
  W7-10: G.D-minimal (pickup refusal only; runtime); concurrent with S12 31.G onwards

S12.G-Plus (POST-RC2 sprint; not in this sprint scope):
  G.D-full
  G.E dashboard
  G.F broad migration
```

---

## §7. Operator answers (all locked 2026-05-13)

| # | Question | Locked answer |
|---|---|---|
| 1 | Sprint naming | **Sprint S12.G: Governance Engine** (separate sprint, dependency-linked to S12) |
| 2 | G.A-v0 timeline | **3-5d** (tight kernel; codex P0 recommended) |
| 3 | Deputy initial roster size | **1** to start; expand as needed (half-year review) |
| 4 | Roster file storage | **B: git-tracked GPG-encrypted + decrypted runtime copy on prod host** (more complex than plain YAML but stronger audit + tamper evidence; codex Q6d escalation) |
| 5 | G.C override path UI | **CLI-only** (no auth surface to harden) |
| 6 | G.D-minimal vs G.D-full | **G.D-minimal in Core (pickup refusal only); G.D-full deferred to post-rc2** |
| 7 | G.E dashboard timing | **post-rc2** (wait until violations exist + audit log shape stable) |
| 8 | L1/L2 boundary per category | **Per recommendation table** (see §7.1 below) |
| 9 | Override reviewer rotation | **L1 + designated L2 alternate weekly** (per ADR-0034) |
| 10 | Schema migration v0→v1 | **B (modified): Breaking transform with 4 mitigations** (see §7.2 below) |

### §7.1 L1/L2 boundary lock (per Q8)

| Category | Authority | Rationale |
|---|---|---|
| Production secret rotation (API tokens / DB passwords) | **L1** | irreversibility + blast radius high |
| CI secret store mutation (GitLab CI variables) | **L2** | bounded scope; pre-approved rotation cadence |
| DNS / domain ownership changes | **L1** | external irreversible authority |
| Billing / vendor account ownership | **L1** | persistent authority transfer |
| SSO / IdP / admin role changes | **L1** | persistent authority grant |
| Release signing policy changes (cosign key rotation) | **L1** | trust root mutation |
| Adding new replication target | **L2** | reversible; bounded |
| Adjusting alert volume cap (31.C-4d) | **L2** | tuning op; reversible |
| Rotating individual webhook URLs (Discord/SMTP per-channel) | **L2** | bounded; rotation runbook exists |

### §7.2 Schema migration v0→v1 (per Q10 — modified B)

**Policy**: Breaking migration with 4 mitigations.

- v0 schema is **designed for v0 use only** (does NOT pre-include v1 fields)
- v1 ship at W3 includes **explicit transform script** (v0 → v1) shipped as part of G.A-4bc
- At W3 revalidation gate: transform script runs against ALL v0 tickets filed during W0-W3

**4 mitigations** (codex risk reduction):

1. **Transform script MUST have --dry-run mode** — operator runs twice (dry-run → review diff → apply)
2. **Transform script bundled in G.A-v1 ship** — not written hastily at W3; tested with fixtures in G.A-v1 Integration
3. **Each v0 ticket transform is a separate Gerrit commit** — grep-able audit trail; per-ticket revert if needed
4. **Tickets where transform fails (insufficient derivation or operator-judgment needed) → file follow-up ticket** — does NOT block W3 gate; tracked separately; must clear before G.B starts

**Transform derivation rules** (auto for most v1 fields):
- `class:operator-window-top` → `authority_required: L1`
- `class:operator-window-deputy` → `authority_required: L2`
- `class:subscription-*` → `authority_required: L3`
- `external_side_effect: meta-closure` → `environment_scope: release-infra`
- `external_payload_class: key-material` → `credential_custody: <inspect; needs operator input>`
- `destructive_op_scope: system` + `external_side_effect: network-production` → `reversibility: <inspect>`

Fields requiring operator review (NOT auto-derived):
- `allowed_actors`
- `approval_required_by` vs `execution_allowed_by` separation
- `reversibility: irreversible vs recoverable-with-backup`
- `credential_custody: rotate vs destroy`

These create follow-up tickets per mitigation #4.

---

## §8. Submission flow

1. ADR-0033 v1 + ADR-0034 v1 + this spec at `docs/sprint-s12/sprint-s12g-governance-engine-spec.md` (v2)
2. Operator §7 lock (10 items; #8 needs per-category decision)
3. Commit + Gerrit +2 + submit (ADR-0033 + ADR-0034 + spec)
4. Filing script files Sprint S12.G META + **G.A-v0 ticket FIRST** (highest priority; blocks S12 31.A)
5. G.A-v1 + G.B + G.C + G.D-minimal file concurrent with S12 phases
6. S12.G-Plus (G.D-full + G.E + G.F broad migration) → new sprint planning post-rc2

---

**End of Sprint S12.G spec v2 (~75 Core + ~30 Plus deferred; G.A v0/v1 split; 11 NEW schema fields; ADR-0034 override review; codex-recommended scope reduction). Awaiting operator §7 lock.**
