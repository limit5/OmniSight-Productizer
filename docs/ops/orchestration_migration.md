# Orchestration migration runbook — monolith ↔ distributed

O8 (#271).  Backing module: `backend/orchestration_mode.py`.  Tests:
`backend/tests/test_orchestration_mode.py`.

OmniSight runs the Lead-Orchestrator → specialist-agent pipeline in one
of two modes:

| Mode          | Entry point                              | Worker pool | Scope       |
| ------------- | ---------------------------------------- | ----------- | ----------- |
| `monolith`    | `backend.agents.graph.run_graph`         | none (in-proc) | default — v0.1.0 baseline |
| `distributed` | `queue_backend.push` → `worker.py` pool  | separate processes / hosts | horizontal scale |

Both modes emit the **same SSE event sequence** (see
`PARITY_EVENT_SEQUENCE` — the parity test fails CI if either path skips
a stage or adds an out-of-order one).  That parity is the migration
contract: UI subscribers, audit log, and downstream consumers cannot
tell from the outside which path ran.

This document covers:

1. [Turning distributed on (grey-deploy)](#1-turning-distributed-on-grey-deploy)
2. [Rolling back to monolith](#2-rolling-back-to-monolith)
3. [Verifying parity before widening traffic](#3-verifying-parity-before-widening-traffic)
4. [Troubleshooting](#4-troubleshooting)

> Golden rule: `OMNISIGHT_ORCHESTRATION_MODE` defaults to `monolith` on
> every release.  A binary upgrade **never** changes runtime behaviour —
> operator intent must be explicit.

---

## 1. Turning distributed on (grey-deploy)

### 1.1 Pre-flight

Before flipping the flag, confirm the distributed substrate is healthy:

```bash
# Queue backend reachable?  (Redis URL honoured if set; otherwise in-mem)
curl -fsS "$ORCH_URL/api/v1/orchestrator/check-change-ready?change_id=dummy"

# Worker pool registered?  Expect at least one entry.
curl -fsS "$ORCH_URL/api/v1/observability/workers" | jq '. | length'

# Redis auth configured for production (ACLs per O10 §3)?
redis-cli -u "$OMNISIGHT_REDIS_URL" ACL WHOAMI
```

All three MUST return a healthy response.  A missing worker pool is the
#1 cause of `distributed_wait_timeout_after_Xs` errors post-flip.

### 1.2 Per-tenant grey-deploy

Prefer per-tenant over global flips.  Use the env var on the
orchestrator pod(s) serving the cohort you want to migrate:

```bash
# Tenant-scoped orchestrator deployment.
kubectl set env deployment/orchestrator-tenant-acme \
    OMNISIGHT_ORCHESTRATION_MODE=distributed \
    OMNISIGHT_ORCHESTRATION_DISTRIBUTED_WAIT_S=600
```

Rolling restart replaces the running pods; `current_mode()` is
re-evaluated per dispatch, so the flip takes effect as soon as the new
pod serves traffic.  Existing monolith runs drain naturally (they are
in-process and will finish on the old pod before termination).

### 1.3 Widen the cohort

After 24 h of clean parity checks (see §3), flip the next cohort.  Do
not jump straight from 1 tenant to 100 % — the queue backpressure / DLQ
profile in distributed mode is materially different from monolith and
deserves a 2-cohort confirmation.

---

## 2. Rolling back to monolith

Rollback is the scenario the runbook has to get right — when you flip
from `distributed` back to `monolith`, the queue may still be holding
messages that THIS orchestrator pushed but haven't terminated yet.

### 2.1 Soft rollback (recommended)

The worker pool is still up; you just want to stop sending new work and
let the in-flight queue drain naturally.

```bash
# 1. Freeze new traffic: set mode back to monolith on the orchestrator.
kubectl set env deployment/orchestrator-tenant-acme \
    OMNISIGHT_ORCHESTRATION_MODE=monolith

# 2. Wait for in-flight to finish (the orchestrator tracks its own dispatches).
python -m backend.orchestration_drain \
    --strategy wait \
    --wait-s 600
```

The `wait` strategy polls `queue_backend.get()` for each tracked message
id until `Done` / `Failed` (or ack-deleted), then returns.  Output
example:

```json
{
  "strategy": "wait",
  "drained": ["msg-abc123", "msg-def456"],
  "redispatched": [],
  "still_pending": [],
  "elapsed_s": 42.3
}
```

If `still_pending` is empty → rollback complete, the worker pool can be
torn down.  If not, either extend the wait, or escalate to the hard
rollback.

### 2.2 Hard rollback (worker pool going away)

When the worker pool is already being torn down (e.g. container migration,
emergency kill-switch), the `wait` strategy will hang.  Use
`redispatch_monolith` instead — every still-pending dispatch gets
re-run through the monolith path so the user command still gets a
completion signal:

```bash
python -m backend.orchestration_drain \
    --strategy redispatch_monolith \
    --wait-s 120
```

Invariants you should know:

* Redispatched work runs **in addition to** any residual queue state; if
  a worker happens to finish an old message after the redispatch, the
  net effect is duplicated work, not inconsistent state.  Idempotency
  of the underlying agent (e.g. a Gerrit push) is what makes this safe —
  see O10 §4 for the idempotency guarantees we require of executor
  protocols.
* The helper only sees THIS orchestrator's dispatches.  On a multi-shard
  deployment, run drain on every orchestrator pod.  Use the hostname in
  the emitted `orchestration.dispatch.started` SSE event to tell pods
  apart.
* DLQ entries are not touched — they remain available for
  `dlq_redrive` after the rollback completes (see `queue_backend.py`).

### 2.3 Emergency stop

If distributed is misbehaving and you need an immediate hard stop:

```bash
# 1. Flip every orchestrator to monolith simultaneously.
kubectl set env deployment/orchestrator \
    OMNISIGHT_ORCHESTRATION_MODE=monolith

# 2. Optionally drop the queue backlog (ONLY if work is safe to lose).
redis-cli -u "$OMNISIGHT_REDIS_URL" DEL omnisight:queue:stream:P0 \
    omnisight:queue:stream:P1 omnisight:queue:stream:P2 omnisight:queue:stream:P3

# 3. Audit what you discarded (for SOC2 trail):
redis-cli -u "$OMNISIGHT_REDIS_URL" HGETALL omnisight:queue:dlq:entries > discard-audit.json
```

`redis-cli DEL` is destructive — do NOT run step 2 unless the CATC
contents are reproducible from Jira (they normally are) AND you have
filed a post-mortem ticket.

---

## 3. Verifying parity before widening traffic

### 3.1 Synthetic probe

```bash
# Send the same command through both modes from a test tenant.
python -m backend.orchestration_probe \
    --user-command "describe OmniSight architecture in two sentences" \
    --both

# Expect: both outcomes list the full PARITY_EVENT_SEQUENCE in order.
```

### 3.2 Prometheus invariants to watch post-flip

* `orchestration_dispatch_started_total{mode="distributed"}` should rise
  at the rate of user-request volume.
* `orchestration_dispatch_completed_total{mode="distributed", ok="true"}` /
  `started_total` stays within ± 1 % of the monolith baseline.  A sudden
  divergence is the leading signal that workers are silently DLQ'ing.
* `queue_depth{priority="P2"}` (from O2) should be < 100 under nominal
  load.  Sustained > 500 means your worker pool is under-provisioned for
  the distributed traffic.
* `orchestration_dispatch_completed_total{mode="distributed", ok="false"}`
  — inspect the attached `error` label; `distributed_wait_timeout_after_Xs`
  is the canonical "worker pool too small" signal.

### 3.3 SSE parity spot-check

In the UI, subscribe to the `invoke` SSE channel and filter
`action_type` starting with `orchestration.dispatch.`.  A healthy
distributed flow produces, per request:

```
orchestration.dispatch.started   mode=distributed
orchestration.dispatch.routed    mode=distributed  routed_to=distributed
orchestration.dispatch.executed  mode=distributed  ok=true
orchestration.dispatch.completed mode=distributed  ok=true  queue_message_id=msg-XXXX
```

Swap `mode=monolith` and `routed_to=firmware|software|…` for the legacy
path.  If the order deviates, or a stage is missing, the parity test
failed to catch a regression — file a bug and revert.

---

## 4. Troubleshooting

### 4.1 `distributed_wait_timeout_after_600s`

Cause: no worker pulled the message within the wait window.  Check:

1. `curl $ORCH_URL/api/v1/observability/workers` — is the pool empty?
2. `redis-cli XINFO CONSUMERS omnisight:queue:stream:P2 omnisight-workers`
   — any consumers registered?
3. Worker logs — are they blocked on a stuck distributed-lock or failing
   to authenticate to Redis?

Mitigation while debugging: raise
`OMNISIGHT_ORCHESTRATION_DISTRIBUTED_WAIT_S`; long-term: add more
workers or scale down distributed traffic.

### 4.2 `queue_push_failed: ...`

Cause: queue backend reject.  Usually a CATC validation error — the
synthesised CATC's `impact_scope.allowed` ended up empty.  The default
synthesis sets `["**"]` when the caller doesn't pass `allowed_globs`,
so this should never happen with library callers; it typically means a
direct `_build_catc_from_request` invocation with custom globs.

### 4.3 Parity test fails in CI

Open `backend/tests/test_orchestration_mode.py` — the parity test
serialises the event sequence from both modes and compares them.  A
failure means:

* Someone added a new emit in the monolith path (`agents/graph.py` /
  `agents/nodes.py`) without mirroring it in
  `orchestration_mode._distributed_dispatch`; or
* A new stage was inserted into `PARITY_EVENT_SEQUENCE` without
  updating one of the modes.

Fix forward — never skip the parity test.

### 4.4 Stuck `list_inflight()` entries

If `orchestration_mode.list_inflight()` keeps entries around forever, a
dispatch crashed between `_register_inflight` and `_unregister_inflight`
without cleanup.  Call `orchestration_mode.reset_inflight_for_tests()`
(despite the name, it's safe in prod — it just clears the in-process
dict; the underlying queue state is authoritative).

---

## Appendix: config reference

| Variable                                      | Default      | Effect |
| --------------------------------------------- | ------------ | ------ |
| `OMNISIGHT_ORCHESTRATION_MODE`                | `monolith`   | `monolith` / `distributed` — picked up per-dispatch |
| `OMNISIGHT_ORCHESTRATION_DISTRIBUTED_WAIT_S`  | `600.0`      | How long `dispatch()` waits for a worker verdict in distributed mode before timing out |
| `OMNISIGHT_REDIS_URL`                         | (empty)      | Unset → in-memory queue (single-process); set → Redis Streams backend |

The two env vars can be flipped live.  `current_mode()` resolves them
per-dispatch so you never need to restart the process to swap modes.
