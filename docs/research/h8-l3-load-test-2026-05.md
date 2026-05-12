# Spike Report — H8 L3 event-driven release conductor: synthetic dry-run + load test (OP-953)

**Date**: 2026-05-12
**Author**: claude-bot (under operator oversight per OP-953)
**Spec**: `docs/adr/ADR-0018-event-driven-release-pipeline.md` + Sprint H META
**Harness**: `scripts/spike_l3_load_test.py` (re-run: `python3 scripts/spike_l3_load_test.py --write-report`)
**Tests**: `tests/integration/test_l3_load_test.py`
**Scope**: spike + load test only. No production code changes. Out-of-area
domains untouched: db, devops, embedded, frontend, security, tooling. The
harness exercises `backend/release_conductor/*` and `backend/api/release_approval.py`
through their public test seams (`set_engine_for_tests`, `register_handler`,
the FastAPI router) against an in-memory sqlite engine with the 0232/0233/0234
schemas applied.

---

## TL;DR — go/no-go for L3 production cutover

**GO** — proceed to L3 production cutover, with the two follow-ups below filed.

| Case | Result | Headline metric |
|---|---|---|
| 1 — synthetic happy run (`v0.99-h-rc1`) | ✅ pass | walked `pending → done`; **1 operator click**, 0 JIRA touches; 10 pipeline events |
| 2 — 5-parallel load (rc + 2 customer + 2 hotfix) | ✅ pass | 42 events emitted → **42 done, 0 DLQ**; drained in 0.0115s (3654.1 ev/s) |
| 3 — latency under target | ✅ pass | p95 **10.908 ms** vs target 1000.0 ms (n=52, p99=11.249 ms, max=11.334 ms) |

**Error catalog (per ticket):** `LoadTestEventLoss` — not observed (event loss
0, DLQ 0). `LatencyExceedsTarget` — not observed (p95 well under target).

---

## 1. Methodology

The harness stands up an isolated L3 stack: the H2 `release_events` durable
queue + worker (`backend/release_conductor/{event_router,worker}.py`), the H3
`release_state` machine, and the H7 compliance ledger, all on an in-memory
sqlite engine. It then synthesises the webhook events the ADR-0018 subscription
matrix would deliver in production and pushes them through the *same* code path
as a live event: `event_router.persist_event` → `worker.process_one_event` →
`event_handlers.dispatch` → handler → `mark_done`. The single operator
interaction is performed against the **real H4 router** (`backend/api/release_approval.py`)
via a FastAPI `TestClient`, so the "1 click" claim is exercised end to end
(`record_decision` + the H2 dispatch of `operator.approval.granted`).

Hermetic substitutions (documented so the numbers are honest):

* `operator.approval.{granted,aborted}` — the H4 endpoint queues these
  (`_dispatch_decision_event`), but the ADR-0018 dispatch table has no row for
  the `operator` source, so in production they land in **dead-letter** (the
  worker treats an unrouted `(source, event_type)` as `UnknownEventType` and
  parks it immediately). The harness registers an inert stub — see **Finding F1**.
* `gerrit/hotfix-label-added` — the real handler shells out to
  `scripts/hotfix_cherry_pick.py` and `gerrit review` over SSH; the harness
  stubs it (the H5 cherry-pick logic is covered by `backend/tests/test_hotfix_label_handler.py`).
* JIRA payloads are minimal, so `release_notifications.transition_from_jira_event`
  returns `None` and the H6 fan-out short-circuits to `ignored` (H6 is
  load-tested separately under OP-951).

"Parallelism" in case 2 = the five releases' event streams round-robin-interleaved
into the one queue, then drained by a single worker — which **is** the production
topology (ADR-0018 §"Why sync": the channel is low volume and the worker is
intentionally synchronous/single-tick).

Latency = `time.perf_counter()` from `persist_event` returning to the worker
calling `mark_done` for that row — i.e. queue wait + dispatch + the handler's
state-machine write. This is the *coordination overhead the L3 layer adds*; it
does not include webhook network delivery (ADR-0018 budgets ~1–5 s for Gerrit,
~10 s for JIRA, both upstream of this harness).

---

## 2. Case 1 — synthetic happy run (`v0.99-h-rc1`)

Release id `OP-99953`. The harness walks:

`pending` →(orchestrator: build)→ `building` →(orchestrator: stage)→ `staging`
→(**event**: `canary.stage.transitioned canary_5`)→ `canary_5`
→(**event**: `canary.stage.transitioned canary_25`)→ `canary_25`
→(**operator gate**: `request_approval` + **1 click** `POST /release-approvals/OP-99953/approve`)→
→(**event**: `canary.stage.transitioned canary_100`)→ `canary_100`
→(orchestrator: publish)→ `done`

Interspersed L3 events: `gerrit/change-merged` ×2 (`develop_merge`),
`jira/jira:issue_updated` ×2 (`dashboard_breadcrumb`, `advance_next`),
`canary/canary.stage.started` (`stage_started`), `gerrit/label-added` CR+2
(`review_gate_satisfied`), `operator/operator.approval.granted` (H4 dispatch,
stubbed). The build/stage/publish state edges are driven directly by the
harness because **they are not yet event-driven** (only ADR-0018 row 9 — the
canary stage edges — flow through the worker as of H7). See **Finding F2**.

| Metric | Value |
|---|---|
| Final state | `done` |
| Pipeline events emitted | 10 |
| All events `done` | yes |
| Operator clicks (H4 web UI) | 1 |
| Operator JIRA touches | 0 |
| H3 transition-log entries | 9 |
| H7 ledger rows / chain intact | 7 / True |
| p95 / max event→handler latency | 0.6815033499151467 ms / 0.7118009962141514 ms |

Handler outcomes observed:

| Outcome | Count |
|---|---|
| `advance_next` | 1 |
| `dashboard_breadcrumb` | 1 |
| `merge_recorded` | 2 |
| `operator_decision_ack` | 1 |
| `review_gate_satisfied` | 1 |
| `stage_started` | 1 |
| `state_advanced` | 3 |

**Assertions**

| | Detail |
|---|---|
| ✓ overall | pass |
| ✓ | AC#1 — v0.99-h-rc1 walked the full L3 pipeline to 'done'. |
| ✓ | AC#2 — exactly 1 operator click (H4 web UI), 0 JIRA touches. |
| ✓ | AC#1/#3 — all 10 pipeline events dispatched cleanly. |
| ✓ | H7 ledger: 7 rows, hash chain intact. |

---

## 3. Case 2 — 5-parallel load (AC #4)

Releases (`v0.99-h-…`): v0.99-h-rc1 (rc), v0.99-h-cust-acme1 (customer), v0.99-h-cust-globex1 (customer), v0.99-h-hotfix-1+1 (hotfix), v0.99-h-hotfix-2+1 (hotfix). Each is
seeded and pre-staged to `staging`, then its ~9-event stream is interleaved
round-robin with the others and drained by one worker; the `canary_100 → done`
edge is then applied per release (orchestrator step).

| Metric | Value |
|---|---|
| Releases | 5 |
| Events emitted | 42 |
| Events dispatched / `done` | 42 / 42 |
| Events in `failed`/`dead_letter` (DLQ) | 0 |
| Drain wall-clock | 0.0115 s |
| Throughput | 3654.1 events/s |
| p50 / p95 / max latency | 6.80660386569798 / 11.059170297812669 / 11.333717964589596 ms |

Final state per release:

| Release | Final state | Expected |
|---|---|---|
| `v0.99-h-rc1` | done | `done` |
| `v0.99-h-cust-acme1` | done | `done` |
| `v0.99-h-cust-globex1` | done | `done` |
| `v0.99-h-hotfix-1+1` | done | `done` |
| `v0.99-h-hotfix-2+1` | done | `done` |

Handler outcomes observed across the burst:

| Outcome | Count |
|---|---|
| `advance_next` | 5 |
| `dashboard_breadcrumb` | 5 |
| `hotfix_label_ack` | 2 |
| `merge_recorded` | 5 |
| `review_gate_satisfied` | 5 |
| `stage_started` | 5 |
| `state_advanced` | 15 |

**Assertions**

| | Detail |
|---|---|
| ✓ overall | pass |
| ✓ | AC#4 — no event loss: all 42 events from 5 parallel releases reached 'done'. |
| ✓ | AC#4 — no DLQ: zero events in failed/dead_letter. |
| ✓ | all 5 releases (rc + 2 customer + 2 hotfix) reached 'done'. |

---

## 4. Case 3 — latency under target (AC #3)

Pooled per-event latency across cases 1 + 2 (n = 52):
p50 = 5.096 ms, p95 = 10.908 ms, p99 = 11.249 ms,
max = 11.334 ms. Target = 1000.0 ms (the L3 coordination
budget; webhook network delivery is separate per §1).

| | Detail |
|---|---|
| ✓ overall | pass |
| ✓ | AC#3 — p95 event→handler latency 10.91ms < target 1000ms (n=52, max=11.33ms). |

---

## 5. Findings

**F1 — `operator.approval.{granted,aborted}` is not in the ADR-0018 dispatch
table.** `backend/api/release_approval.py:_dispatch_decision_event` persists an
`operator`-sourced event into the H2 queue "so the worker fires the R9-style
state advance", but `backend/release_conductor/event_handlers/__init__.py:HANDLER_TABLE`
has no `("operator", …)` row, so the worker classifies it `UnknownEventType` and
parks it in `failed`/`dead_letter` *immediately* (no retry budget burned, but
the promised advance never happens and the operator's click silently no-ops
downstream of the state-machine write). The harness stubs this; in production it
is a real gap. **Recommendation:** file a follow-up to add the `operator` rows
(handler can be a thin acknowledgement that records the decision into
`handler_result_json` for idempotent replay; the H3 state edge stays owned by the
canary handlers). Not P0 — the decision *is* durably recorded by
`state_machine.record_decision` before the dispatch — but it should land before
the L3 cutover so the dashboard/runner-gate signal is complete.

**F2 — the build / stage / publish state edges are not event-driven yet.** Only
ADR-0018 row 9 (canary stage transitions) currently calls `state_machine.transition`
from a handler; `pending→building`, `building→staging`, and `canary_100→done`
are still driven by the G1/G3 polling path. The harness drives them directly.
This is consistent with the ADR (L3 is "a scheduler on top of the graph, not a
replacement") but it means the latency win quoted in ADR-0018 §Consequences
(~12 min recovered) only applies to the canary segment until those edges get
their own subscribers. **Recommendation:** file a follow-up (or fold into H9) to
wire `gerrit/change-merged`-on-`develop` and the R13-publish JIRA transition into
real state edges if the full latency win is wanted.

**F3 — the L3 handlers are robustly DLQ-proof under reordering.** Every handler
reconciles against the JIRA-graph state rather than the event stream: an
out-of-order `canary.stage.*`, an unknown stage, a not-yet-created release row,
or a routine JIRA field edit all return a classified hint and `mark_done` — they
do not raise, so they cannot reach the retry/dead-letter path. The only way an
event DLQs is `UnknownEventType` (F1) or three handler exceptions in a row; the
load test saw neither. This is the property that makes "no DLQ" hold even when
the queue is interleaved across five releases.

---

## 6. Recommendation

**GO** — proceed to L3 production cutover, with the two follow-ups below filed.

The L3 plumbing (durable queue, idempotency, FIFO worker, state machine,
ledger) carries a synthetic release end to end with a single operator click and
survives a five-release interleaved burst with zero loss and zero dead-letter,
at sub-millisecond-to-low-millisecond coordination latency. The two findings
above are additive (a missing dispatch row and an incremental latency
opportunity), not blockers. Proceed to H9 / production cutover; file F1 and F2
as follow-ups and reference them in the cutover DoD.

---

## 7. How to re-run

```bash
python3 scripts/spike_l3_load_test.py            # run all cases, print summary
python3 scripts/spike_l3_load_test.py --write-report   # also regenerate this file
python3 -m pytest tests/integration/test_l3_load_test.py -q
```

## 8. References

* `docs/adr/ADR-0018-event-driven-release-pipeline.md` — the subscription matrix + idempotency/failure-mode contracts this harness exercises.
* `docs/operations/release-conductor-runbook.md` — G1 "the JIRA graph IS the conductor" model L3 sits on top of.
* `backend/release_conductor/event_router.py`, `worker.py`, `state_machine.py`, `compliance_ledger.py` — the modules under test.
* `backend/api/release_approval.py` — the H4 web-UI router used for the single operator click.
* `backend/tests/test_event_router.py`, `test_release_state_machine.py`, `test_release_approval_api.py`, `test_compliance_ledger.py` — unit-level coverage this spike complements with an end-to-end pass.
