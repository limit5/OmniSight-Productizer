# Multi-device state sync — mutation path audit

**Task:** Q.3 #297 — checkbox 1 of 3 (audit only; sub-task creation + chat history migration covered in follow-up checkboxes).
**Audit date:** 2026-04-24
**Auditor:** software-beta

## 0. Scope & Method

For each of the 8 mutation paths listed in `TODO.md` Q.3, we verify three layers:

| Layer | Question | Why it matters |
|-------|----------|----------------|
| **(a) Persistence** | Is the mutation written to a *shared* store (PG row / SharedKV / Redis)? | uvicorn runs `--workers N`; module-global Python state is **per-process**, so two devices that hit different workers will diverge. |
| **(b) SSE broadcast** | Is an event emitted after the write, and at what scope? | Without a push event, device B sees device A's change only on the next poll / page reload. |
| **(c) Frontend listener** | Does the frontend subscribe to that event and re-render? | An emitted event with no listener is silently dropped. |

A path is **OK** only when all three layers are present and scope-correct.

## 1. Event bus primitives (reference)

Central helpers live in `backend/events.py`. The publish API is:

```python
bus.publish(event, data, *, session_id=None, broadcast_scope="global",
            tenant_id=None)  # backend/events.py:107
```

Fifteen convenience emitters wrap `bus.publish`:

| emitter | default scope | file:line |
|---|---|---|
| `emit_agent_update` | `"global"` | `events.py:209` |
| `emit_task_update` | `"global"` | `events.py:235` |
| `emit_tool_progress` | `"global"` | `events.py:249` |
| `emit_pipeline_phase` | `"global"` | `events.py:270` |
| `emit_workspace` | `"global"` | `events.py:284` |
| `emit_container` | `"global"` | `events.py:299` |
| `emit_invoke` | `"global"` | `events.py:314` |
| `emit_token_warning` | `"user"` | `events.py:329` |
| `emit_simulation` | `"global"` | `events.py:349` |
| `emit_agent_entropy` | `"global"` | `events.py:366` |
| `emit_agent_scratchpad_saved` | `"global"` | `events.py:409` |
| `emit_agent_token_continuation` | `"global"` | `events.py:446` |
| `emit_debug_finding` | `"global"` | `events.py:481` |
| `emit_new_device_login` | `"user"` | `events.py:559` |

### ⚠ Scope-filter contract gap (important)

`EventBus._deliver_local` (`events.py:81-105`) **only filters on `"tenant"` scope**. `"user"` and `"session"` values are stored in the payload (`_session_id`, `_broadcast_scope`) but **every subscriber still receives the message**. The frontend is expected to self-filter on `data.user_id` / `data._session_id`. This is an explicit design decision (see comment at `events.py:568-577`), but it means:

- A `broadcast_scope="user"` emit is *declarative*, not enforced — server side, all clients see it.
- If a frontend consumer forgets to self-filter, events leak across users/sessions.
- Q.4 (#298 — SSE event scope policy) is the proper place to tighten this. The current audit only *documents* the gap.

Cross-worker fan-out is handled by `shared_state.publish_cross_worker()` → Redis Pub/Sub (I10 primitive, falls back to in-process delivery when Redis absent).

The frontend attaches a single shared `EventSource` (`frontend/lib/api.ts:334+`, `_ensureSharedEventSource`) with ~12 multiplexed subscribers. Primary dispatcher: `frontend/hooks/use-engine.ts:184`.

## 2. Path-by-path audit

### Path 1 — LLM provider keys

| Layer | Evidence | Status |
|-------|----------|--------|
| (a) Persistence | `backend/routers/integration.py:68` `_runtime_settings_kv = SharedKV("runtime_settings")`; write at `:432`. `backend/routers/providers.py:54-58` mutates runtime `settings.*` in-process; persistence is via the `/integration` write path that mirrors into SharedKV. | ✅ |
| (b) SSE emit | `emit_invoke("provider_switch", …)` at `providers.py:66` and `integration.py:451`. Scope = default `"global"`. | ✅ |
| (c) Frontend listener | `frontend/hooks/use-engine.ts:218-223` — on `event.event === "invoke" && d.action_type === "provider_switch"` triggers `providerSwitchCallbackRef.current()` → refetch providers. | ✅ |
| **Verdict** | **OK** (already confirmed commit `8d626489`). | |

### Path 2 — Integration settings (Gerrit / JIRA / GitHub / …)

| Layer | Evidence | Status |
|-------|----------|--------|
| (a) Persistence | `backend/routers/integration.py:430-438` mirrors into `_runtime_settings_kv` SharedKV hash (fields enumerated at `:94+`, `_SHARED_KV_STR_FIELDS` + `_SHARED_KV_TYPED_FIELDS`). | ✅ |
| (b) SSE emit | **Missing for non-LLM fields.** Only the LLM subset (`llm_provider` / `llm_model` / keys) triggers `emit_invoke("provider_switch", …)` at `:451`. A Gerrit-token-only edit or JIRA-URL change emits nothing. | ⚠ PARTIAL |
| (c) Frontend listener | `frontend/components/omnisight/integration-settings.tsx:2680-2687` refetches on modal `open`; `:2822-2823` refetches after local save. **No SSE subscription** for integration settings. | ⚠ |
| **Verdict** | **PARTIAL** — persistence is solid cross-worker, but device B's open settings modal will show stale JIRA/Gerrit/GitHub values until user closes and reopens. Accepted trade-off (rare concurrent admin edits) but should be documented. | |

### Path 3 — `workflow_runs` state

| Layer | Evidence | Status |
|-------|----------|--------|
| (a) Persistence | `backend/workflow.py:143,303` `UPDATE workflow_runs` with `version` optimistic lock; `backend/routers/workflow.py:28-34` enforces `If-Match`. I2 RLS via `tenant_where_pg` + session GUC. | ✅ |
| (b) SSE emit | **MISSING.** `grep -n "workflow_updated\|bus\.publish\|emit_" backend/workflow.py` → 0 matches. `backend/routers/workflow.py` has no emit either. Repo-wide grep for `workflow_updated` returns only `TODO.md`. **The TODO.md claim "✅ SSE `workflow_updated`" is false.** | ❌ GAP |
| (c) Frontend listener | No handler exists (no frontend file references `workflow_updated`). | ❌ |
| **Verdict** | **GAP** — optimistic lock is correct (concurrent writes fail loudly with 409), but there is zero push notification. Device A submits a workflow transition → device B's workflow list is stale until refresh. **Correct TODO status: ⚠ needs SSE wiring, not ✅.** | |

### Path 4 — Task CRUD

| Layer | Evidence | Status |
|-------|----------|--------|
| (a) Persistence | `backend/routers/tasks.py::_persist()` `:68-90` — `_tasks[]` in-memory mirror + `db.upsert_task(conn, …)`. DB row is canonical. | ✅ |
| (b) SSE emit | Only `PATCH /tasks/{id}` emits `emit_task_update(...)` at `tasks.py:190`. `POST /tasks` (`:107-127`) does **not** emit. `DELETE /tasks/{id}` (`:275-283`) does **not** emit. | ⚠ PARTIAL |
| (c) Frontend listener | `frontend/hooks/use-engine.ts:201-203` handles `task_update` events. | ✅ for the one event that fires. |
| **Verdict** | **PARTIAL** — create + delete are invisible to other devices. Fix: add `emit_task_update(task.id, action="created", …)` after the insert at `tasks.py:127`, and `emit_task_update(task.id, action="deleted")` before the DELETE returns at `tasks.py:283`. Frontend hook already dispatches on event type and can switch on `action`. | |

### Path 5 — Chat history (**biggest gap**)

| Layer | Evidence | Status |
|-------|----------|--------|
| (a) Persistence | `backend/routers/chat.py:30` — `_history: list[OrchestratorMessage] = []` (module-global **list**, not `deque` as TODO said — same problem, slightly different shape: unbounded growth). Appended at `:122, 126, 129, 146, 151-152`. **Not in PG, not in SharedKV, not in Redis.** | ❌ GAP |
| (b) SSE emit | None for history mutation. `emit_pipeline_phase` at `:57, 61, 72` covers pipeline lifecycle (start / complete / error), not the message row. | ❌ |
| (c) Frontend listener | `frontend/lib/api.ts:959-961` defines `getChatHistory()` but grep shows **no `.tsx` consumer** — the endpoint is orphaned. Current chat UI receives the single `POST /chat` response; no cross-device replay. | ❌ |
| **Verdict** | **GAP — triple failure.** Module-global list diverges per worker (`uvicorn --workers N` means each worker has its own `_history`), has no broadcast, and no frontend consumer even exists. This is the biggest remediation item and is already carved out into TODO checkbox #3 (`chat_messages` table migration + SSE `chat.message` scope=user). | |

### Path 6 — INVOKE command results

| Layer | Evidence | Status |
|-------|----------|--------|
| (a) Persistence | Side-effects flow through `_persist_agent` / `_persist_task` — no invoke-specific log table for the event itself. | ~ (N/A — invoke is ephemeral by design) |
| (b) SSE emit | ~10 `emit_invoke(...)` call sites in `backend/routers/invoke.py` (`:316, 322, 337, 379, 585, 612, 897, 1221, 1237`). **None pass `session_id` or `broadcast_scope`** → every invoke event broadcasts at scope `"global"` (the `emit_invoke` default in `events.py:316`). | ⚠ PARTIAL (scope wrong, not missing) |
| (c) Frontend listener | `frontend/hooks/use-engine.ts:218+` logs and routes `provider_switch` on the `invoke` channel. | ✅ for `provider_switch`; other `action_type`s are logged only. |
| **Verdict** | **PARTIAL — scope-mismatch, not gap.** The TODO line assumed "SSE stream tied to originator session, other devices can't see it." In reality events are emitted at scope `"global"`, so *every* connected client sees them — the opposite of the stated concern. The correct design (per TODO's own rationale "only push to originator for in-flight streaming, but completed invocation summaries belong to user-scope history") is to split the `action_type`s: in-flight streaming → `scope="session"`, completion summaries (e.g. `task_complete`, `halt`, `resume`) → `scope="user"`. This sequencing belongs in Q.4 (#298 event-scope policy) and the checkbox-2 sub-task list. | |

#### Q.3-SUB-7 close-out (2026-04-24) — defer-to-Q.4 decision is **ratified, not implemented**

Per the TODO directive (`Q.3-SUB-7 (P2 — INVOKE scope split, defer to Q.4 #298)` — "**不獨立 land**"), this audit row is closed by **transferring the implementation spec to Q.4 (#298)**, not by editing `emit_invoke` here. Doing the split in isolation would be churned: Q.4 must declare `scope` on every emitter via a single repo-wide policy file (`docs/design/sse-event-scope-policy.md`) and a `test_event_scope_declared` lint-style test (TODO.md:802). A pre-emptive partial fix on `invoke` channel only would (a) be re-touched once Q.4 lands, (b) not ship the actual security boundary because `EventBus._deliver_local` (`events.py:81-105`) still doesn't enforce `user`/`session` scope (see §1.2 above) — Q.4 lifts both at once.

**Implementation spec carried to Q.4 #298 (full call-site inventory, all 24 sites — verified 2026-04-24)**:

| File:line | `action_type` | Trigger context | Target scope (Q.4) | Why |
|---|---|---|---|---|
| `backend/routers/invoke.py:316` | `stuck_switch_model` | Stuck-agent recovery: model downgraded | `user` | Operator should see across all their devices |
| `backend/routers/invoke.py:322` | `stuck_spawn_alt` | Stuck-agent recovery: no source task | `user` | Operator alert |
| `backend/routers/invoke.py:337` | `stuck_spawn_alt` | Stuck-agent recovery: alt task spawned | `user` | Operator alert |
| `backend/routers/invoke.py:379` | `stuck_hibernate` | Stuck-agent recovery: container paused | `user` | Operator alert |
| `backend/routers/invoke.py:585` | `gerrit_push` | Agent pushed change for review | `user` | Cross-device dashboard refresh |
| `backend/routers/invoke.py:612` | `task_complete` | Agent finished task | `user` | Cross-device dashboard refresh |
| `backend/routers/invoke.py:897` | `start` | INVOKE batch begin | `user` | Operator-initiated lifecycle |
| `backend/routers/invoke.py:1221` | `halt` | INVOKE batch halt | `user` | Operator-initiated lifecycle |
| `backend/routers/invoke.py:1237` | `resume` | INVOKE batch resume | `user` | Operator-initiated lifecycle |
| `backend/routers/webhooks.py:282` | `review_rejected` | Gerrit -1 received → fix task | `user` | Owner of the change |
| `backend/routers/webhooks.py:302` | `merged` | Gerrit change merged | `user` | Owner of the change |
| `backend/routers/webhooks.py:340` | `replicated` | Git push to mirror | `user` | Owner of the repo |
| `backend/routers/webhooks.py:512` | `ci_triggered` | GitHub Actions kicked | `user` | Owner of the project |
| `backend/routers/webhooks.py:539` | `ci_triggered` | Jenkins build kicked | `user` | Owner of the project |
| `backend/routers/webhooks.py:576` | `ci_triggered` | GitLab CI kicked | `user` | Owner of the project |
| `backend/routers/integration.py:452` | `provider_switch` | LLM provider/model swap (Q.3-SUB-5 already wired user-scope on the **non-LLM sibling channel** `integration.settings.updated`; this LLM-only one inherits same target) | `user` | Cross-device "active model" indicator |
| `backend/routers/providers.py:66` | `provider_switch` | Direct `/providers/switch` API | `user` | Same as above |
| `backend/pipeline.py:171` | `pipeline` | E2E pipeline start | `user` | Cross-device pipeline dashboard |
| `backend/pipeline.py:231` | `pipeline` | E2E pipeline complete | `user` | Cross-device pipeline dashboard |
| `backend/intent_bridge.py:371` | `intent_bridge:{kind}` | Intent translation event | `user` | Operator visibility |
| `backend/intent_bridge.py:382` | `intent_bridge:error` | Intent translation failure | `user` | Operator alert |
| `backend/orchestrator_gateway.py:982` | `orchestrator_intake:{event}` | Orchestrator session lifecycle | `user` | Operator visibility |
| `backend/orchestration_mode.py:165` | `{mode}:{event}` | Mode change (auto/manual) | `user` | Operator-initiated, cross-device |
| `backend/merger_agent.py:1134` | `merger.{outcome}` | Merger agent +2 outcome | `user` | Owner of the change |
| `backend/merge_arbiter.py:277` | `orchestration.{kind}` | Merge arbiter ruling | `user` | Owner of the change |

**Audit-vs-reality correction** (carried to Q.4 #298 as a "watch-out"): the Q.3 TODO line and the Path 6 Verdict above hypothesised `scope="session"` for "in-flight streaming (`stream_chunk` / `agent_thinking`)" call sites. **Empirically those `action_type`s do not exist on the `invoke` channel today** — chat-stream tokens flow through `EventSourceResponse` (HTTP body, originator-bound by transport, never `bus.publish`). The whole `invoke` channel is operator-facing summary/lifecycle telemetry; the right Q.4 policy for it is uniform `scope="user"` (24 of 24 sites). If a future feature adds true streaming-via-bus on this channel, **only then** introduce `scope="session"` — the policy file should make that the explicit fork rule.

**Q.4 #298 acceptance for the INVOKE slice**:
1. All 24 sites above pass `broadcast_scope="user"` after sweep, `payload._broadcast_scope == "user"` (matches the Q.3-SUB-1 / -3 / -4 / -5 / -6 family pattern so `EventBus` doesn't need a payload schema migration when the filter switches from advisory → enforced).
2. `test_event_scope_declared` (TODO.md:802) catches any regression where `emit_invoke` is called without `broadcast_scope=`.
3. Frontend `use-engine.ts:218+` invoke-channel dispatcher self-filters on `data.user_id === currentUser.id` (already harmless under current `"global"`; under `"user"` becomes redundant once `EventBus._deliver_local` enforces — leave the self-filter in as defense-in-depth).
4. No code change on `events.py::emit_invoke` itself — the helper already accepts `broadcast_scope` (`events.py:316`); Q.4 only changes call sites + the helper's default (or removes the default and forces explicit declaration).

**Q.3 status**: this row is `[x]` because the audit + decision + spec-transfer is the deliverable. The line `emit_invoke("...", broadcast_scope="user")` rewrites land in Q.4 #298, not here.

### Path 7 — User preferences (locale / theme / wizard)

| Layer | Evidence | Status |
|-------|----------|--------|
| (a) Persistence | `backend/routers/preferences.py:62-80` — `INSERT … ON CONFLICT (user_id, pref_key) DO UPDATE` into `user_preferences` (tenant-scoped via `tenant_insert_value()` + `tenant_where_pg`). PG row is canonical. | ✅ |
| (b) SSE emit | **MISSING.** `backend/routers/preferences.py` has no `emit_*` / `bus.publish` call. | ❌ GAP |
| (c) Frontend listener | `frontend/components/storage-bridge.tsx:37-46` uses `window.addEventListener("storage", …)` — this syncs across **tabs in the same browser**, not across devices. `e2e/j4-storage-sync.spec.ts` confirms the J4 coverage is cross-tab only. | ❌ (cross-tab only, not cross-device) |
| **Verdict** | **GAP — masqueraded as ✅ via cross-tab J4.** Device A's locale/theme change persists to PG; device B picks it up only on the next full page reload or explicit prefs GET. Common case (re-login next morning) happens to work; rare case (two devices open simultaneously, flip theme on one) does not sync. **Correct TODO status: ⚠ cross-tab-only, not cross-device.** Remediation: small — add `emit_preferences_updated(pref_key, value, scope="user")` in the PUT handler + `use-engine.ts` subscriber that patches `useUserPrefs` context. | |

### Path 8 — Notifications read-state

| Layer | Evidence | Status |
|-------|----------|--------|
| (a) Persistence | `backend/routers/system.py:1442-1450` — `POST /notifications/{id}/read` → `db.mark_notification_read(conn, id)`. PG row `notifications.read_at`. | ✅ |
| (b) SSE emit | **MISSING for read-transition.** Only notification **creation** emits (`backend/notifications.py:77-86` → `bus.publish("notification", …)`). The mark-read handler emits nothing. | ❌ GAP |
| (c) Frontend listener | `frontend/hooks/use-engine.ts:233-246` subscribes to `notification` (create) events and increments `unreadCount`. The notification center (`components/omnisight/notification-center.tsx`) has an `onMarkRead` prop but no SSE listener to *decrement* on remote reads. | ❌ |
| **Verdict** | **GAP — confirms TODO's ⚠.** Device A clicks a notification → PG `read_at` updated → device B's bell stays at `unreadCount = N` until next `/notifications/unread-count` poll (currently only refetched on focus / periodic). Remediation: emit `bus.publish("notification.read", {id, user_id}, broadcast_scope="user")` in the POST handler + extend `use-engine.ts` to decrement on receipt. Low-risk, low-LOC. | |

## 3. Summary

| # | Path | Persistence | SSE | Frontend | Status |
|---|------|:-:|:-:|:-:|---|
| 1 | LLM provider keys | ✅ SharedKV | ✅ `provider_switch` global | ✅ `use-engine.ts:218` | **OK** |
| 2 | Integration settings | ✅ SharedKV | ⚠ LLM subset only | ⚠ modal-open refetch | **PARTIAL** |
| 3 | Workflow_run state | ✅ PG + optimistic lock | ❌ no emit | ❌ no listener | **GAP** |
| 4 | Task CRUD | ✅ PG | ⚠ update only (no create/delete) | ✅ `use-engine.ts:201` | **PARTIAL** |
| 5 | Chat history | ❌ module-global list | ❌ no emit | ❌ no listener | **GAP (biggest)** |
| 6 | INVOKE results | ~ ephemeral | ⚠ global (should split session vs user) | ✅ `use-engine.ts:218` | **PARTIAL** |
| 7 | User preferences | ✅ PG | ❌ no emit | ❌ cross-tab only | **GAP** |
| 8 | Notifications read-state | ✅ PG | ❌ no emit on read | ❌ no listener | **GAP** |

**Tally:** 1 OK · 3 PARTIAL · 4 GAP.

The TODO's original status column had **3 ✅ · 5 ⚠**. Our audit reclassifies **#3 (workflow_run) from ✅ → GAP** and **#7 (user preferences) from ✅ → GAP** — both were over-credited. No status is downgraded from more-severe to less-severe.

## 4. Sub-task breakdown (for checkbox 2 of Q.3)

Remediation opens as follow-up child tasks. Ordered by LOC / risk:

| Priority | Fix | Effort | Notes |
|:-:|---|:-:|---|
| P0 | **Chat history → DB** (checkbox 3, already carved out) | ≈ 1 day | new `chat_messages(id, user_id, session_id, role, content, ts)` table + Alembic migration + `migrate_sqlite_to_pg.py::TABLES_IN_ORDER` update + `/chat` writes DB + emits `chat.message` scope=user + frontend listener appends to local UI. Streaming tokens stay session-scoped. |
| P0 | **Workflow_run SSE push** | ≈ 2 h | `backend/workflow.py` `UPDATE` success → `emit_workflow_updated(run_id, status, version, scope="user")` (add helper to `events.py`). Frontend `use-engine.ts` dispatcher + `use-workflows` hook patch entry. |
| P1 | **Task CRUD — emit on create + delete** | ≈ 1 h | Two lines in `backend/routers/tasks.py`: `emit_task_update(task.id, action="created", …)` after insert `:127`; `emit_task_update(task.id, action="deleted")` before DELETE return `:283`. Frontend switch on `action`. |
| P1 | **Notifications read-state broadcast** | ≈ 1 h | `bus.publish("notification.read", {id, user_id}, broadcast_scope="user")` in `system.py:1442-1450` + `use-engine.ts` handler decrements `unreadCount`. |
| P1 | **User preferences SSE push** | ≈ 1 h | `emit_preferences_updated(pref_key, value, scope="user")` in `preferences.py` PUT + cross-device subscriber in `storage-bridge.tsx` / `use-engine.ts`. |
| P2 | **INVOKE scope split** (Q.3-SUB-7) | defer to Q.4 | Don't attempt in isolation — Q.4 (#298) will sweep all `emit_*` call sites and declare scope per `action_type`. This audit documents the mis-scoping; fix lands with the policy file. **Closed-out 2026-04-24** with full 24-site routing-rule spec under §Path 6 → "Q.3-SUB-7 close-out" (target = uniform `scope="user"` across the whole `invoke` channel — no streaming sites exist today). |
| P2 | **Integration-settings SSE** | ≈ 1 h (after Q.4) | Add `emit_integration_settings_updated(fields_changed, scope="user")` at `integration.py:432+`; frontend refetches `/settings` on receipt. Depends on Q.4 for scope declaration pattern. |

**Total remediation estimate:** ~1 day + ~5 hours for P0+P1 (≈ 1.5 days aggregate, matching the TODO estimate).

## 5. Open structural questions (not in this audit's scope)

Captured here so they are not lost. These feed into Q.4 (#298) and beyond:

1. **Scope enforcement is cooperative, not mandatory.** `_deliver_local` does not filter on `session_id` / `user_id` even when the payload carries them. A lint rule or middleware could enforce this, but currently a buggy frontend consumer could leak data across users. Track as a separate security follow-up.
2. **`session_id` propagation** — most `emit_invoke` call sites have access to the current request but don't thread `session_id` through. Need a context-var pattern (similar to `current_tenant_id`) so emitters pick it up automatically.
3. **Persistence of SSE events** — only events in `_PERSIST_EVENT_TYPES` are persisted (`events.py:135`). Replay-on-reconnect after flaky mobile network is not currently possible for non-persisted events. Orthogonal to this audit.
4. **Chat history bounded growth** — current `list` is unbounded per worker. Migration to DB resolves memory unboundedness too (add retention policy at same time: e.g. last-30-days-per-user, or last-N-messages).

## 6. Q.4 Pre-sweep scope table (#298, 2026-04-24)

> **Deliverable for Q.4 #298 checkbox 1** (`TODO.md` "Q.4 SSE event scope policy 審視 + 強制宣告" first bullet). This table is the **input** to the Q.4 sweep — the sweep will (a) add `broadcast_scope=` to every listed call site, (b) tighten `EventBus._deliver_local` to enforce `user` / `session` in addition to the `tenant` filter it already honours, and (c) codify the 4-rule rubric into `docs/design/sse-event-scope-policy.md` (checkbox 3). Rows marked **✓ already correct** need no call-site change — they only need the helper default locked once the enforcement flips from advisory to mandatory.
>
> Count sanity-check: the TODO estimated "~30-40 call sites"; the actual inventory (`grep -n "\bemit_\w+\(" backend/ --include='*.py'` minus tests minus `def emit_*` minus `loop.create_task` / doc refs) is **166 call sites** across **24 helpers** emitting **26 distinct SSE event types** (19 via `backend/events.py`, 5 via `backend/orchestration_observability.py`, 2 via `backend/ui_sandbox_sse.py`). The 30-40 estimate tracked event types / helpers, not raw call-site count — aggregated-by-event the table is 26 rows, which fits that range. The `invoke` helper alone accounts for 25 call sites (catalogued in §Path 6 close-out above); a repeat per-call-site table for every helper would balloon past 150 rows without adding decision value, so this table aggregates **per event type** and references Path 6 for the `invoke` per-site breakdown.

### 6.1 Per-event scope decisions (the Q.4 sweep input)

| # | Event type | Emit helper | File:line (def) | Call sites (prod) | Current default | Target scope | Rationale |
|--:|---|---|---|--:|---|---|---|
| 1 | `agent_update` | `emit_agent_update` | `backend/events.py:209` | 16 | `global` | **session** | `thought_chain` is private debug — a 2nd operator logged in to the same tenant should not see another user's agent reasoning stream. Session-scoped mirrors `ui_sandbox.*`. Upgrade to `user` only if a product decision later says agent timelines should follow the operator across devices. |
| 2 | `task_update` | `emit_task_update` | `backend/events.py:235` | 7 | `global` | **user** | Same operator on phone + desktop must see status flips. Task rows are user-owned objects (`tasks.assigned_agent_id`), not session-ephemeral. |
| 3 | `tool_progress` | `emit_tool_progress` | `backend/events.py:249` | 5 | `global` | **session** | Per-agent per-invocation tool trace (REPORTER VORTEX output) — session-scoped like `agent_update`. |
| 4 | `pipeline` | `emit_pipeline_phase` | `backend/events.py:270` | 62 | `global` | **session** | Biggest single fan-out source. Phases are originator-bound transient telemetry; no cross-device replay use case today. **Security gain:** 62 global broadcasts currently leak every pipeline phase to every subscribed client across tenants (the `_deliver_local` default path without enforcement). |
| 5 | `workspace` | `emit_workspace` | `backend/events.py:284` | 3 | `global` | **user** | Workspace lifecycle (create / mount / destroy) belongs to a user — cross-device dashboard sync needed. |
| 6 | `container` | `emit_container` | `backend/events.py:299` | 6 | `global` | **user** | Docker container lifecycle; operator needs cross-device visibility on hibernation / pause decisions. |
| 7 | `invoke` | `emit_invoke` | `backend/events.py:314` | 25 | `global` | **user** | Per Q.3-SUB-7 close-out (§Path 6 above): uniform `user` across all 24 audited sites + 1 double-counted `integration.py` site. No streaming `action_type` exists on this channel today; chat tokens flow via `EventSourceResponse` HTTP body. |
| 8 | `token_warning` | `emit_token_warning` | `backend/events.py:329` | 17 | **`user`** ✓ | **user** | Already correct. Budget is per-user; warning must follow the operator across devices. Sweep only locks the default (no call-site change). |
| 9 | `simulation` | `emit_simulation` | `backend/events.py:349` | 4 | `global` | **user** | Sim start/progress/result is user-initiated; cross-device progress bar. |
| 10 | `agent.entropy` | `emit_agent_entropy` | `backend/events.py:366` | 1 | `global` | **session** | Per-agent-per-round semantic-entropy verdict (R2 #308); debug-level, session-scoped to match `agent_update`. |
| 11 | `agent.scratchpad.saved` | `emit_agent_scratchpad_saved` | `backend/events.py:409` | 1 | `global` | **session** | Scratchpad flush telemetry (R3 #309); session-scoped. |
| 12 | `agent.token_continuation` | `emit_agent_token_continuation` | `backend/events.py:446` | 1 | `global` | **session** | Auto-continue adapter event (R3 #309); session-scoped. |
| 13 | `debug_finding` | `emit_debug_finding` | `backend/events.py:481` | 6 | `global` | **user** | Operator-facing diagnostic; cross-device dashboard (debug findings panel). `cross_agent/observation` routing to DE proposal is already user-private. |
| 14 | `workflow_updated` | `emit_workflow_updated` | `backend/events.py:559` | 2 | **`user`** ✓ | **user** | Already correct (Q.3-SUB-1). Sweep locks default. |
| 15 | `notification.read` | `emit_notification_read` | `backend/events.py:592` | 1 | **`user`** ✓ | **user** | Already correct (Q.3-SUB-3). Sweep locks default. |
| 16 | `preferences.updated` | `emit_preferences_updated` | `backend/events.py:621` | 1 | **`user`** ✓ | **user** | Already correct (Q.3-SUB-4). Sweep locks default. |
| 17 | `integration.settings.updated` | `emit_integration_settings_updated` | `backend/events.py:655` | 1 | **`user`** ✓ | **user** | Already correct (Q.3-SUB-5). Sweep locks default. |
| 18 | `chat.message` | `emit_chat_message` | `backend/events.py:692` | 1 | **`user`** ✓ | **user** | Already correct (Q.3-SUB-6). Sweep locks default. |
| 19 | `security.new_device_login` | `emit_new_device_login` | `backend/events.py:741` | 1 | **`user`** (hardcoded) ✓ | **user** | Already correct (Q.2). Hardcoded — no caller-overridable param. |
| 20 | `orchestration.queue.tick` | `emit_queue_tick` | `backend/orchestration_observability.py:384` | 1 | `global` (via `_publish`) | **tenant** | Tenant-admin operational dashboard — not user-facing, not session-bound. Currently the 5 `orchestration.*` events broadcast to every tenant, which is the same leak pattern as #4. |
| 21 | `orchestration.lock.acquired` | `emit_lock_acquired` | `backend/orchestration_observability.py:398` | 1 | `global` | **tenant** | Same as #20. |
| 22 | `orchestration.lock.released` | `emit_lock_released` | `backend/orchestration_observability.py:415` | 1 | `global` | **tenant** | Same as #20. |
| 23 | `orchestration.merger.voted` | `emit_merger_voted` | `backend/orchestration_observability.py:426` | 1 | `global` | **tenant** | Merger agent +2 telemetry — tenant admin visibility. |
| 24 | `orchestration.change.awaiting_human_plus_two` | `emit_change_awaiting_human` | `backend/orchestration_observability.py:447` | 0 (helper only called via `_refresh_awaiting_gauge` path) | `global` | **tenant** | Same as #20; zero-call-site means Q.4 only sets the helper default. |
| 25 | `ui_sandbox.screenshot` | `emit_ui_sandbox_screenshot_event` | `backend/ui_sandbox_sse.py:899` | 1 | session (session_id passed explicitly) ✓ | **session** | Already correct — helper threads `session_id` from the payload into `pub.publish(..., session_id=...)`. Only needs the `broadcast_scope="session"` addition to make the intent explicit. |
| 26 | `ui_sandbox.error` | `emit_ui_sandbox_error_event` | `backend/ui_sandbox_sse.py:928` | 1 | session (session_id passed explicitly) ✓ | **session** | Same as #25. |

**Summary:** 26 event types → **11 already-correct** (token_warning / workflow_updated / notification.read / preferences.updated / integration.settings.updated / chat.message / security.new_device_login / ui_sandbox.screenshot / ui_sandbox.error — all Q.2/Q.3 family work; plus 2 existing hardcoded-user variants) + **15 to-be-swept** (pipeline×62, invoke×25, agent_update×16, token_warning already OK so not counted, task_update×7, container×6, debug_finding×6, tool_progress×5, simulation×4, workspace×3, 5×orchestration.*, agent.entropy/scratchpad/continuation×1 each). Aggregate call-site-level changes: **141 sites** need `broadcast_scope=` added by the Q.4 sweep (166 total – 25 invoke already catalogued in Path 6 – 0 already-correct helpers since those already pass the arg).

### 6.2 Direct `bus.publish()` callers (outside `emit_*` helpers — Q.4 secondary sweep)

Also landed in the repo are 16 direct `bus.publish("event", …)` call sites that bypass any `emit_*` helper. Q.4's policy enforcement (the `test_event_scope_declared` lint of checkbox 4) will catch these too if the check is `bus.publish` call-site lint rather than `emit_*`-only. Listed here for traceability; **not required by the current TODO row** (which is scoped to `emit_*`). Default target: follow the same rubric (admin / tenant / user / session).

| Event | Call site | Target scope |
|---|---|---|
| `budget_strategy_changed` | `backend/budget_strategy.py:117` | tenant |
| `artifact_created` | `backend/agents/tools.py:1091`, `backend/routers/webhooks.py:438`, `backend/workspace.py:568`, `backend/release.py:201`, `backend/report_generator.py:111` (×5) | user |
| `cf_tunnel_provision` | `backend/routers/cloudflare_tunnel.py:230` | tenant |
| `sandbox_capacity_reclaim` | `backend/sandbox_capacity.py:531` | tenant |
| `sandbox_capacity_grace_enforced` | `backend/sandbox_capacity.py:547` | tenant |
| `pep.decision` | `backend/pep_gateway.py:322` | tenant |
| `tenant_storage_warning` | `backend/tenant_quota.py:357` | tenant |
| `sandbox.prewarm_paused` | `backend/sandbox_prewarm.py:226` | tenant |
| `chatops.message` | `backend/chatops_bridge.py:216` | user |
| `notification` | `backend/notifications.py:77` | user |
| `cross_agent_observation` | `backend/cross_agent_router.py:92` | user |
| `host.metrics.tick` | `backend/host_metrics.py:1021` | tenant |
| `cert_expiry` | `backend/codesign_store.py:963` | tenant |
| `profile_changed` | `backend/decision_profiles.py:164` | tenant |
| `sandbox.deferred` | `backend/decision_engine.py:477` | tenant (already passes `tenant_id=`) |
| `mode_changed` | `backend/decision_engine.py:841`, `backend/decision_engine.py:900` | tenant |

### 6.3 Q.4 sweep acceptance hook

When Q.4 #298 lands, it must:

1. **Every row above has `broadcast_scope=` passed at every call site** (sweep-by-sed + manual review of the 15 to-be-swept event types × 141 sites). The 11 already-correct rows need no call-site change but should have their helper signature default changed from `"global"` → the target scope, or the default removed entirely to force explicit declaration (checkbox 2 of the TODO row).
2. **`EventBus._deliver_local` enforces `user` and `session`** filters (it currently only enforces `tenant`). Otherwise the scope declarations remain advisory and the cross-user leak documented in §5.1 stays unresolved.
3. **`test_event_scope_declared`** (TODO checkbox 4) walks the AST of every `.py` under `backend/` and fails if any `emit_*(...)` call-site has no `broadcast_scope=` kwarg — prevents silent regression after the sweep.
4. **`test_user_scope_does_not_leak_across_users`** (TODO checkbox 4) — spin up two SSE subscribers with different `user_id`s (same tenant), publish with `broadcast_scope="user"` + `_user_id="u1"`, assert subscriber 2 (`u1`) receives, subscriber 3 (`u2`) does not. This is the actual security assertion; the lint in #3 is the belt, this one is the braces.
5. **`docs/design/sse-event-scope-policy.md`** (TODO checkbox 3) — 4-rule rubric derived from this table: *private debug → session / user UI state → user / tenant admin → tenant / system health → global*. The global bucket should be empty after the sweep (nothing legitimately crosses tenant boundaries on the bus).

### 6.4 Scope-choice rubric (draft — feeds checkbox 3)

Four decision rules, in evaluation order (first match wins):

1. **Private debug / per-invocation telemetry → `session`.** Agent reasoning, tool output, pipeline phases, entropy / scratchpad / continuation diagnostics, UI sandbox frames. Criterion: "if the originator closes the tab, nobody needs to see the rest of this event."
2. **User-owned UI state → `user`.** Task CRUD, workflow status, notification read-state, preferences, integration settings, chat history, provider switch, security alerts, artifacts, chatops messages, cross-agent observations. Criterion: "same operator on phone + laptop must converge."
3. **Tenant admin dashboards → `tenant`.** Orchestration queue / locks / merger, sandbox capacity / prewarm, PEP decisions, storage / budget / host metrics, tunnel provisioning, profile / mode changes, cert expiry. Criterion: "only visible on the admin console, bounded by tenant isolation."
4. **System-wide operational health → `global`.** Currently empty after the sweep. Reserved for multi-tenant SRE signals (e.g. Redis down, PG failover, global config reload) — such events today go through `logger.critical` + Prometheus, not SSE, so this bucket should stay empty unless a deliberate operator-console channel is added later.
