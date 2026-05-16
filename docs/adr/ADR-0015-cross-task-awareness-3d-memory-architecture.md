---
id: ADR-0015
title: Cross-task awareness 3D memory architecture
status: Proposed
date: 2026-05-12
---

# ADR 0015 — Cross-task awareness 3D memory architecture

**Status**: Proposed (Sprint F / OP-914; awaiting Gerrit human +2)

**Decider**: sora (operator) + AI fleet

**Related**:
- OP-900 / F2 — Memory Tool storage
- OP-901 / F3 — Graphiti MCP deployment
- OP-902 / F4 — `runner_incidents` backfill
- OP-903 / F5 — Cognee initial ECL ingestion
- OP-904 / F6 — `/api/v1/project-state` aggregator
- OP-905 / F7 — prompt-builder injection gate
- OP-907 / F9 — Cognee drift detection
- OP-908 / F10 — Memory Tool monitoring
- OP-909 / F11 — agent drift metrics
- OP-910 / F12 — Hopfield / HDC spike
- OP-911 / F13 — Sprint F canary runbook
- OP-913 / F15 — cross-task awareness dashboard

## Context

The operator's coordination problem is not "the runner needs more text".
The problem is that the relevant text lives across several mature but
separate systems:

- JIRA knows current ticket shape, parent META, blockers, siblings, labels,
  and status transitions.
- Git/Gerrit know the code and review history.
- Cognee stores structural knowledge over code, docs, ADRs, and lessons.
- Graphiti stores temporal/project-history memory.
- `runner_incidents` and failure-class tooling store causal links between
  past runner failures and current ticket risk.
- Anthropic Memory Tool stores per-fleet scratchpad state that helps an
  agent carry lightweight working notes across pickups.

Before Sprint F, the operator filled the gap manually: read JIRA, search
docs, remember recent failures, and paste context into runner prompts. That
does not scale as the fleet grows. It also creates a failure mode where a
ticket looks locally simple but is globally risky because a sibling ticket,
prior incident, ontology drift, or recent agent regression is invisible at
pickup time.

Sprint F therefore introduced cross-task awareness as a production
integration problem, not as a single new memory product. The architecture
joins three axes at runner pickup:

| Axis | Question | Primary sources |
| --- | --- | --- |
| Structural | "Where does this ticket sit in the project graph?" | JIRA graph, Gerrit/develop SHA, Cognee KG |
| Temporal | "What similar work happened recently?" | Graphiti MCP, ticket/release history |
| Causal | "What failure patterns are likely here?" | `runner_incidents`, failure classes, lessons |

The API contract is documented in
`docs/operations/project-state-api-runbook.md`: a single
`GET /api/v1/project-state?ticket=<key>` call returns those axes with
per-axis budgets and graceful `null` degradation. F15 then exposes the
operator-facing dashboard for the same signals.

## Decision

Adopt a **3D cross-task awareness architecture** for runner pickup:

1. **Three axes are explicit and independently budgeted.**
   Structural, temporal, and causal context are distinct sections in the
   project-state payload. Each axis may fail or time out without blocking
   the other two.
2. **Memory grows autonomously, but schema growth is governed.**
   Runtime facts may accumulate through ingestion, monitors, and drift
   reports, but ontology classes and durable schema changes require review.
   Cognee class growth is controlled by `config/cognee_entity_classes.yaml`
   and the weekly proposal process in
   `docs/operations/cognee-ontology-governance.md`.
3. **The integration layer is the product boundary.**
   The runner consumes `/api/v1/project-state`, not Cognee, Graphiti,
   Memory Tool, and failure graph directly. The dashboard likewise reads
   the aggregated surface so operators see the same mental model the runner
   used.
4. **The first production target is context injection, not autonomous
   runtime judgment.**
   Sprint F ships code delivery and design outcomes. Real canary metrics,
   ontology drift events, and agent drift trends belong to the v0.5.1
   RELEASE retrospective after those signals exist.
5. **Specialised memory products stay specialised.**
   Cognee remains the structural KG. Graphiti remains temporal memory.
   Memory Tool remains per-fleet scratchpad storage. `runner_incidents`
   remains causal failure memory. The integration layer composes them
   instead of replacing them.

## Alternatives Evaluated

### A. Replace the stack with Letta — rejected

Letta would centralise agent memory under one product boundary. That is
attractive operationally, but it would discard the specialised work already
done for Cognee ontology governance, Graphiti deployment, Memory Tool fleet
storage, and `runner_incidents` failure-class recall. It also turns a
coordination problem into a migration problem before Sprint F has production
evidence that the existing sources are insufficient.

Rejected because Sprint F needed an integration layer over mature existing
systems, not a new canonical memory store.

### B. Pursue Hopfield / HDC associative recall — rejected for Sprint F

OP-910 measured Modern Hopfield retrieval against the pgvector/cosine
baseline for `runner_incidents`. The decisive finding was structural:
one-step Modern Hopfield top-K is order-equivalent to cosine top-K for any
positive beta because softmax is monotonic. The iterated variant did not
clear the recall or latency gate. HDC was not pursued because the current
ranked-retrieval use case does not justify another associative-memory stack.

Rejected because it adds research and runtime cost without improving the
causal recall contract Sprint F needs.

### C. Keep manual operator prompt assembly — rejected

Manual assembly keeps infrastructure simple, but it preserves the failure
mode Sprint F exists to remove. The operator remains the only join point
between JIRA, memory, KG, incidents, and live dashboard signals. That is not
repeatable across multiple runner fleets or release cycles.

Rejected because it does not scale and cannot be verified by code.

### D. One flat "project context" blob — rejected

A flat blob would be easier for prompt injection but harder to observe. It
would hide which source failed, make budget breaches ambiguous, and tempt the
runner to treat missing causal context the same as missing structural context.

Rejected because observability and graceful degradation require axis-level
status.

## Consequences

### Positive

- Runner pickup has a single contract for cross-task context.
- Operators and agents share the same three-axis mental model.
- Each backing store can evolve independently behind the aggregator.
- Partial failure is survivable: one axis can be `null` while the rest of
  the payload still ships.
- Sprint F can validate code delivery now and defer real runtime outcomes
  to the v0.5.1 release retrospective.

### Negative

- The integration code is larger than the product specs implied. Five
  mature memory products require glue, governance, fallback paths, and
  dashboard/readiness artifacts.
- LLM and API cost rises because useful context is now fetched and injected
  systematically rather than only when the operator remembers to paste it.
- Operators own more monitoring surfaces: Memory Tool cap usage, Cognee
  ontology drift, Graphiti availability, project-state latency, and agent
  drift trends.
- Debugging moves from "one memory product failed" to "which axis degraded,
  which source inside that axis failed, and what did the runner see?"

## Schema Lock-in Dependencies

This ADR intentionally locks the integration layer to two schema families:

1. **Cognee entity classes.**
   `config/cognee_entity_classes.yaml` is the canonical class list. Renames
   are migration events, not local edits, because KG nodes carry class labels
   and queries are class-aware. The project-state structural axis depends on
   those classes remaining meaningful.
2. **`runner_incidents` schema.**
   The causal axis depends on stable incident fields and failure-class
   taxonomy from the F4 backfill lineage. Changing the schema or enum shape
   changes recall semantics and therefore requires a co-change to causal-axis
   retrieval, reports, and retrospective interpretation.

These dependencies are acceptable because both schemas are governance
surfaces, not volatile implementation details.

## Monitoring Obligations

The architecture is only production-safe if the following stay visible:

- Project-state latency and per-axis errors via
  `/api/v1/project-state/metrics`.
- Memory Tool cap pressure via `memory_tool_monitor` metrics.
- Cognee ontology drift via the F9 drift runbook and ignore list.
- Agent drift via the F11 monthly report.
- F7 rollout health via the F13 canary runbook and F15 dashboard.

Runtime values from those monitors are explicitly not recorded in this ADR.
They belong to the v0.5.1 RELEASE retrospective once the rollout has real
data.

## Scope Boundaries

This ADR does not approve a runtime rollout by itself. It records the
architecture chosen by Sprint F. It also does not pre-judge canary success,
ontology drift frequency, real agent drift, or live LLM cost; those are
release-retrospective facts.
