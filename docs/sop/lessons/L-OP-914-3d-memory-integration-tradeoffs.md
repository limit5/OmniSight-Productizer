---
id: L-OP-914
ticket: OP-914
title: Budget memory integrations for glue and governance, not adapters
date: 2026-05-12
tags: [memory, sprint-f, architecture]
---

# Budget memory integrations for glue and governance, not adapters

**Situation**: Sprint F looked like a composition of mature pieces:
Memory Tool storage, Graphiti temporal memory, Cognee structural KG,
`runner_incidents` causal recall, and an operator dashboard. The hidden
cost was the integration layer between them: schemas, drift checks, feature
flags, canary gates, metrics contracts, runbooks, and fallbacks.

**Fix**: Treat multi-memory production work as a governance problem from
the start. Define the axis contract first, then require each source to name
its schema owner, degradation mode, metric surface, and retrospective owner
before it feeds runner prompts.

**Verification**: OP-914 documents the pattern in ADR-0015 and the Sprint F
retrospective. Existing source artifacts show the required surfaces:
`docs/operations/project-state-api-runbook.md`, `docs/operations/cognee-ontology-governance.md`,
`docs/operations/memory-tool-monitoring-runbook.md`,
`docs/operations/agent-drift-runbook.md`, and
`docs/operations/cross-task-awareness-runbook.md`.

**Generalisation**: Production integration of five mature memory products
requires about four times the spec'd adapter code once glue and governance
are counted. Estimate separately for adapter, schema lock, drift monitor,
operator runbook, dashboard contract, rollback path, and retrospective
boundary; otherwise the ticket looks smaller than the operational system it
actually creates.
