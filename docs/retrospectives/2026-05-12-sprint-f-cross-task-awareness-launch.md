---
title: Sprint F cross-task awareness launch retrospective
date: 2026-05-12
ticket: OP-914
labels: [meta:retrospective, sprint-f, cross-task-awareness]
scope: code delivery and design outcomes only
---

# Sprint F Cross-task Awareness Launch Retrospective

**Scope**: code delivery and design outcomes only.

**Out of scope**: runtime canary metrics, real ontology drift events, real
agent drift trends, and live canary outcomes. Those belong to the v0.5.1
RELEASE retrospective after the F13/F15 surfaces have production signal.

## 0. Pre-Sprint F Audit Accuracy

The pre-Sprint F audit estimated the right category of gap: cross-task
awareness was not a single missing model capability, but a missing
integration layer. The work that landed confirms that estimate.

What the audit got right:

- The runner needed a three-axis project-state payload rather than more
  hand-pasted operator text.
- The causal axis needed `runner_incidents` backfill before useful failure
  recall could exist.
- Cognee needed ontology governance before structural KG results could be
  trusted in prompts.
- The feature needed a canary and dashboard path because prompt injection
  changes runner behavior in ways unit tests cannot fully predict.

What was underestimated:

- Integration glue outweighed adapter code. Each mature product brought its
  own schema, observability, failure modes, and operator procedure.
- The Sprint vs RELEASE boundary needed to be explicit. Sprint F can close
  code delivery and design, but production runtime lessons require v0.5.1
  release data.
- "Memory" is not one operational surface. Storage caps, KG drift, temporal
  MCP availability, causal recall quality, and agent drift all need separate
  owners.

## 1. Phase 0 — Readiness and Scope Lock

Outcome: successful design lock.

Sprint F started with the right constraint: avoid trying to prove live
runtime outcomes during the sprint. The release-conductor pattern now
separates Sprint retrospective material from RELEASE retrospective material:
Sprint F records whether the code, docs, and architecture landed; v0.5.1
records whether real rollout behavior validated them.

The main lesson is scope discipline. A sprint that builds observability must
not invent observations before the system has run.

## 2. Phase 1 — Infrastructure Spec Quality

Outcome: strong, but larger than estimated.

The core infrastructure split into specialised source contracts:

| Source | Sprint F role | Outcome |
| --- | --- | --- |
| Memory Tool | Per-fleet scratchpad storage and cap monitoring | Storage and monitoring runbooks define host paths, cap policy, and dashboard feed |
| Graphiti | Temporal project memory | MCP deployment runbook gives the temporal axis an operational home |
| Cognee | Structural KG | Ontology governance and drift detection define class growth and alert handling |
| `runner_incidents` | Causal failure memory | Backfill lineage gives the causal axis a corpus and schema |
| Project-state API | Integration boundary | `/api/v1/project-state` exposes structural, temporal, and causal axes with budgets |

The spec quality was good where it named budgets, degradation, and owners.
The weak point was effort estimation: the adapter itself was only a slice of
the work. Governance and operator artifacts were load-bearing.

## 3. Phase 2 — Integration Patterns

Outcome: the axis contract is the right abstraction.

The project-state API became the durable boundary between memory products
and runner prompts. That kept the prompt-builder from depending on Cognee,
Graphiti, Memory Tool, and incident recall internals directly.

The successful pattern:

- Fetch structural, temporal, and causal context independently.
- Keep per-axis budgets visible.
- Treat missing axis data as degraded context, not as a hard runner failure.
- Cache by ticket and develop SHA so context is stable for a pickup.
- Expose the same signals to operators through F15.

The integration pattern should be reused for future memory sources. New
sources should attach to an axis or justify a new axis; they should not
inject directly into runner prompts.

## 4. Phase 3 — Drift and Observability Code Completeness

Outcome: delivery complete enough for launch, runtime evidence deferred.

Sprint F delivered the observability contracts needed before rollout:

- Memory Tool monitoring writes per-fleet usage and stale-file candidates.
- Cognee drift runbook defines stale, missing, and schema drift responses.
- Agent drift reporting defines monthly success, time-to-complete, and
  lessons-used trend thresholds.
- Project-state metrics expose cache, latency, axis errors, and budget
  breaches.
- F15 joins Memory Tool, Cognee, agent drift, project-state SLO, and the F7
  feature flag into one operator dashboard.

This is code completeness, not runtime validation. The retrospective does not
claim that production drift rates are acceptable, that canary success is
proven, or that the context improves runner outcomes. Those claims need live
data.

## 5. Phase 4 — Spike Verdicts

Outcome: reject Hopfield/HDC for the current use case.

F12 answered the largest research question: Modern Hopfield and HDC should
not replace the pgvector/cosine causal recall path for `runner_incidents` in
Sprint F.

The key result was not just empirical. One-step Modern Hopfield top-K is
order-equivalent to cosine top-K for any positive beta because softmax is
monotonic. The only variant that could differ, iterated retrieval, did not
clear the recall or latency gate in OP-910's spike. HDC was therefore not
worth adding as another production memory stack for the same ranked-recall
use case.

The verdict preserved focus: ship the integration layer over existing
sources and avoid adding another research surface.

## 6. Open Items for v0.5.1 RELEASE Retrospective

The v0.5.1 RELEASE retrospective should pick up these runtime questions:

1. Did F13 canary stages complete without SLO breach, runner success-rate
   drop, or revert-loop signal?
2. Did `/api/v1/project-state` stay under the 2 second total budget at
   production query volume?
3. Which axis failed most often, and did graceful degradation preserve runner
   usefulness?
4. Did Cognee produce real ontology drift events, and were the ignore/review
   paths sufficient?
5. Did Memory Tool cap pressure require operator-approved cap changes?
6. Did agent drift trends show improvement, regression, or insufficient
   baseline?
7. Did cross-task context change ticket completion behavior enough to justify
   its LLM/API cost?

No answers are recorded here because the data does not exist yet.

## 7. Retrospective Conclusions

Sprint F succeeded as a code-delivery and design sprint. It established the
3D memory architecture, kept mature memory products in their specialised
roles, rejected the Hopfield/HDC detour, and provided the operator dashboard
and canary path needed for release validation.

The main design outcome is ADR-0015: cross-task awareness is an integration
layer with governed memory sources, not a replacement-memory purchase. The
main operational lesson is L-OP-914: multi-memory integration should be
estimated around glue and governance, because that is where production cost
appears.

Runtime validation remains open by design and should be handled by the next
v0.5.x RELEASE retrospective.
