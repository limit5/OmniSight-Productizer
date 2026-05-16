---
id: L-OP-954
ticket: OP-954
title: Finalize event-driven ADRs only after load evidence
date: 2026-05-12
tags: [release-conductor, event-driven-release, adr, load-test, operations]
---

# Finalize event-driven ADRs only after load evidence

**Situation**: Sprint H designed the L3 event-driven release layer in
ADR-0018 before the synthetic dry-run and load test existed. H8 later showed
that the durable queue, worker, state machine, approval seam, and ledger could
walk one release to `done`, drain 42 interleaved events across five releases,
and stay far under the 1000 ms coordination-latency budget.

That evidence changed the ADR's status. Leaving ADR-0018 as `Proposed` after
H8 would make future operators guess whether the matrix was still a design
draft or the accepted cutover contract. Finalizing it without carrying H8's
two findings would be equally risky: operators would miss that approval
events need an explicit dispatch row and that build/stage/publish edges are
not fully event-driven yet.

**Fix**: Treat the empirical gate as part of ADR closure. For OP-954,
ADR-0018 was moved to `Accepted`, gained a final H8 verdict section, and
recorded the operator decision: GO for L3 cutover with two non-blocking
follow-ups. The Sprint H retrospective and operator runbook both repeat the
same boundary: events wake the JIRA graph, they do not replace it.

**Verification**: `docs/adr/ADR-0018-event-driven-release-pipeline.md`
contains the H8 load-test verdict and `LoadTestVerdictReject` rollback path.
`docs/research/h8-l3-load-test-2026-05.md` records the measured evidence:
one happy run to `done`, 42/42 loaded events processed with zero DLQ, and
p95 event-to-handler latency of 10.908 ms. The operator handoff in
`docs/operations/event-driven-release-runbook.md` states when to use L1,
L2, or L3 and where to escalate.

**Generalisation**: Any ADR that authorizes an event-driven production path
should not be finalized until the representative load or dry-run evidence is
checked in and cited. The final ADR must state:

- the go/no-go decision,
- the concrete test evidence,
- which caveats are blockers and which are follow-ups,
- the rollback path if the empirical gate rejects,
- the operator surface that turns the decision into daily procedure.

This keeps design intent, measured behavior, and operator practice in one
reviewable chain instead of spreading them across tickets and memory.
