# SSE event scope policy

**Task:** Q.4 #298 — checkbox 3 of 4 (policy write-up).
**Status:** normative; referenced by `backend/events.py::_resolve_scope`,
`backend/orchestration_observability.py`, `backend/ui_sandbox_sse.py`,
and the `test_event_scope_declared` / `test_user_scope_does_not_leak_across_users`
guards (checkbox 4).
**Owner:** software-beta.
**Last updated:** 2026-04-24.

---

## 1. Why this policy exists

Every call to `backend.events.EventBus.publish(...)` (directly, or via
the 26 `emit_*()` helpers) writes a message onto a single in-process
fan-out channel that is multiplexed over SSE to the entire frontend
client population. The payload carries a `_broadcast_scope` tag and,
optionally, a `_session_id` / `_user_id` / `tenant_id` for downstream
routing.

Two structural problems were documented by the Q.3 audit
(`docs/design/multi-device-state-sync.md` §1.2, §5.1) and motivate
this policy:

1. **Scope is advisory, not mandatory.** `EventBus._deliver_local`
   (currently) only filters on `broadcast_scope="tenant"`. Values
   `"user"` and `"session"` are recorded in the payload but every
   subscriber on the bus still receives the frame; the frontend is
   expected to self-filter on `data.user_id` / `data._session_id`.
   Consequence: a careless helper that defaults to `"global"` leaks
   private telemetry to every connected client across tenants.

2. **Most legacy emitters default to `"global"`.** Per the §6.1
   scope audit (2026-04-24), **15 of 26 event types** — the worst
   offender being `pipeline` with 62 production call sites — ship
   today at scope `"global"`. Even after `_deliver_local` is tightened
   to enforce `user` / `session`, a `"global"` default means every
   callsite must be hand-audited to pick the right scope.

The Q.4 fix has three parts: (i) force every `emit_*` helper to
declare scope (checkbox 2, done), (ii) codify how authors pick the
right scope — **this document**, and (iii) add AST lint +
cross-user-leak regression guards (checkbox 4).

## 2. The rubric (four rules, evaluated in order, first match wins)

Every SSE emit lands in exactly one of four buckets. Apply the rules
top-to-bottom and take the first match:

### Rule 1 — Private debug / per-invocation telemetry → `session`

> *"If the originator closes the tab, nobody else needs to see the
> rest of this event."*

**What belongs here:** agent thought-chains, tool output (REPORTER
VORTEX traces), pipeline phases, semantic-entropy verdicts,
scratchpad flushes, auto-continuation adapter events, UI-sandbox
screenshots and compile errors.

**Routing behaviour:** the event is tagged with the originator's
`session_id`; `EventBus._deliver_local` will (once enforcement is
flipped from advisory to mandatory, per §4 below) drop the frame for
subscribers whose SSE connection is not the originating session.

**Boundary with Rule 2:** the distinguishing question is *persistence
of relevance*. Agent reasoning that stops mattering the moment the
originator navigates away is session-scoped; task status that must
keep syncing to the originator's phone after their laptop is closed
is user-scoped. "Same user, different device" resolves **in favour of
Rule 2**; "same device, different tab in the same session" stays in
Rule 1.

**Known inhabitants (per §6.1):** `agent_update`, `tool_progress`,
`pipeline`, `agent.entropy`, `agent.scratchpad.saved`,
`agent.token_continuation`, `ui_sandbox.screenshot`,
`ui_sandbox.error`.

### Rule 2 — User-owned UI state → `user`

> *"The same operator on phone + laptop must converge on this state."*

**What belongs here:** task CRUD, workflow run status, notification
read-state, preferences (locale / theme / wizard), integration
settings, chat history, provider switches, artifact lifecycle,
chatops messages, cross-agent observations, new-device security
alerts, INVOKE lifecycle summaries (start / halt / resume /
task_complete / gerrit_push / ci_triggered / review outcome / mode
change / merger vote).

**Routing behaviour:** the event is tagged with the owning `user_id`
in the payload; subscribers whose SSE connection is not
authenticated as that user will drop the frame (under enforced
`_deliver_local`). Cross-worker fan-out via Redis Pub/Sub
(`shared_state.publish_cross_worker`) honours the tag on the
receiving side.

**Boundary with Rule 3:** user-scoped events belong to a *single
end-user*. If the payload describes *someone else's* activity (e.g.
a tenant-wide lock was acquired, another user's sandbox was
reclaimed) the audience is "admins of the tenant", which is Rule 3.
Rule 2 answers the question "does *this operator* need to see it on
every device they own?".

**Known inhabitants (per §6.1):** `token_warning`, `task_update`,
`workspace`, `container`, `invoke` (all 25 call sites, per
§Path 6 close-out), `simulation`, `debug_finding`,
`workflow_updated`, `notification.read`, `preferences.updated`,
`integration.settings.updated`, `chat.message`,
`security.new_device_login`; plus the `artifact_created` /
`chatops.message` / `notification` / `cross_agent_observation`
direct-`bus.publish` callers from §6.2.

### Rule 3 — Tenant admin dashboards → `tenant`

> *"Visible only on the admin console, bounded by tenant isolation."*

**What belongs here:** orchestration queue / lock / merger telemetry,
sandbox capacity and pre-warm decisions, PEP gateway rulings, host
metrics, budget / storage quota warnings, tunnel provisioning,
decision-profile / mode changes, code-signing cert expiry, budget
strategy changes.

**Routing behaviour:** `broadcast_scope="tenant"` is the **one filter
`EventBus._deliver_local` already enforces today** (events.py:161).
`tenant_id` must be passed (either explicitly or resolved via
`current_tenant_id` context-var); subscribers on a different tenant
never see the frame.

**Boundary with Rule 2:** a tenant event describes collective state.
If any individual operator in the tenant (not only the admin)
legitimately consumes the signal on their *own* devices, route it
as Rule 2 instead. "Admin dashboard panel refreshes" is Rule 3; "end
user's notification badge decrements" is Rule 2.

**Boundary with Rule 4:** tenant events stop at the tenant boundary.
If the audience is genuinely cross-tenant (e.g. the SRE on-call),
consider whether the signal should even be on the SSE bus at all
(see Rule 4).

**Known inhabitants (per §6.1 + §6.2):** `orchestration.queue.tick`,
`orchestration.lock.acquired`, `orchestration.lock.released`,
`orchestration.merger.voted`,
`orchestration.change.awaiting_human_plus_two`,
`budget_strategy_changed`, `cf_tunnel_provision`,
`sandbox_capacity_reclaim`, `sandbox_capacity_grace_enforced`,
`pep.decision`, `tenant_storage_warning`, `sandbox.prewarm_paused`,
`host.metrics.tick`, `cert_expiry`, `profile_changed`,
`sandbox.deferred`, `mode_changed`.

### Rule 4 — System-wide operational health → `global`

> *"This signal needs to cross tenant boundaries to a multi-tenant
> SRE surface."*

**Expected occupancy: empty.**

Today the OmniSight SSE bus has no legitimate cross-tenant surface.
Multi-tenant SRE signals (Redis down, PG failover, global config
reload, deploy status) flow through `logger.critical` + Prometheus
alerting + pager, **not** SSE. A `broadcast_scope="global"` emit is
therefore an anti-pattern: it means an event leaks across tenants on
a channel that is reachable by every authenticated frontend client.

**If you think you need Rule 4, stop and ask:**
- Is there really no tenant that owns this event? (If the emitter
  has access to a tenant_id, use Rule 3.)
- Is the consumer an end-user browser, or is it an operator tool?
  (If operator tool, it belongs on a separate ops channel, not the
  public SSE bus.)
- Could the signal be served by a targeted log + metric + alert
  rather than a pushed SSE event? (Almost always yes.)

The sweep (§4) is expected to zero this bucket out. Any new
`broadcast_scope="global"` added after Q.4 lands must include a
design-doc reference justifying the departure from zero.

## 3. Decision flowchart

```
    start: author wants to emit an SSE event
           │
           ▼
    ┌──────────────────────────────────────────────────┐
    │ Q1: If the originating device closes now, does   │
    │     anyone else still care about this event?     │
    └──────────────────────────────────────────────────┘
        │ no                                 │ yes
        ▼                                    ▼
    scope="session"       ┌──────────────────────────────────────┐
    (Rule 1)              │ Q2: Is the audience this *same* end  │
                          │     user on their other devices?     │
                          └──────────────────────────────────────┘
                              │ yes                       │ no
                              ▼                           ▼
                          scope="user"      ┌──────────────────────────────────┐
                          (Rule 2)          │ Q3: Is the audience bounded by a │
                                            │     tenant (admins / all users   │
                                            │     within a single tenant)?     │
                                            └──────────────────────────────────┘
                                                │ yes                │ no
                                                ▼                    ▼
                                            scope="tenant"     scope="global"
                                            (Rule 3)           (Rule 4 — STOP,
                                                               see §2 rule 4)
```

## 4. How the policy is enforced

Three layers, each closes a gap the next layer depends on:

### 4.1 Helper signature (done — checkbox 2)

All 26 `emit_*` helpers (`backend/events.py` ×19,
`backend/orchestration_observability.py` ×5,
`backend/ui_sandbox_sse.py` ×2) take `broadcast_scope: str | None = None`.
The shared resolver `backend.events._resolve_scope(helper_name, scope,
legacy_default)`:

- returns the caller-supplied value if non-`None`;
- otherwise emits **one** `logger.warning` per `(helper, legacy_default)`
  tuple and falls back to the historical per-helper default;
- **raises `TypeError` at call time** if `OMNISIGHT_SSE_SCOPE_STRICT=1`
  is set, letting CI and ops preview the post-grace-period behaviour.

The warn-once dedupe (`events._SCOPE_WARNED` + lock) is **intentionally
per-worker** (SOP Step 1 module-global audit answer #3): the reminder
is diagnostic, not correctness-critical, and a missing-scope helper
in one worker does not suppress the warning in another.

### 4.2 Scope-filter enforcement (pending — Q.4 remaining work)

`EventBus._deliver_local` (`events.py`) currently enforces only the
`"tenant"` filter; `"user"` and `"session"` are payload-only. The
Q.4 sweep must extend the filter to drop frames whose
`_broadcast_scope="user"` and `data._user_id` does not match the
subscriber, and analogously for `"session"`. Until that lands the
policy is declarative — it still reduces leaks once the `test_user_
scope_does_not_leak_across_users` assertion (§4.3) forces the
behaviour.

### 4.3 Regression guards (pending — checkbox 4)

Two tests, one belt and one braces:

- **`test_event_scope_declared`** — AST-walks every `.py` under
  `backend/` and fails if any `emit_*(...)` callsite has no
  `broadcast_scope=` kwarg. Belt: prevents silent regression after
  the sweep lands.
- **`test_user_scope_does_not_leak_across_users`** — spins up two
  SSE subscribers with different `user_id`s (same tenant), publishes
  `broadcast_scope="user"` with `_user_id="u1"`, asserts subscriber
  `u1` receives and subscriber `u2` does not. Braces: this is the
  actual security boundary; the lint is a proxy.

Both tests live in `backend/tests/` and run in the default `pytest`
invocation — no opt-in gate.

### 4.4 Grace period and strict-mode rollout

| Phase | Behaviour when `broadcast_scope=` is omitted |
|-------|---------------------------------------------|
| **Today (grace)** | Warn once per helper, fall back to the legacy per-helper default (`global` / `user` / `session` / `tenant`, per §6.1). |
| **`OMNISIGHT_SSE_SCOPE_STRICT=1` (ops/CI preview)** | `raise TypeError` immediately — same as the next-release default. |
| **Next release (post-sweep)** | Flip the resolver default to raise. All callsites must pass `broadcast_scope=` explicitly. |

The grace window lets the 141 to-be-swept callsites be migrated in
batches without breaking builds. CI can opt into strict-mode early to
track migration progress.

## 5. Authoring checklist

Before merging a new `emit_*` callsite (or a new `bus.publish(...)`
direct caller):

- [ ] Pick the scope per the rubric in §2. When in doubt, prefer the
      narrower scope — "user" is narrower than "tenant" which is
      narrower than "global".
- [ ] Pass `broadcast_scope=` explicitly, even if the helper's
      legacy default already matches your choice. Explicitness is
      the contract — relying on defaults regresses the lint gate.
- [ ] If scope is `"user"`, confirm the payload carries `user_id`
      (either directly via `data["user_id"]` or via the helper's
      internal wiring). Without `user_id` the "user" tag is
      meaningless under enforced `_deliver_local`.
- [ ] If scope is `"tenant"`, confirm `tenant_id` is passed (or
      resolvable via `current_tenant_id` context-var).
- [ ] If scope is `"session"`, confirm `session_id` is threaded
      through from the request context.
- [ ] If scope is `"global"`, add a comment next to the call
      justifying why no narrower bucket applies, and flag the row
      for reviewer scrutiny. This bucket should remain empty after
      the Q.4 sweep.
- [ ] If you're adding a new `emit_*` helper, set its
      `legacy_default` in `_resolve_scope(...)` to the rubric
      answer — not `"global"`.

## 6. References

- `docs/design/multi-device-state-sync.md` §6.1 — per-event target
  scope audit (26 events, 166 callsites); the normative input to the
  Q.4 sweep.
- `docs/design/multi-device-state-sync.md` §6.2 — direct
  `bus.publish(...)` callers (16 sites, secondary sweep scope).
- `docs/design/multi-device-state-sync.md` §6.3 — Q.4 sweep
  acceptance conditions; this policy discharges item 5.
- `docs/design/multi-device-state-sync.md` §6.4 — pre-draft of the
  four rules; this policy is the formalisation.
- `docs/design/multi-device-state-sync.md` §Path 6 close-out —
  full 24-site `invoke`-channel scope decisions carried into Q.4.
- `backend/events.py::_resolve_scope` — the runtime enforcement
  point for §4.1.
- `backend/tests/test_emit_scope_enforcement.py` — 72-test
  signature / warn-once / strict-mode contract lock (checkbox 2
  evidence).
- `TODO.md` Q.4 #298 — parent milestone.
