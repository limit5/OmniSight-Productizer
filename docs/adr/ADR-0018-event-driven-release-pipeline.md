---
id: ADR-0018
title: Event-driven release pipeline — webhook subscription matrix for L3 conductor
status: Proposed
date: 2026-05-12
---

# ADR 0018 — Event-driven release pipeline: webhook subscription matrix for the L3 conductor

**Status**: Proposed (2026-05-12, Sprint H — META OP-9xx via H1 / OP-946)

**Decider**: sora (operator) + AI fleet

**Related**:
- [ADR-0010 — Deployment Automation Gates](ADR-0010-deployment-automation.md) — milestone-gate event shape that the D-track stages emit; L3 consumes the same events.
- [ADR-0011 — Sprint D Implementation Plan](ADR-0011-sprint-d-implementation-plan.md) — full D1..D18 pipeline architecture; L3 is the layer that replaces the operator-eyeball poll loop between stages.
- [ADR-0003 — Gerrit Code Review](ADR-0003-gerrit-code-review.md) — the +2 / Code-Review label-grant rules that fire `label-added`.
- [`docs/operations/release-conductor-runbook.md`](../operations/release-conductor-runbook.md) — operator artifact for G1; explicitly names "Sprint H will add a smarter scheduler on top" (line 24).
- OP-883 (D11) — SLO monitor that emits `slo.breach` (`backend/orchestrator/slo_monitor.py:423`).
- OP-882 (D10) — canary orchestrator emitting `canary.stage.{started,transitioned}` / `canary.gate.failed` / `canary.rolled_back` (`backend/orchestrator/canary.py:199,221,237,250`).
- OP-884 (D12) + OP-911 (F13) — feature-flag-gated canary that emits `sprint_f.canary.*` SSE frames (`scripts/sprint_f_canary_orchestrator.py`).
- OP-937 (G1) — release template engine that instantiates `RELEASE-vX.Y.Z` META + 13 children; the JIRA graph this ADR's L3 conductor walks.

---

## Context

The G1 release-template engine (OP-937) shipped the **JIRA-graph-as-state-
machine** model: a release is one `RELEASE-vX.Y.Z` META plus 13 child
tickets wired by `blockedBy`, and progress is driven by the runner's
pre-pickup gate (`backend.agents.file_coordinator.has_unresolved_blockedby`)
which walks the chain. Today the runner polls JIRA on a fixed cadence to
discover newly-eligible tickets — every release-stage transition incurs
one full poll interval of latency, even when the upstream stage has
already emitted a structured event that says "I'm done."

The runbook for G1 names the next step explicitly (line 24):

> *There is no external conductor service. The G2/G3/Sprint H wave will
> add operator dashboards and a smarter scheduler on top, but the
> underlying state machine is the JIRA graph itself.*

L3 in `release-conductor-pattern` (operator's working name for the
"smarter scheduler" layer) replaces the poll loop with an
**event-driven** scheduler:

- When Gerrit merges a change-id on a `develop` or `release/*` branch,
  immediately enqueue the corresponding release child.
- When a Code-Review +2 or `release:*` / `hotfix:*` label lands on a
  Gerrit change, advance the child's gate without waiting for the next
  poll.
- When a JIRA child transitions to 公開済み (Published) or 進行中 (In
  Progress), wake the children blocked on it.
- When the D11 SLO monitor (OP-883) emits a `slo.breach` event, halt
  the conductor's advance loop and surface the breach to the operator
  dashboard.
- When the D9 prod orchestrator (OP-881 — not yet built) emits a
  `canary-stage-transition` event, the conductor advances the
  matching `R8`/`R9` child or rolls back via the existing D10/D11
  rollback path.

For L3 to subscribe to these events it needs a **subscription matrix**
that names every event source, the auth contract for each, the
idempotency contract (because some sources retry on slow responses and
some don't dedupe duplicates), and the failure-mode for each (because
webhooks lose deliveries and the conductor must not stall when they
do).

### What this ADR is, and is not

- This ADR is **spec-only** — it documents the matrix and the contracts.
  No code lands with this change. H2 (handler skeletons), H3 (idempotency
  store), and H4 (failover to G3 cron) are downstream tickets that
  implement the contracts named here.
- This ADR **does** ground itself in the current code. Every "exists today"
  claim cites the file + line. Every "to be added" claim names the
  downstream ticket.

### Pre-check: ADR number conflict

ADR-0009, 0015, 0016, 0017 are gaps in the numbering sequence (verified
2026-05-12 against `docs/adr/`). 0018 is the next free slot. If a parallel
ticket between H1 design and merge claims 0018, the resolution is to
rename this file and update the `id:` front-matter — there is no
load-bearing reference to "ADR-0018" outside this document yet, so the
rename is local. Future references in H2/H3/H4 will pin to whatever
number lands.

### Decision criteria

1. **Tier-M LOC budget (~700 LOC) for design only** — no implementation
   slips into this ADR; the matrix and contracts are the deliverable.
2. **Every event source named in the AC has a row** — Gerrit (merge +
   label), JIRA (transition + label), D11 SLO breach, D9 canary
   transition. No gaps.
3. **Auth contract uses existing infrastructure** — JIRA bearer-token
   auth + Gerrit Caddy/Cloudflare tunnel auth are already deployed
   (`backend/routers/webhooks.py:1460`, `:270`). The matrix must not
   propose new auth surfaces unless current ones are demonstrably
   insufficient.
4. **Idempotency contract uses existing patterns** — the runner already
   has `backend/agents/idempotency.py` for outbound mutations
   (24h SQLite TTL); the receiver-side equivalent can mirror its shape
   rather than invent a new one.
5. **Failure-mode story is explicit per source** — the AC names "delivery
   failure, duplicate delivery, out-of-order delivery"; every row must
   say what happens for each.
6. **G3 cron-path fallback is reachable from every external row** — when
   a webhook source is unreachable, L3 degrades to the G1 poll loop
   rather than stalling.

## Decision

**Adopt a five-source webhook + internal-event subscription matrix for
L3, with a per-source auth/idempotency contract and a single shared
fallback path (G3 cron) when any external source is unreachable.**

The matrix is split into two halves:

- **External webhooks** — Gerrit and JIRA. Network-delivered POSTs; the
  L3 conductor receives them at HTTP endpoints inside the existing
  `backend/routers/webhooks.py` surface.
- **Internal events** — D11 SLO breach and D9 prod-orchestrator
  canary-stage-transition. In-process pub/sub via
  `backend.events.bus.publish` (already wired by D11 and D10/F13;
  D9 not yet built).

### The matrix

| # | Source | Event | Wire | Auth | Idempotency key | Handler | What L3 does |
|---|--------|-------|------|------|------------------|---------|---------------|
| 1 | Gerrit | `change-merged` (on `develop`) | HTTP POST → `/webhooks/gerrit` | Caddy/Cloudflare tunnel + payload-shape check (no plugin auth — see §Auth-1) | `(source="gerrit", event_id=change_id + ":" + revision + ":merged")` | `_on_change_merged` (exists, `webhooks.py:982`) → L3 dispatcher | Look up the `RELEASE-vX.Y.Z` child whose `gerrit_change_id` field matches; transition it to In Progress (it will already be there from runner pickup, so this is a no-op but logged); ungate any child whose `blockedBy` resolves. |
| 2 | Gerrit | `change-merged` (on `release/X.Y`) | same | same | same | new dispatcher (H2) | Same as #1 but additionally tags the matching `HOTFIX-*` META if the merged change carries a `hotfix:vX.Y.Z` topic. |
| 3 | Gerrit | `label-added` (Code-Review +2) | HTTP POST → `/webhooks/gerrit` (not currently routed — H2) | Caddy/Cloudflare tunnel | `(source="gerrit", event_id=change_id + ":" + revision + ":CR+2:" + reviewer_id)` | new handler (H2) | Mark the matching child's "review gate" satisfied; if the child is `Rn` with `n ∈ {2,3,5,6,7}` (the review-gated stages), advance it past the +2 dwell. |
| 4 | Gerrit | `label-added` (topic `hotfix:*` / `release:*`) | same | same | `(source="gerrit", event_id=change_id + ":" + revision + ":topic:" + topic_name)` | new handler (H2) | Resolve `topic_name` to a release/hotfix META and link the change to it via `Relates`. |
| 5 | JIRA | `jira:issue_updated` (transition → 公開済み) | HTTP POST → `/webhooks/jira` | Bearer token, secret from `git_accounts(platform='jira').webhook_secret` (`webhooks.py:1454`) | `(source="jira", event_id=issue_key + ":" + changelog_id)` | `_on_jira_issue_updated` (exists, `webhooks.py:1577`) → L3 dispatcher | When `Rn` reaches 公開済み, walk the `blockedBy` graph and ungate the next child; if it was `R13`, transition the META to Published per §10 of `jira-ticket-conventions.md`. |
| 6 | JIRA | `jira:issue_updated` (transition → 進行中) | same | same | same | same | Update the L3 dashboard's "current stage" indicator. No graph advance — 進行中 is a pickup signal, not a completion signal. |
| 7 | JIRA | `jira:issue_updated` (label set/unset on `release:*` / `hotfix:*`) | same | same | `(source="jira", event_id=issue_key + ":labelchg:" + changelog_id)` | new dispatcher (H2) | Update the release META's child set when a new child is labelled in/out. |
| 8 | Internal | `slo.breach` | `backend.events.bus.publish` (in-process) | Trusted (in-process — only D11 publishes; see §Auth-2) | `(source="slo_monitor", event_id=breach_id)` where `breach_id = sha256(error_rate + p95 + breach_window_start)` | new subscriber (H2) | Halt the L3 advance loop for the active release. Surface the breach to the operator dashboard. Do **not** auto-rollback — D11 already calls its own `RollbackTrigger` (`slo_monitor.py:445`); L3's job is to stop *forward* progress so the rollback can complete cleanly. |
| 9 | Internal | `canary.stage.{started,transitioned}` | `backend.events.bus.publish` (in-process, D10 + F13 emit) | Trusted (in-process) | `(source="canary", event_id=rollout_id + ":" + stage_name + ":" + transition_index)` | new subscriber (H2) | Advance the matching `R8` (canary kick-off) / `R9` (canary advance) child to 公開済み when the corresponding stage transition lands. |
| 10 | Internal | `canary.gate.failed` / `canary.rolled_back` | same | Trusted (in-process) | `(source="canary", event_id=rollout_id + ":" + outcome + ":" + timestamp_minute)` | new subscriber (H2) | Halt the L3 advance loop; surface the gate failure to the operator; do not roll back upstream stages (the rollback is scoped to the canary stage by D10 contract). |
| 11 | Internal | `canary-stage-transition` (D9 prod orchestrator, OP-881 — **not yet built**) | `backend.events.bus.publish` (planned) | Trusted (in-process, future) | `(source="prod_orchestrator", event_id=stage_id + ":" + transition_index)` | placeholder subscriber (H2) | Same as #9/#10 for the prod stage; placeholder no-op until OP-881 ships. |

Eleven rows total — four external (rows 1–4), three external (rows 5–7),
four internal (rows 8–11). Rows 9 and 11 are the AC §1.4 entries
(canary-stage-transition).

### What this ADR locks in

- **External wire**: HTTP POST into `backend/routers/webhooks.py`. New
  routes (rows 3/4/7) extend the existing module; do **not** stand up a
  new FastAPI app for L3 specifically.
- **Internal wire**: `backend.events.bus.publish` (in-process pub/sub).
  L3 subscribes via a new `backend.orchestrator.l3_conductor` subscriber
  that registers callbacks at startup; subscriber failure is logged
  but does not block the publisher (the SLO monitor and canary
  orchestrator already publish best-effort, see `slo_monitor.py:436` —
  "Page operator (best-effort -- never block the rollback)").
- **Auth contracts** — see §Auth, three classes:
  1. *Caddy/Cloudflare-tunnel-only* (Gerrit webhooks): the security
     boundary is the tunnel; the plugin offers no header auth (per
     `webhooks.py:256`). L3 inherits this contract.
  2. *Bearer-token with rotation* (JIRA webhooks): secret resolved
     via `get_webhook_secret_for_host_async("", "jira")` from the
     `git_accounts` table with `settings.jira_webhook_secret` fallback
     (`webhooks.py:1449`). L3 inherits.
  3. *In-process trust* (internal events): only the publishing module
     can publish; receivers do not authenticate the publisher.
- **Idempotency contract** — every external row carries a stable
  `event_id` derived from immutable fields of the event payload (change
  id + revision for Gerrit; issue key + changelog id for JIRA). The L3
  dispatcher writes `(source, event_id) → handler_result` into a
  Postgres `l3_seen_events` table with a 7-day retention sweep (the
  longest plausible release-pipeline runtime). Duplicate deliveries
  short-circuit on the existing row; out-of-order deliveries are
  re-evaluated against the JIRA graph state (the graph is the source of
  truth, not the event order).
- **Failure modes** — see §Failure modes. The matrix is **eventual-
  consistent against the JIRA graph**: every external webhook is a
  hint, never the source of truth. If the hint never arrives, the G3
  cron path (H4) re-derives the same conclusion from the JIRA REST API
  at the existing G1 poll cadence (no regression vs. pre-L3).

### What this ADR explicitly does **not** lock in

- The **storage schema** for `l3_seen_events`. H3 will choose between a
  Postgres table, a Redis hash with TTL, or extension of the existing
  SQLite store at `~/.config/omnisight/idem-keys.db`. The matrix
  constrains only the *key shape*, not the storage.
- The **subscriber registration mechanism** (decorator vs. explicit
  registry call). H2 picks; the matrix constrains only the *callback
  contract* (event payload → handler result).
- The **D9 prod orchestrator API** for row #11. OP-881 owns that
  decision; L3 reserves the row and ships a placeholder subscriber.
- The **failover trigger threshold for G3**. H4 owns it; the matrix
  asserts only that fallback exists and is reachable from every
  external row.

## Auth

### Auth-1 — Gerrit webhooks (tunnel-only, no plugin auth)

The Gerrit `webhooks` plugin v3.13.5 cannot enforce header-level auth —
OP-714 attempted to require `Authorization: Bearer` + `X-Jira-Webhook-
Secret` headers and 401'd every real event because the plugin
documentation contains no references to `header`, `auth`, `bearer`,
`signature`, `secret`, or `algorithm` (`webhooks.py:256–272`).

OP-715 moved the proactive-merger trigger to the SSH stream-events
daemon (`backend.agents.gerrit_jira_bridge`, auth at the SSH protocol
layer) and accepted that the `/webhooks/gerrit` HTTP endpoint stays
auth-less at the application layer. The security boundary is the
Caddy / Cloudflare zero-trust tunnel upstream of the backend.

**L3 inherits this contract verbatim.** New handlers for rows #3/#4
mount on `/webhooks/gerrit` and trust the upstream tunnel. If a future
incident shows tunnel auth is insufficient, the migration target is
SSH stream-events (the daemon path that survived OP-714/OP-715), not
a new HTTP signing scheme.

### Auth-2 — JIRA webhooks (bearer token with rotation)

JIRA Cloud webhooks deliver a static Authorization header with a
pre-shared secret. The current resolver is:

```text
get_webhook_secret_for_host_async("", "jira")
  → tenant-scoped lookup in git_accounts(platform='jira')
  → falls back to settings.jira_webhook_secret
```

Per `webhooks.py:1449–1462`, the secret is compared with
`hmac.compare_digest`, and the rotate endpoint in
`backend.routers.integration` mirrors a rotated secret to
SharedKV/Redis so a rotate on worker-A is visible to the verifier on
worker-B (overlay called from inside the webhook handler at
`webhooks.py:1446`).

**L3 inherits this contract verbatim.** New handlers for rows #6/#7
reuse `jira_webhook()` as the entry point and delegate to the L3
dispatcher only after the bearer check has passed.

Multi-tenant note: the resolver scopes by `current_tenant_id()`,
defaulting to `t-default` at webhook time. L3's dispatcher must NOT
trust the webhook's `tenant_id` field to bypass this scope — a tenant
A-supplied event whose secret matches `t-default`'s row is still
processed under `t-default`. The Y4 work-stream will swap the scope
to derive from the event; L3 reads `current_tenant_id()` and writes
it into the `l3_seen_events` row so per-tenant deduplication is
preserved across that swap.

### Auth-3 — internal events (in-process trust)

`backend.events.bus.publish` is an in-process pub/sub. There is no wire
between publisher and subscriber. The trust model is: *the process is
the boundary*. Any module that can `import backend.events` can publish;
subscribers do not validate the publisher.

This is consistent with how D10 (`canary.py:199`), D11
(`slo_monitor.py:423`), and F13 (`sprint_f.canary.*`) already publish.
L3 does not change the contract; it adds subscribers.

If a future deployment splits the orchestrator across processes (e.g.
the SLO monitor moves to a sidecar), the migration target is a
durable queue (Postgres LISTEN/NOTIFY, NATS, etc.) and the matrix
gains an Auth-4 contract per channel. Out of scope for L3.

## Idempotency

### Key shape

Every row in the matrix has an idempotency key of the form
`(source, event_id)` where `event_id` is derived from immutable fields
of the event payload:

| Source | event_id components | Why immutable |
|--------|----------------------|---------------|
| Gerrit `change-merged` | `change_id + ":" + revision + ":merged"` | A change-id + revision pair identifies one merged commit forever; the `merged` suffix prevents collision with future label events on the same revision. |
| Gerrit `label-added` (CR+2) | `change_id + ":" + revision + ":CR+2:" + reviewer_id` | A reviewer can re-vote +2 (which Gerrit treats as a no-op), and the webhook re-fires; the reviewer-id suffix dedupes per-reviewer per-revision. |
| Gerrit `label-added` (topic) | `change_id + ":" + revision + ":topic:" + topic_name` | Topic changes are rare but the same topic can be re-set; the topic-name suffix prevents collision. |
| JIRA `issue_updated` (transition) | `issue_key + ":" + changelog_id` | JIRA's changelog ids are monotonic and unique per issue. |
| JIRA `issue_updated` (label change) | `issue_key + ":labelchg:" + changelog_id` | Same. |
| `slo.breach` | `sha256(error_rate + p95 + breach_window_start)` | A breach is uniquely identified by its sample tuple; if the same sample fires twice (it shouldn't — D11 cooldown gate at `slo_monitor.py:29` prevents that), the hash collapses. |
| `canary.stage.*` | `rollout_id + ":" + stage_name + ":" + transition_index` | Each rollout has a fresh id; each stage transition is indexed. |
| `canary.gate.failed` / `canary.rolled_back` | `rollout_id + ":" + outcome + ":" + timestamp_minute` | Minute-bucketed because the gate can fail twice within the same minute on a rapid breach; the index field on `canary.stage.*` doesn't apply here. |
| D9 `canary-stage-transition` (future) | `stage_id + ":" + transition_index` | Owned by OP-881. |

### Store

L3 writes `(source, event_id) → handler_result + processed_at` into a
new Postgres table `l3_seen_events`. The schema is owned by H3, not
this ADR, but the matrix constrains:

- Primary key is `(source, event_id)`.
- Retention sweep at 7 days (longest plausible release-pipeline
  runtime). Sweep is a daily cron, not at-write.
- On retry, the handler re-reads the existing row's `handler_result`
  and returns it without re-invoking the side effect. This matches
  the contract used by `backend/agents/idempotency.py::IdempotencyStore`
  for outbound mutations (24h TTL there; 7d here because the
  release pipeline runs longer than a single runner tick).

### Why not reuse the SQLite store

`backend/agents/idempotency.py` is **per-runner-process**, file-backed,
and SQLite-locked. L3 is **per-backend-instance** (and there are two,
`backend-a` + `backend-b` behind Caddy LB per the Sprint D arch).
A file at `~/.config/omnisight/idem-keys.db` doesn't span the two
backends; a Gerrit webhook landing on backend-a after the same event
already landed on backend-b would be processed twice.

Postgres is the natural shared store (the deploy already has
`pg-primary` per `reference_op693_deploy_outcomes.md`). H3 will
choose between a new `l3_seen_events` table and reusing the existing
`audit.log` table with a dedup index; the matrix only requires that
the chosen store be cross-backend-coherent.

## Failure modes

The AC names three: delivery failure, duplicate delivery, out-of-order
delivery.

### Delivery failure (`WebhookSourceUnreachable`)

Symptom: the L3 dispatcher does not receive an event it was expecting
within the SLA window (per-source — Gerrit events should arrive within
seconds of the underlying state change; JIRA within ~10s; internal
events synchronously).

Detection: per-source heartbeat. The L3 conductor tracks
`last_event_at` per source. If `now - last_event_at > heartbeat_max`,
emit `l3.source.stale(source=...)` and degrade.

Recovery: **fall back to the G3 cron path**. G3 is the polling
implementation (H4 owns the ticket; G1 already does the equivalent
polling for the runner's pre-pickup gate, so the fallback uses the
same JQL + Gerrit REST queries — see `backend/agents/scheduler.py`
and `backend/agents/file_coordinator.py`). The fallback's job is to
**re-derive the conclusion** the missing webhook would have delivered
by reading the current state from the JIRA REST API + Gerrit REST
API. The release pipeline does not stall; it merely runs at G1's
poll cadence until the webhook source recovers.

Heartbeat thresholds (H4 owns final values; matrix defaults):

| Source | `heartbeat_max` | Rationale |
|--------|------------------|-----------|
| Gerrit | 5 min | Gerrit webhooks fire within seconds; 5 min is 60× the expected interval and stays below the alert noise floor. |
| JIRA | 2 min | JIRA Cloud webhooks deliver in <10s 95p; 2 min is well above the tail. |
| `slo.breach` | n/a — synchronous in-process | No heartbeat possible; if the process is alive, the publisher is reachable. |
| `canary.*` | n/a — synchronous in-process | Same. |
| D9 (future) | TBD by OP-881 | Reserved. |

### Duplicate delivery

Symptom: the same event arrives twice (Gerrit retries on slow responses
`webhooks.py:309`; JIRA Cloud retries on 5xx; in-process events can be
re-published if a subscriber crashes mid-tick).

Detection: `l3_seen_events` PK collision on `(source, event_id)`.

Recovery: short-circuit — return the cached `handler_result` without
re-invoking the side effect. This is the same contract as
`backend/agents/idempotency.py::IdempotencyStore._cached_or_call`.

Cross-handler note: some downstream effects are themselves idempotent
at the JIRA / Gerrit layer (e.g. "transition to 公開済み" is a no-op
if already there). L3's dedup is defence-in-depth — it prevents
duplicate operator-visible side effects (extra comments, extra
notifications) even when the underlying transition is idempotent.

### Out-of-order delivery

Symptom: a `change-merged` on a child arrives **before** the
`patchset-created` for the same change. Or a JIRA `issue_updated`
arrives in the wrong sequence relative to its changelog.

Why this happens: Gerrit fires webhooks per-event in event order, but
the network can re-order them. JIRA Cloud's `changelog_id` is
monotonic but the webhooks are delivered in HTTP arrival order, which
can be reordered behind a multi-instance backend.

Recovery: the matrix is **eventual-consistent against the JIRA graph
state**, not against the event stream. Every handler:

1. Reads the current JIRA state for the affected ticket (REST `GET
   /issue/{key}`) before deciding what to do.
2. Reconciles the event against the state — e.g., if a `change-merged`
   arrives and the matching child is already 公開済み, the handler
   logs and returns; if a `jira:issue_updated → 公開済み` arrives and
   the child is still 進行中, the handler emits an inconsistency
   warning but trusts the JIRA REST read as authoritative.
3. Updates `l3_seen_events` with the handler's reconciled result.

This is the same defence the G1 runbook describes in §0 (line 13):
*"the JIRA graph IS the conductor."* L3's events are hints; the graph
is truth. Out-of-order events cannot wedge the conductor because the
graph encodes the actual dependency order via `blockedBy`.

## Error catalog

| Class | When raised | What L3 does |
|-------|-------------|--------------|
| `ADRConflictsExistingNumber` | Pre-check — at ADR-authoring time, if 0018 is already taken in `docs/adr/`. | The author renames the file and updates `id:` front-matter. Currently 0018 is free (verified 2026-05-12). |
| `WebhookSourceUnreachable` | Per-source heartbeat exceeded (Gerrit 5 min, JIRA 2 min). | Fall back to G3 cron path (H4). Emit `l3.source.stale(source=...)`. Page operator if the fallback itself fails. |
| `EventSchemaMismatch` (additive) | A webhook payload is missing a required field (e.g. a Gerrit event with no `change.id`). | Log + 200 OK to the upstream (so it doesn't retry forever) + emit `l3.event.malformed`. Do not advance the graph. |
| `IdempotencyStoreUnavailable` (additive) | Postgres unreachable while looking up `l3_seen_events`. | Log + 503 to the upstream (so it does retry). Do not advance the graph. Page operator if sustained > 1 min. |

The two AC-named classes (`ADRConflictsExistingNumber`,
`WebhookSourceUnreachable`) are non-negotiable per the ticket. The two
additive classes (`EventSchemaMismatch`, `IdempotencyStoreUnavailable`)
are flagged as additive because they emerged from the
auth/idempotency design and are required to make the matrix
implementable. H2/H3 may rename or refine them; the AC entries cannot
be removed.

## Alternatives considered

### A. Keep the G1 poll loop, skip L3 entirely — rejected

The poll-loop approach (G1 today) works correctly but has two costs:

1. Per-stage latency = one full poll interval (today ~60s). Across 13
   stages that adds ~13 minutes of release latency that is purely
   coordination overhead.
2. The operator dashboard has no real-time signal — the runner picks
   up a child silently and the dashboard learns when the next poll
   reports it. Operators report this as "the release feels stuck"
   even when it's making progress.

L3's value is removing both costs. Skipping L3 keeps the costs;
rejected because the Sprint H scope already accepts the latency cost
as worth fixing.

### B. Single global "release event" channel instead of a per-source matrix — rejected

A single channel (every source posts to the same in-process bus, L3
subscribes once, dedup is purely by event id) is simpler to wire.
But it conflates two trust domains (external network events vs.
in-process events) under one auth contract, which is brittle: the
in-process channel cannot tolerate the bearer-token check JIRA needs,
and the external channel cannot tolerate the zero-auth assumption the
in-process channel uses.

Split the matrix into external/internal halves — that's what the
decision does — and the trust domains stay clean.

### C. Use Postgres LISTEN/NOTIFY instead of `backend.events.bus.publish` for the internal half — rejected for this ADR

LISTEN/NOTIFY is durable across backend-a/backend-b process boundaries,
which is a real win for the multi-instance deployment. But D11 and D10
already publish to `backend.events.bus.publish` (per `slo_monitor.py:423`,
`canary.py:199`); switching them to LISTEN/NOTIFY would mean rewriting
D11 + D10 + F13 emitters, which is out of scope for H1.

Kept on the table for a future ADR if the SSE channel proves
insufficient. The migration cost is bounded — three publishers, one
new helper module, one Postgres trigger — but it doesn't pay for
itself until the operator sees a missed event on one of those
channels, which hasn't happened yet.

### D. Skip the idempotency store; rely on JIRA / Gerrit handlers being intrinsically idempotent — rejected

Most downstream JIRA effects are idempotent at the API layer (a
transition to a target state is a no-op if already there). But the
matrix carries side effects beyond JIRA mutations:
- Operator notifications (`notify(...)` calls — duplicate pages are
  costly).
- Dashboard SSE pushes (duplicate frames spam the UI).
- Audit-log writes (duplicate rows pollute the retrospective).

The store is defence-in-depth against operator-visible duplication.
Skipping it pushes that defence to every handler, which is fragile.

### E. Subscribe to GitLab webhooks too (for the develop-merge publish pipeline) — out of scope

OP-792 publishes docs on `develop` merge via a GitHub Actions workflow,
not a webhook. The matrix doesn't need a GitLab row today. If a future
ADR moves docs publishing to a GitLab CI pipeline that emits a webhook,
that becomes a new row added by a follow-up ADR.

## Consequences

**Positive**

- Per-stage release latency drops from one poll interval (~60s) to
  webhook-delivery latency (~1–5s for Gerrit, ~10s for JIRA). Across
  13 stages, ~12 minutes of pipeline time recovered.
- The operator dashboard gains real-time events — every stage
  transition fires an SSE frame the React side already knows how to
  render (D11 + D10/F13 already publish to the same bus).
- The contracts are documented per-source, so future H-track tickets
  (and the AI fleet implementing them) have an explicit answer for
  "what auth does row N use" / "what idempotency key" / "what
  happens when row N fails."
- The graph stays the source of truth; webhooks are hints. This
  preserves the G1 invariant (line 24 of the runbook) that there is
  no external conductor service — L3 is a *scheduler* on top of the
  graph, not a replacement for it.

**Negative**

- A new Postgres table (`l3_seen_events`) joins the schema, with a
  daily sweep cron. Operational cost is small but real.
- Subscribers in `backend.events.bus` are in-process — if the
  backend-a process restarts, in-flight events miss the L3
  subscriber. The G3 cron fallback covers this, but at the cost of
  one poll interval of latency on top of the restart.
- The matrix's auth-1 (Gerrit) row inherits OP-714's accepted-but-real
  trust assumption: the tunnel is the only auth boundary. A future
  CVE in the tunnel propagates to L3 without an L3-layer defence.
  Documented as inherited residual risk.

**Neutral**

- The 11-row matrix is the maximum size we expect for the current
  release-pipeline scope. Adding rows (e.g. for hotfix specialisation
  or for a future GitLab CI publish event) is an O(1) edit per row
  with no ripple effect — H2/H3/H4 are parameterised over the row
  count.
- Row #11 (D9 prod-orchestrator) is a placeholder. The matrix locks
  in the *shape* of the row but not the payload, so OP-881 can ship
  the publisher without re-opening this ADR.

**Reversibility**

The ADR is **revertable**. Until H2 lands, no code subscribes to the
internal events on L3's behalf and no new endpoints exist on
`/webhooks/gerrit` for rows #3/#4. A revert is a single-commit delete
of this file.

After H2/H3/H4 land, the revert path is staged:

1. Disable the L3 subscriber registration in
   `backend.orchestrator.l3_conductor` (one boolean flag).
2. L3 stops processing events; the matrix becomes inert.
3. G1 polling resumes its pre-L3 cadence.
4. Drop `l3_seen_events` table at the next migration window.

No data loss — the JIRA graph is the source of truth and is
untouched.

## References

- `docs/operations/release-conductor-runbook.md` — G1 runbook;
  parent context for "what the conductor is" (line 24 names the
  Sprint H wave).
- `backend/routers/webhooks.py` — existing webhook surface; lines
  cited inline.
- `backend/orchestrator/slo_monitor.py` — D11 SLO breach emitter.
- `backend/orchestrator/canary.py` — D10 canary stage emitter.
- `scripts/sprint_f_canary_orchestrator.py` — F13 flag-gated canary
  emitter.
- `backend/agents/idempotency.py` — outbound idempotency store
  (24h SQLite); L3's inbound store mirrors its contract at the row
  level.
- `backend/events.py` — in-process pub/sub bus.
- `docs/sop/jira-ticket-conventions.md` §10 — META auto-transition to
  Published on R13 completion (the terminal condition L3 honours).
