---
id: ARCH-MEMO-2026-05-14
title: Runner-as-Product Pivot — Strategic Reframe + 3-Layer Architecture Proposal
status: In-discussion (operator 2026-05-14); Q1 + Q2 unresolved
date: 2026-05-14
authors: Claude (interactive session) + sora (operator)
related:
  - docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md (event chain: incident → tactical patch)
  - docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md (tactical patch: G.A-v2 defense)
  - ADR-0007 Multi-Provider Subscription Orchestrator (per-tenant cost calibration)
  - ADR-0011 Sprint D Implementation Plan (canonical "OmniSight is multi-tenant" statement)
  - ADR-0015 Cross-task awareness 3D memory architecture (orthogonal 3-axis query model; see §4.1)
  - ADR-0018 Event-driven Release Pipeline (current_tenant_id() resolver, Y4 scope swap)
  - ADR-0023 Foundation Rebuild
  - ADR-0033 Governance Engine
  - ADR-0034 Override Review Lifecycle
  - ADR-0035 Runner FSM + Error Handling
  - docs/architecture/agents/rate_limiter.md, postgres_stores.md (KS.1 multi-tenant rollout plan)
  - TODO.md (empirical evidence of original runner success at smaller scale)
---

# Runner-as-Product Pivot — Strategic Reframe + 3-Layer Architecture Proposal

## §0. Origin

This document is the third in a chain captured 2026-05-14:

1. **Event** — host-reboot incident retrospective (`docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md`)
2. **Tactical patch** — G.A-v2 Runtime Defense Contract spec (`docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md`)
3. **Strategic reframe** — this document

The chain emerged in this order because **each stage exposed a deeper layer of the same problem**. The incident exposed missing defense (gaps ⑤–⑨). G.A-v2 drafted the defense contract. **Then a meta-question — "why did we keep missing things?" — surfaced this strategic-level reframe**: the runner has been carrying two incompatible missions, and **the second mission is the real source of the chaos**.

## §1. Project history recap (what's been true vs what was assumed)

| | What was true | What was assumed |
|---|---|---|
| **Origin** | Runner was internal dev tool; scope: ship this project's tickets | (same — correct origin decision) |
| **Empirical proof** | TODO.md (6,851 lines, 631 tickets across 11 sprints + 8 priority tiers S0–S10, MP/FX2/RPG/BP/WP/CL/HD/L4/L5/HE) — shipped by the original simpler runner | (same — empirical proof original design worked at smaller scale) |
| **2026-04..05 strategic pivot** | Operator observed runner shipping work well at small scale; decided "let it evolve into product" so it doesn't lose existence after this project completes | **THIS IS THE STRATEGIC MISJUDGMENT** — the pivot quietly mixed two missions with incompatible requirements |
| **Current state (2026-05-14)** | 22.4 labels/ticket; 45% of recent tickets carry `runner-blocked`; 50% of SP-B-X 21 patch tickets are "fix previously broken behavior" not new capability | "Runner is broken" — actually the runner isn't broken; **its scope is** |

The TODO.md scale + completion evidence is important: **the original runner concept is not invalidated by the current mess.** The mess emerged when scope expanded — not because the original architecture was wrong for the original scope.

## §2. The two missions that conflict

The current runner is asked to serve two missions that have **fundamentally incompatible requirements**:

| Dimension | dev-runner (mission A) | user-agent (mission B) |
|---|---|---|
| Mission end condition | Tickets done | Never (user keeps using) |
| Input | Structured JIRA ticket | Arbitrary natural language |
| Output | git commit + Gerrit PS | Whatever the user needs |
| Failure tolerance | Fail loudly (block ticket) | Gracefully degrade (continue session) |
| Cost model | Subscription CLI fixed cost | Per-user billable; must enforce budget |
| Failure-mode set | Enumerable (ADR-0035 FSM + SP-B-X C1–C12 ≈ 12 classes) | **NOT enumerable** — comes from arbitrary user requests |
| Growth model | Doesn't need to grow (this project finishes someday) | **Must grow autonomously** — 3D memory, self-reflection, evolution |
| Tenancy | Single-tenant (operator) | **Multi-tenant** with strict isolation (see §4) |
| Authority model | Operator authority (ADR-0033 L1/L2/L3) | Per-user authority + system meta-authority (NEW) |
| Privacy model | None required (operator owns all data) | **Strict per-tenant isolation + anonymized system-level rollup only** |

Stacking "never-ending + non-enumerable failure + must grow autonomously + multi-tenant" onto a runner originally designed to "finish a ticket and exit cleanly" produces:

- Ad-hoc exception handlers per new failure mode → `runner-blocked` at 45%
- Workaround labels per new capability → 22.4 labels/ticket
- Runner state leaking into JIRA labels because runners need shared coordination → ~30% of label volume is volatile B-class
- SP-B-X 21 patches that fix yesterday's breakage but can't preempt tomorrow's
- Repeated burned-money incidents in past Claude API attempts (residuals still in codebase) — cost guard wasn't independent of executor, so when executor failed, budget enforcement failed with it

**Conclusion**: the runner isn't broken. Its scope is. The mess is the runner correctly responding to incompatible requirements being stacked on it.

## §3. The 3-layer architecture proposal

The structural answer is **3 layers, not 2** — operator's instinct to split was right but the split is more useful with a shared platform underneath:

```
              ┌──────────────────────────────────────┐
              │       Layer 1: Platform primitives    │
              │   (shared; mandatory; tenant-aware)   │
              │                                      │
              │  Multi-model adapter                   │
              │    (claude / codex / gemini / grok /   │
              │     qwen / deepseek × sub / API / SDK) │
              │  Cost ledger (tokens × dollars,        │
              │    per-tenant + system-aggregate)      │
              │  3D memory + relational                │
              │    (polymorphic; see §4)               │
              │  Capability registry                   │
              │    (model × task fit × cost × MTBF)    │
              │  Coordination substrate                │
              │    (Postgres locks, claim state,       │
              │     tenant-scoped queues)              │
              │  Failure taxonomy + recovery primitives │
              │    (extends ADR-0035 invariants)        │
              └────────┬──────────────────┬──────────┘
                       │                  │
              ┌────────┴────────┐  ┌──────┴────────────────┐
              │  Layer 2a       │  │  Layer 2b              │
              │  dev-runner     │  │  user-agent (product)  │
              │                 │  │                        │
              │  JIRA poll      │  │  User session manager  │
              │  Worktree mgmt  │  │  Natural-language task │
              │  Git / Gerrit   │  │    decomposition       │
              │  ADR-0035 FSM   │  │  Output streaming      │
              │  TODO list      │  │  Per-user billing      │
              │  Ticket-driven  │  │  Rate limiting         │
              │  Operator-only  │  │  Auth / multi-tenant   │
              │                 │  │  Conversation context  │
              │  Enumerable     │  │  Per-tenant memory     │
              │  Finishes       │  │  Never finishes        │
              │  Fail loud      │  │  Degrade gracefully    │
              └─────────────────┘  └────────────────────────┘
                 build this proj           sell to users
                 (operator dog-foods       (the actual product)
                  by USING this layer
                  to build Layer 2b)
```

### How this resolves operator-stated pains

| Pain (operator-stated) | Resolution |
|---|---|
| Coordination breaks at N=2; N=6 will be catastrophic | Layer 1 substrate (Postgres locks); N runners no longer collide quadratically |
| 6 model families × sub/API/SDK matrix is intractable | Layer 1 adapter pattern; each new model = 1 adapter, 1 week of work, no Layer 2 changes |
| Past API/SDK attempts burned money | Layer 1 cost ledger is **independent of executor**; executor crash ≠ budget enforcement failure |
| 3D memory + relational + self-reflection requirement | Layer 1 memory primitive; both Layer 2a and 2b consume it polymorphically |
| Runner carrying too much weight | Layer 2a shrinks to "this project only"; SP-B-X 21 patches all become Layer 2a-internal concerns, not platform concerns |
| User-facing needs are unbounded and unpredictable | Layer 2b is a fresh design with user requirements as input from day 1, not a retrofit |
| Dog-fooding belief | **Still dog-fooded — at a different level**: Layer 2a (runner) builds Layer 2b (product) on top of Layer 1 (platform). Operator uses Layer 2a to build the user product. Runner doesn't need to BE the product to dog-food it. |

**"Runner doesn't need to BE the product"** is the key cognitive release. Layer 2a can be small, stable, and admit "I'm just a dev tool" — without the existential burden.

## §4. Multi-tenant constraint (existing architectural truth — see ADR-0011)

> **Correction 2026-05-14 (post-publish)**: v1 of this doc claimed multi-tenant was "first surfaced today" — that was wrong. Operator pointed out the prior documentation. Multi-tenant is **existing architectural truth**, recorded in:
> - **ADR-0011 Sprint D Implementation Plan** §Context line 26: *"OmniSight is a live multi-tenant product"* + §Rejection line 197: *"OmniSight is a multi-tenant SaaS with paying customers"*
> - **ADR-0007 Multi-Provider Subscription Orchestrator**: per-tenant cost calibration (R-MP.2 mitigation)
> - **ADR-0018 Event-driven Release Pipeline**: `current_tenant_id()` resolver + per-tenant deduplication + Y4 work-stream scope swap
> - **Implementation in flight**: `docs/architecture/agents/rate_limiter.md` and `postgres_stores.md` both reference a planned **KS.1 multi-tenant rollout** that will patch `tenant_id = $N` into every query
> - **Code already references it**: 10+ backend files have `tenant_id` / `current_tenant` references (`skill_distiller`, `sandbox_prewarm`, `legacy_llm_credential_migration`, `models`, `db_context`, `host_metrics`, `sandbox_capacity`, `llm_credential_resolver`, `enterprise_web_stack`, `security/auth_event`)

**What was actually new on 2026-05-14** is the *strategic-level mapping* of the multi-tenant constraint onto the proposed 3-layer architecture — this hadn't been done before because the 3-layer concept itself didn't exist before today:

1. **Multi-tenant propagates uniformly through Layer 1 primitives**, not as a Layer 2 retrofit. Every primitive (cost ledger / memory / capability registry / coordination substrate) must have tenant-awareness as a first-class API parameter — not an after-the-fact filter.
2. **Polymorphic memory model** (§4.1 below) is the first time multi-tenant constraint + ADR-0015 3D memory architecture + autonomous-evolution requirement are explicitly stitched together into a single design.
3. **Cross-pollination via Layer 1 aggregate** (operator's Q2 framing) — system can evolve from collective experience without violating tenant isolation. The privacy boundary lives at the Layer 1 anonymization step, not at Layer 2 read time.

The constraint itself is not new; **what's new is the 3-layer mapping** of it. This is an important distinction — KS.1 work and the existing tenant-scoped code in backend/ should be honored, not bypassed, by the Layer 1 primitive design.

### Privacy + security requirements

1. **Per-tenant data isolation** — all user data (memory, conversation, generated artifacts, billing, capability profile) MUST be fully isolated per tenant. No tenant can observe another tenant's data via any system surface.
2. **System-level meta-observation is permitted** — the platform itself MAY observe aggregate usage patterns across tenants (anonymized, statistical-only) to inform system evolution. This is the "system learns from collective experience" requirement that lets self-reflection scale.
3. **No PII in aggregate** — aggregate observations must strip identifying information. Compliance posture (GDPR / CCPA / regional analog) is a downstream concern but the architecture must support it from day 1.

### Implications per Layer 1 primitive

| Primitive | Tenant-scoped | System-aggregate | Notes |
|---|---|---|---|
| **Cost ledger** | Required (per-user billing) | Permitted (model cost trends, capacity planning) | Tenant view: my spend. System view: cost-per-model across users (anonymized) |
| **3D memory + relational** | Required (a tenant's memory only contains their data) | Permitted (anonymized failure patterns, learning rollups) | See §4.1 polymorphic memory model |
| **Capability registry** | Read-only per tenant (what's available to me?) | Owned by system | Tenant doesn't customize the registry; system does |
| **Coordination substrate** | Per-tenant queues + locks | Cross-tenant orchestration for system-internal work | Tenant work never touches other tenant queues |
| **Multi-model adapter** | Per-tenant credentials (for API/SDK with user-supplied keys) | Shared infra (for subscription path) | Mixed-mode authentication |
| **Failure taxonomy + recovery** | Per-tenant logging | Anonymized cross-tenant failure trends | "Class C7 happens 12% on Haiku" type rollup |

### §4.1 Polymorphic memory model (orthogonal to ADR-0015 3D)

> **Disambiguation**: ADR-0015 "Cross-task awareness 3D memory architecture" defines **3 query axes** (Structural / Temporal / Causal) for memory access at runner pickup time. That ADR is runner-internal and tenancy-silent (0 `tenant` mentions in 204 lines).
> What this section adds is **orthogonal**: 3 *shapes* (different stores, different lifetimes, different ownership) — not 3 *axes within one store*. The two concepts compose: each shape below can be queried along ADR-0015's 3 axes independently.

Operator's prior discussion about "memory enabling self-reflection and autonomous evolution" requires polymorphism along the OWNERSHIP / LIFETIME / SCOPE axis, because the two missions use memory differently:

| Memory shape | Used by | Scope | Retention | Purpose |
|---|---|---|---|---|
| **Short-term episodic** | Layer 2a dev-runner | Per-ticket | Hours | Recover from a crashed pickup; re-establish context after restart |
| **Long-term per-tenant** | Layer 2b user-agent | Per-tenant | Years (user-controlled) | User memory: "what we've talked about, what they prefer, what they're building" |
| **Anonymized aggregate** | Layer 1 platform | Cross-tenant, no PII | Indefinite | System evolution: "tasks of shape X tend to fail on model Y at step Z" |

Each shape has different:
- Access patterns (episodic = read-write hot path; long-term = read-frequent; aggregate = batch)
- Privacy guarantees (episodic + long-term = strict isolation; aggregate = irreversibly anonymized)
- Storage backends (episodic = Redis or fast KV; long-term = Postgres/Neo4j per tenant; aggregate = data warehouse style)

A single memory primitive that conflates these will fail privacy compliance OR fail to support evolution OR fail to handle volume. **3D memory means at least these 3 shapes are first-class, with separate APIs and isolation contracts**.

## §5. Decisions locked today

| # | Decision | Rationale |
|---|---|---|
| D1 | **Runner-as-product pivot was the strategic misjudgment** — naming it explicitly | Frees Layer 2a from product-aspiration scope creep |
| D2 | **3-layer architecture** (Platform + dev-runner + user-agent) is the target shape | Resolves the operator-listed pains structurally |
| D3 | **Multi-tenant constraint (existing per ADR-0011) propagates uniformly through every Layer 1 primitive** | Honor existing KS.1 rollout plan + tenant_id code; uniform Layer 1 propagation is the new contribution |
| D4 | **Memory is polymorphic** (3 shapes: episodic / per-tenant / aggregate) | Supports privacy + evolution simultaneously |
| D5 | **G.A-v2 reframed** — defense_contract belongs to Layer 1 primitives, not to runner specifically | Layer 2a and 2b both inherit defense from Layer 1 |

## §6. Open questions (still undecided; operator's framing preserved)

### Q1: Layer 2b origin — green-field vs fork-from-runner?

| Approach | Pros | Cons |
|---|---|---|
| **Green-field** | Clean architecture; designed for user needs from day 1; no inherited mental-model baggage | Slow; loses dog-fooded learnings already in runner; starts from zero |
| **Fork from runner** | Fast; inherits operational learnings | Inherits all baggage including wrong mental model |
| **Hybrid (extract → selectively fork)** | Extract Layer 1 primitives first; then selectively port Layer 2a components to Layer 2b where requirements overlap | Most complex; longest sequencing; but probably best end-state |

**Operator's stated position**: undecided. The trade-off is the source of the indecision — both extremes have real costs, and a clear "best of both worlds" hasn't surfaced.

**Provisional recommendation** (not yet locked): hybrid. Extract Layer 1 primitives first (months 1–4); Layer 2b starts as green-field application that consumes those primitives; selective Layer 2a code is ported only when its abstraction is generic enough to make sense at Layer 2b's scope.

### Q2: Cross-pollination between Layer 2a and Layer 2b — independent or learning from each other?

| Approach | Pros | Cons |
|---|---|---|
| **Fully independent** | Maximum isolation; easiest privacy story; lowest coupling | Each system relearns lessons the other has already learned |
| **Cross-pollinating via Layer 1 aggregate** | Both feed anonymized failure / capability data to Layer 1 aggregate memory; Layer 1 redistributes generalized learnings | Privacy boundary must be airtight (system aggregate ≠ tenant data exposure); architectural complexity |

**Operator's stated position**: leaning toward cross-pollination via Layer 1, gated on privacy compliance. The multi-tenant constraint **makes this necessary** — system can't evolve if every tenant's failure is invisible to the platform, but tenants must not see each other's failures.

**Provisional recommendation** (not yet locked): cross-pollination via Layer 1 aggregate, with **strict anonymization at the Layer 1 boundary** (not at Layer 2 read-time). This means Layer 2a and 2b emit anonymized failure events to Layer 1 aggregate; Layer 1 redistributes synthesized learnings (e.g., "tasks of shape X fail more on model Y") downstream. No cross-tenant data flow at the application layer.

## §7. Path forward (proposed; operator-confirmable)

A 6-month sequencing that honours: "product is in production, can't pause; gradual evolution; each step independently verifiable."

| Phase | Work | Estimated | Outcome |
|---|---|---|---|
| **0. Clarify + draft** | Write this document (done); audit past Claude API attempts (residuals + why each failed); finalize Layer 1 primitive boundaries | 1–2 weeks | Concrete spec input for §1 below |
| **1. First primitive extraction: Cost ledger** | Highest-leverage isolated primitive; without it, API/SDK runners cannot exist safely | 2–3 weeks | API/SDK runner becomes structurally feasible |
| **2. Coordination substrate** | Migrate `claim:*` mutex + runner state from JIRA labels to Postgres; SP-B-X-001/003/012 align here | 2–3 weeks | Drops `runner-blocked` rate dramatically; resolves "runner state leak" |
| **3. Multi-model adapter** | Unified interface for claude / codex; designed to support gemini / grok / qwen / deepseek as future adapters | 4–6 weeks | New model = 1 adapter ≈ 1 week, not a runner rewrite |
| **4. 3D memory + relational (polymorphic)** | The hardest piece; 3 shapes (episodic / per-tenant / aggregate); privacy-by-design from day 1 | 4–8 weeks | Layer 1 ready to support Layer 2b |
| **5. Layer 2a slim-down** | Remove product-aspiration code from runner; runner becomes thin app on Layer 1 | 2–3 weeks | runner is "just a dev tool" again |
| **6. Layer 2b green-field bootstrap** | New product application built on Layer 1; first user-facing tenant onboarding | 6–12 weeks | The actual product comes online |

**Total: ~6 months minimum.** No fast path. But each phase is independently shippable and verifiable; the system stays operational throughout (strangler pattern).

### Interaction with G.A-v2 Runtime Defense Contract

The G.A-v2 spec (`docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md`) drafted today should be **reframed as Layer 1 defense_contract** rather than runner-specific defense:

- The 5-dimension contract (error / exception / exit / recovery / rescue) applies at the platform level.
- Family ⑤⑥⑦⑧⑨ + ⑩ become defense instances declared by Layer 1 primitives, inherited by Layer 2a and 2b.
- This makes G.A-v2 more valuable, not less — its output is used twice (both apps), not once (just runner).
- G.A-v2 v1 draft can ship to codex review **as-is**, with the understanding that Phase 1 / 2 / 3 of the path above will further deepen its scope.

## §8. What this document is NOT

- Not a final architecture decision (Q1 + Q2 still open)
- Not an implementation plan (Phase 0–6 sequencing is provisional)
- Not a replacement for ADR-0023 Foundation Rebuild — Foundation Rebuild is still the right concept; this document refines its scope by clarifying the runner-product boundary that Foundation Rebuild was implicit about
- Not a retrospective of operator decisions — the runner-as-product pivot was a reasonable bet given what was known at the time. This document captures the realization that the bet now needs to be re-played differently, not that it was wrong to make

## §9. Cross-references

- **Event source**: `docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md` (the incident that triggered today's deep-dive)
- **Tactical patch**: `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` (G.A-v2 50-ticket Runtime Defense Contract)
- **Concept ancestor**: `docs/adr/ADR-0023-foundation-rebuild.md` (the broader Foundation Rebuild concept this refines)
- **Runner contract**: `docs/adr/ADR-0035-runner-fsm-and-error-handling.md` (current runner FSM, will continue to govern Layer 2a)
- **Governance**: `docs/adr/ADR-0033-governance-engine-and-operator-authority.md` (Layer 2a authority; Layer 2b adds per-user authority on top)
- **Historical evidence**: `TODO.md` (6,851 lines; empirical proof original runner shipped at smaller scale)
