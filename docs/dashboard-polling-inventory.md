# Dashboard polling inventory (Phase 4-4)

Triage of every dashboard-side periodic HTTP poll, mapped against what
`GET /api/v1/dashboard/summary` (Phase 4-1) and the SSE stream already
cover. Drives Phase 4-5 (rate-limit roll-back) and future SSE-first
panels.

Audited 2026-04-24 against `master` HEAD, post-Phase-4-3.

## 1. Baseline — `useEngine` (top-level)

`hooks/use-engine.ts::fetchSystemData` — **single call** to
`/api/v1/dashboard/summary` every **10 s** (Phase 4-3). The aggregator
demuxes internally into 11 sub-keys (`systemStatus`, `systemInfo`,
`devices`, `spec`, `repos`, `logs`, `tokenUsage`, `tokenBudget`,
`notificationsUnread`, `compression`, `simulations`). Real-time state
(agents / tasks / events / notifications / artifacts / tokens / chat)
rides SSE via `subscribeEvents` and is not polled.

Per-tab top-level cost: **6 req/min** (1 call × 6 ticks/min).

## 2. Panel-local poll triage

| Panel | File | Interval | Endpoints | SSE? | Decision | Next action |
|---|---|---|---|---|---|---|
| ops-summary-panel | `components/omnisight/ops-summary-panel.tsx:44` | 10 s | `GET /api/v1/ops/summary` | — | **ABSORB** into `/dashboard/summary` | Phase 4-5 prereq: add 12th sub-key `opsSummary` to the aggregator; useEngine writes panel state; panel drops its own `setInterval` |
| orchestration-panel | `components/omnisight/orchestration-panel.tsx:61,70` | 10 s + SSE | `GET /orchestration/snapshot` | `orchestration.queue.tick`, `orchestration.change.awaiting_human_plus_two` | **SSE-FIRST** | Keep current 10 s poll as safety net (panel is always visible when dashboard is open; `snapshot` is cheap). Revisit to push `snapshot` into aggregator only if Phase 4-6 soak shows pressure |
| pipeline-timeline | `components/omnisight/pipeline-timeline.tsx:77,78` | 10 s + SSE | `GET /pipeline/timeline` | `pipeline`, `invoke` | **SSE-FIRST** | Same as orchestration — SSE already updates live; poll is convergence safety net. Do not absorb; timeline payload is bigger than the aggregator should carry every 10 s for users who are not looking at it |
| audit-panel | `components/omnisight/audit-panel.tsx:108` | 15 s | `GET /audit/entries`, `GET /sessions` | — | **KEEP INDEPENDENT** | Panel is tab-local (not all operators open it). Absorbing would tax every dashboard tick for a rarely-viewed panel. Revisit if/when operators request real-time tail — would warrant a new `audit.new_entry` SSE event instead of aggregator absorption |
| run-history-panel | `components/omnisight/run-history-panel.tsx:115` (+ `useWorkflows` hook at line 75) | 15 s | `GET /project-runs/{id}`, workflows via hook | `workflow_updated` (via `useWorkflows`) | **SSE-FIRST** | Workflow list already reactive via SSE. The 15 s poll is for per-run detail drilldown — low traffic (only one active run at a time). No change in Phase 4; revisit if `workflow_updated` gets extended to carry per-run step state |
| arch-indicator | `components/omnisight/arch-indicator.tsx:76` | 15 s | `GET /api/v1/runtime/platform-status` | — | **ABSORB** into `/dashboard/summary` | Platform status is slow-changing host state and the indicator is always visible. Phase 4-5 prereq: add 13th sub-key `platformStatus`; panel drops its own `setInterval` |
| host-device-panel | `components/omnisight/host-device-panel.tsx:567` (+ `useHostMetricsTick` hook) | 5 s (SSE-tick-driven refresh) | `/runtime/info` on mount, SSE `host.metrics.tick` drives refresh | `host.metrics.tick` | **NO CHANGE** | Already push-based (SSE `host.metrics.tick` every 5 s is how the backend fans out host metrics to all workers). The 5 s `setInterval` here is a belt-and-braces refresh driven by the SSE handler — not a poll against the backend |
| integration-settings | `components/omnisight/integration-settings.tsx:820` (CircuitBreakerSection only) | 10 s (modal-scoped) + SSE | `getSettings()`, `getProviders()`, `getCircuitBreakers("tenant")` | `integration.settings.updated` | **MODAL-SCOPED, KEEP** | Polling only runs while the settings modal + its Circuit-Breaker sub-section is open — not a dashboard-wide concern. No change needed in Phase 4 |

## 3. Summary

- **Absorb into aggregator** (→ 12- and 13-subkey `/dashboard/summary`):
  `ops-summary-panel`, `arch-indicator`. Both are always-visible,
  cheap, slow-changing state — the exact profile the aggregator
  exists for. Doing both lets the panels drop their own `setInterval`s,
  further shrinking per-tab request count.
- **SSE-first, coarse poll as safety net** (no change in Phase 4):
  `orchestration-panel`, `pipeline-timeline`, `run-history-panel`.
  SSE already carries the interesting deltas; the 10–15 s poll
  converges on lost-event state without adding meaningful load.
- **Keep independent** (no change): `audit-panel` (tab-local, rarely
  visible), `integration-settings` (modal-local).
- **Already push-only**: `host-device-panel`.

## 4. Per-tab request budget

Assuming all panels rendered concurrently in one tab:

| Source | Before Phase 4 | After Phase 4-3 (current) | After Phase 4-5 absorption (projected) |
|---|---:|---:|---:|
| `useEngine` fan-out | 132 req/min (11 × 12) | 6 req/min (1 × 6) | 6 req/min |
| ops-summary-panel | 6 req/min | 6 req/min | **0** (absorbed) |
| orchestration-panel | 6 req/min | 6 req/min | 6 req/min |
| pipeline-timeline | 6 req/min | 6 req/min | 6 req/min |
| audit-panel | 4 req/min | 4 req/min | 4 req/min |
| run-history-panel | 4 req/min | 4 req/min | 4 req/min |
| arch-indicator | 4 req/min | 4 req/min | **0** (absorbed) |
| host-device-panel | 0 (SSE) | 0 (SSE) | 0 (SSE) |
| integration-settings | 0 (modal-closed) | 0 (modal-closed) | 0 (modal-closed) |
| **Total / tab** | **~162 req/min** | **~36 req/min** | **~26 req/min** |

Phase 4-5 (rate-limit roll-back) target: `per_user = 300 / 60 s` on
free plan. 3 tabs × 26 req/min = 78 req/min = ~4× headroom — safe.

## 5. Phase 4-5 pre-requisites (captured, not done here)

These sit in front of the rate-limit roll-back in Phase 4-5. Neither
is in scope for Phase 4-4 (which is this doc):

1. Extend `backend/routers/dashboard.py::get_dashboard_summary` with
   two more sub-queries — `opsSummary` (fan-out to
   `backend/routers/ops.py::get_summary`) and `platformStatus`
   (fan-out to `backend/routers/system.py::get_platform_status`).
   Same `{ok, data|error}` envelope; same fault-tolerant `asyncio.gather`.
2. Frontend: `hooks/use-engine.ts` reads the two new sub-keys and
   exposes them via the existing context; `ops-summary-panel.tsx` and
   `arch-indicator.tsx` consume from context and delete their local
   `setInterval` + `fetch` paths.
3. Add `DashboardSummary` type entries in `lib/api.ts`.
4. Tests: extend `backend/tests/test_dashboard_summary.py` to cover
   the two new sub-keys (happy + partial-failure).

## 6. Explicit non-goals for Phase 4

Per the Phase 4 scope preamble in `TODO.md`:

- Do **not** rewrite the 10+ other polling panels outside the 8 in
  this inventory.
- Do **not** touch the SSE infrastructure (event types, pub/sub
  topology, or the `subscribeEvents` helper).
- Do **not** flip `backend/quota.py::PLAN_QUOTAS["free"]` until Phase
  4-5 — it stays at the elevated `per_user = 1200 / 60 s` set by
  SP-8.1c as a temporary cap-lift.
