# postgres_stores

**Purpose**: Provides Postgres-backed implementations of the four Store Protocols used by the agents subsystem (batch persistence, cost tracking, external tool registry, dead-letter queue), each wired to a specific Alembic migration.

**Key types / public surface**:
- `PostgresBatchPersistence` — implements `BatchPersistence` against `batch_runs` / `batch_results` (alembic 0181).
- `PostgresCostStore` — implements `CostStore` for estimates, actuals, budgets, and alerts (alembic 0183).
- `PostgresExternalToolRegistryStore` — implements `ExternalToolRegistryStore` for operator-configured tool bindings (alembic 0184).
- `PostgresDeadLetterQueue` — implements `DeadLetterQueue` for failed-request entries (alembic 0185).
- `ConnFactory` — type alias for the `async with`-able connection factory all four stores accept.

**Key invariants**:
- The contract with callers is "SQL string + arg shape" — only `execute`/`fetch`/`fetchrow`/`fetchval` are used, so tests can mock without a real Postgres or even asyncpg installed.
- Tenant scoping is deliberately absent at this layer; it's deferred to the KS.1 multi-tenant rollout, which will patch `tenant_id = $N` into every query.
- `ExternalToolDefinition` metadata lives in code (`DEFAULT_TOOL_DEFINITIONS`); only mutable binding state is persisted. If a row's `tool_name` isn't in the static registry, `_row_to_binding` synthesizes an ad-hoc definition (drift fallback).
- `_json_or_none` defensively handles already-serialized JSON strings; `_parse_jsonb` tolerates asyncpg returning either decoded objects or raw strings.
- `PostgresDeadLetterQueue.remove` parses asyncpg's `"DELETE N"` command tag string to derive a boolean — fragile if the driver ever changes that format.

**Cross-module touchpoints**:
- Imports protocol/dataclass types from `backend.agents.batch_client`, `cost_guard`, `external_tool_registry`, and `rate_limiter` — this module is the Postgres adapter layer for all of them.
- Production callers are expected to pass `lambda: get_pool().acquire()`; the pool itself is not imported here (kept loose so InMemory dev mode loads without asyncpg).
