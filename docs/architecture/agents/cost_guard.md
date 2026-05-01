# cost_guard

**Purpose**: Pre-submit USD cost estimation and budget enforcement for Anthropic API calls. Predicts spend from token counts and pricing tables, then gates submissions against per-scope caps (workspace/priority/task_type/model/global) with three-tier alerts.

**Key types / public surface**:
- `CostGuard` — main class; `check()` is the pre-submit gate, plus `record_estimate/actual`, `configure_budget`, `alerts_since`.
- `estimate_cost(...)` — module-level helper that builds a `CostEstimate` from tokens + flags.
- `PRICING_TABLE` / `get_pricing(model)` — module-const Anthropic Tier 4 prices; raises on unknown model.
- `CostStore` Protocol + `InMemoryCostStore` — persistence surface; PG-backed impl deferred to AB.4.
- Dataclasses: `CostEstimate`, `CostActual`, `ScopeKey`, `BudgetCap`, `BudgetCheck`, `BudgetAlert`.

**Key invariants**:
- `input_tokens` passed to `cost_usd` is the *non-cached* portion only; cache_read and cache_creation are separate args (matches Anthropic SDK usage split).
- Batch discount (50%) applies only to input/output rates, not cache rates — Anthropic's batch prices are already net of cache discount.
- Unknown models raise `KeyError` rather than defaulting — fail loud over silent mis-billing.
- Per-batch cap checks are skipped unless caller passes `per_batch_observed_usd` explicitly; the in-memory store's `since` filter is a no-op (no created_at on dataclass — production PG impl is expected to honour timestamps).
- `check()` always evaluates a global scope plus whichever of workspace/priority/task_type/model are populated on the estimate.

**Cross-module touchpoints**:
- Per docstring, integrates with `backend.agents.llm._normalize_ratelimit_headers` indirectly: AB.6 alerts intended to write to `SharedKV("cost_alerts")` paralleling the existing `SharedKV("provider_ratelimit")` rate-watch — but no such write is visible in this file (alert_sink is the injection point).
- Designed to be called from AB.4 dispatcher pre-submit; persistence via alembic 0183.
- No imports from other backend modules in this file — fully standalone surface.
