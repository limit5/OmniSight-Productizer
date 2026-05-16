---
title: Sprint H event-driven release retrospective
date: 2026-05-12
ticket: OP-954
labels: [meta:retrospective, sprint-h, release-conductor, event-driven-release]
scope: code delivery, design outcomes, and H8 synthetic validation only
---

# Sprint H Event-Driven Release Retrospective

**Scope**: code delivery, design outcomes, and H8 synthetic validation only.

**Out of scope**: real production release cycle-time improvement, live
webhook delivery SLOs, and long-running production operator workload. Those
belong to the first RELEASE retrospective that runs with L3 enabled.

## 0. Sprint Goal

Sprint H set out to move the Sprint G release conductor from a poll-driven
baseline to an event-driven wakeup layer without replacing the JIRA graph.
The goal was not "build a second conductor." The goal was to reduce release
latency and improve operator visibility while preserving these Sprint G
invariants:

- one release or hotfix META remains the operator-visible object,
- `blockedBy` links remain the pickup gate,
- webhooks and internal events are hints over the graph,
- missed events degrade to G3 cron behavior,
- dashboards and notifications remain read models.

The sprint succeeded as a code-delivery and synthetic-validation sprint. It
does not yet prove production cycle-time reduction because no live release
has completed under L3.

## 1. Child Outcomes

| Child | Ticket | Outcome |
| --- | --- | --- |
| H1 | OP-946 | ADR-0018 defined the event subscription matrix, auth contracts, idempotency keys, and fallback posture. |
| H2 | OP-947 | Durable event router and worker path shipped for persisted release events, retry, and dead-letter classification. |
| H3 | OP-948 | Release state machine added per-version state and transition logging. |
| H4 | OP-949 | Operator approval UI replaced JIRA +2 as the release approval surface. |
| H5 | OP-950 | Hotfix label handling added the auto-cherry-pick trigger path. |
| H6 | OP-951 | Event-driven notification fan-out extended the Sprint G notification read model. |
| H7 | OP-952 | Compliance audit ledger added immutable append-only release evidence. |
| H8 | OP-953 | Synthetic dry-run and load test validated the L3 stack under one happy run and one five-release burst. |
| H9 | OP-954 | ADR finalization, this retrospective, the operator runbook, and the reusable L-OP lesson close the sprint. |

All implementation children H1 through H8 were `公開済み` in JIRA when H9
began on 2026-05-12. The Sprint H META, OP-945, was still `To Do` at H9
pickup; the forward transition is runner/operator-owned after this patch
lands.

## 2. What Worked

### Sprint G invariants survived the migration

The most important outcome is that L3 did not seize release authority from
JIRA. ADR-0018 says events wake the graph; they do not replace it. The H8
load test exercised that shape by pushing events through the worker and
reconciling outcomes against release state instead of trusting arrival order.

This is the right boundary for release automation. It lets webhooks improve
latency while leaving operators with the same release META and child tickets
they already inspect.

### Durable queue before synthetic load

H2 and H3 landed before H8 measured the system. That sequencing mattered:
the load test measured the queue, worker, state machine, approval seam, and
ledger together. A spike against ad-hoc in-memory callbacks would not have
answered the production question.

### Approval became an operator product surface

H4 moved the release approval moment out of hidden JIRA status handling and
into a deliberate UI/API surface. The H8 happy run records one operator
click and zero JIRA touches. That is the right shape for a release gate:
explicit, auditable, and narrow.

### H8 reported caveats instead of hiding them

The H8 report did not overclaim. It named two non-blocking gaps:
`operator.approval.{granted,aborted}` lacks an ADR-0018 dispatch row, and
build/stage/publish edges are not fully event-driven yet. Both are real
follow-ups, but neither invalidates the core GO decision because the graph
and cron fallback still carry the release.

## 3. What Was Underestimated

### The event matrix needed a final verdict

H1 could not remain a proposed ADR after H8 produced empirical data. The
load-test result changed the document from "design proposal" to "accepted
operator decision." Closing H9 had to update the ADR, not merely add a
retrospective beside it.

### "Event-driven" sounds more complete than it is

Only part of the pipeline is fully event-driven today. Canary transitions
flow through row #9; other edges still rely on the G1/G3 graph and polling
path. That is acceptable because the architecture explicitly treats L3 as a
wakeup layer, but the wording can mislead operators if the handoff does not
say when to choose L1, L2, or L3.

### META closure is still a workflow concern

H9 can provide the closing docs, but it should not race the runner's forward
transition flow. The Sprint H META closure belongs to the normal review,
merge, and publish process. The retrospective records this because OP-690
already showed that manual forward transitions can create misleading runner
signals.

## 4. Child-by-Child Notes

### H1 - OP-946

H1 established ADR-0018 as the contract: external webhook rows, internal
event rows, auth posture, idempotency keys, and failure-mode handling. The
final H9 update accepted the ADR and recorded the H8 GO verdict.

### H2 - OP-947

H2 turned events into durable work. Persisting release events before worker
dispatch is the key reliability boundary because it gives retries and
dead-letter classification a concrete row to inspect.

### H3 - OP-948

H3 provided the state machine that keeps release state explicit. That state
does not replace the JIRA graph; it gives L3 a local operational record and
transition log.

### H4 - OP-949

H4 made release approval a first-class operator action. The H8 happy path
proved the approval seam by using the real API route through FastAPI
`TestClient`.

### H5 - OP-950

H5 carried the event-driven pattern into hotfix work. The H8 load test
included two hotfix releases in the interleaved burst, which is important
because urgent release paths are where hidden coupling usually appears.

### H6 - OP-951

H6 extended notification fan-out. It remains a read model: notification
delivery improves visibility but does not become release authority.

### H7 - OP-952

H7 added the ledger that makes release evidence replayable. The H8 happy
run reported seven ledger rows and an intact hash chain.

### H8 - OP-953

H8 supplied the go/no-go evidence. The spike walked one synthetic release to
`done`, drained 42 events across five interleaved releases with zero
dead-letter rows, and measured p95 event-to-handler latency at 10.908 ms
against a 1000 ms coordination target.

### H9 - OP-954

H9 closes the design loop with four artifacts:

- ADR-0018 accepted with the H8 verdict,
- this retrospective,
- L-OP-954 for the reusable operator lesson,
- `docs/operations/event-driven-release-runbook.md` for L1/L2/L3 operator
  handoff.

## 5. Lessons

1. **Finalize the ADR only after the empirical gate.**
   H1 created the matrix; H8 supplied the verdict. H9 had to connect them
   so future implementers do not treat a pre-test ADR as final truth.

2. **Name residual poll-driven edges honestly.**
   Operators can use L3 safely only if they know which edges are event
   wakeups and which edges still depend on the graph/cron path.

3. **Treat load-test caveats as follow-ups, not footnotes.**
   A GO decision with known gaps is fine when the gaps are classified and
   non-blocking. H8's F1/F2 findings should become follow-up tickets before
   production reliance grows.

4. **Do not race workflow automation during META closure.**
   The runner owns forward transitions and Gerrit comments after CLI exit.
   Manual forward transitions create duplicate or misleading signals.

5. **The operator handoff is part of the architecture.**
   Event-driven release automation is not usable until the operator has a
   short answer for when to use L1, L2, or L3 and where to escalate.

## 6. Follow-up for First L3 Release

The first production release that uses L3 should record:

- actual webhook delivery latency by source,
- whether any source fell back to G3 cron,
- whether the operator approval event produced a classified worker outcome,
- whether build/stage/publish edges remained polling-bound,
- whether notification and dashboard state matched the JIRA graph,
- whether any dead-letter row required operator action,
- total operator clicks and manual JIRA touches.

That data belongs in the RELEASE retrospective, not in this Sprint H
retrospective.

## 7. Retrospective Conclusion

Sprint H succeeded at adding an event-driven release wakeup layer over the
Sprint G conductor. The core safety property survived: JIRA remains the
source of truth, while events reduce waiting and improve operator feedback.

The main design outcome is the accepted ADR-0018. The main operational
lesson is L-OP-954: close the event-driven loop with empirical evidence
before declaring the operator path final.
