---
id: L-OP-2561
ticket: OP-2561
title: A dead SLO is hollow coverage
date: 2026-07-08
tags: [audit, observability, slo, project-state, anti-pattern]
---

# A dead SLO is hollow coverage

**Situation**: The project-state audit loop found a dangerous observability
shape: an SLO can exist, render, and look like coverage while querying a metric
family that the service does not emit. In the `slo_monitor` project-state case,
that meant the SLO could not fail loudly when project-state axis health
disappeared. It was a green wrapper around an absent signal, which is the
observability version of deployed-but-hollow.

**Fix**: Treat metric absence as an SLO failure condition. The project-state
Phase S cure pairs the source-health contract with Prometheus/Grafana checks:
axis and per-half markers are emitted as metrics, content has a non-empty-rate
alert, staging has a probe with known non-empty expected output, and alert rules
include both absence checks and degraded/disabled/unavailable ratio checks. A
query over no series is invalid coverage, not a passing SLO.

**Verification**: OP-2561 records the audit-loop doctrine in three doc
surfaces: anti-pattern #15 in `docs/sop/architecture-anti-patterns.md`, this
lesson, and the OP-2561 amendment to
`docs/adr/ADR-0015-cross-task-awareness-3d-memory-architecture.md`. The
runtime producers and alert rules are tracked by the published blockers
OP-2557 and OP-2560.

**Generalisation**: Every SLO must prove its denominator exists before it can
claim to measure reliability. For Prometheus-backed SLOs, add a companion
`absent()` or equivalent no-data rule for each required metric family, and make
the no-data path page or fail the gate at the same severity as the bad-data
path. A silent SLO over a missing metric is not conservative; it is hollow
coverage.
