# batch_eligibility

**Purpose**: Decides whether each agent task goes to Anthropic's real-time lane or the cheaper batch lane (50% off, 24h SLA), and accumulates batch-eligible tasks into proper waves before handing them to the dispatcher.

**Key types / public surface**:
- `EligibilityRule` — frozen dataclass holding per-`task_kind` routing metadata (eligibility, priority, realtime veto, auto-batch threshold).
- `DEFAULT_ROUTING` — shipped table covering HD parsers, L4 CI, TODO bulk, chat/sandbox/live (realtime-required), and soft-realtime defaults.
- `EligibilityRegistry` — wraps defaults with operator overrides; exposes `get`, `set_override`, `clear_override`, and `route()`.
- `RoutingDecision` — explicit result of `route()` with lane, priority, rule, and human-readable reason for auditing.
- `AutoBatchAccumulator` — buffers same-kind tasks and flushes via injected `dispatcher_enqueue` on count threshold or age timeout.

**Key invariants**:
- A rule cannot be both `realtime_required=True` and `batch_eligible=True` — `__post_init__` raises. Same check applies to operator overrides.
- `realtime_required` hard-vetoes `force_lane="batch"`; the decision silently rewrites to `realtime/P0`. Soft-realtime defaults (e.g. `planning`, `generic_dev`) *are* overridable.
- Unknown `task_kind` falls back to `generic_dev` with a warning — never raises, so callers always get a decision.
- `AutoBatchAccumulator` is deliberately *not* a worker: `add()` only opportunistically flushes on threshold; age-based flushing requires the caller to poll `flush_due()`. Buckets are keyed by `(task_kind, model, tools_signature)` so different model/tool mixes don't get co-batched.

**Cross-module touchpoints**:
- Imports `BatchableTask`, `LaneType`, `PriorityLevel` from `backend.agents.batch_dispatcher` (AB.4); `dispatcher_enqueue` is expected to wire to `BatchDispatcher.enqueue()`.
- Per the module docstring, AB.4's dispatcher caller invokes `EligibilityRegistry.route()` — but the actual call site isn't visible in this file.
