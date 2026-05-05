"""Postgres-backed implementations of the AB.* Store Protocols.

Wires:

  * ``PostgresBatchPersistence``        ↔ alembic 0181 (AB.3)
  * ``PostgresCostStore``               ↔ alembic 0183 (AB.6)
  * ``PostgresExternalToolRegistryStore`` ↔ alembic 0184 (AB.5)
  * ``PostgresDeadLetterQueue``          ↔ alembic 0185 (AB.7)

All four take a ``conn_factory`` callable that yields an ``async with``
context manager handing out an ``asyncpg.Connection``. Production
callers pass ``lambda: get_pool().acquire()``; tests pass a mock that
records SQL.

This pattern keeps the impls testable WITHOUT a real PG instance —
SQL string + arg shape is the contract — while still letting them run
against the production pool unchanged. asyncpg's ``Connection.execute``
/ ``fetch`` / ``fetchrow`` / ``fetchval`` are the only methods used,
which the mock can imitate trivially.

Tenant scoping at this layer is limited to app-level metadata until the
production persistence migration adds batch table columns. The in-memory
contract and dispatcher are tenant-aware for R80; SQL filtering remains
the follow-up once the schema row lands.

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §6.2
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from backend.agents.batch_client import (
    BatchResult,
    BatchRun,
    BatchRunStatus,
)
from backend.agents.cost_guard import (
    BudgetAlert,
    BudgetCap,
    CostActual,
    CostEstimate,
    PeriodKind,
    ScopeKey,
    _period_start,
)
from backend.agents.external_tool_registry import (
    DEFAULT_TOOL_DEFINITIONS,
    ExternalToolBinding,
    ExternalToolDefinition,
    HealthStatus,
)
from backend.agents.rate_limiter import (
    DLQEntry,
)


# ─── Connection factory contract ─────────────────────────────────


# A factory yields an `async with`-able that gives an asyncpg.Connection.
# We don't import asyncpg here so this module loads cleanly when asyncpg
# isn't installed (operator running InMemory dev mode).
ConnFactory = Callable[[], Any]
"""Callable returning ``async with`` context manager that yields a
connection-like object. Production: ``lambda: get_pool().acquire()``."""


@asynccontextmanager
async def _acquire(factory: ConnFactory) -> AsyncIterator[Any]:
    """Wrapper that uniformly enters the factory's context manager."""
    cm = factory()
    async with cm as conn:
        yield conn


def _json_or_none(value: Any) -> str | None:
    """JSON-serialize a Python value for JSONB columns; None passthrough."""
    if value is None:
        return None
    if isinstance(value, str):
        # Already JSON? validate by parsing — but caller usually passes
        # dict / list. Defensive: pass strings through if they're already
        # valid JSON, else wrap.
        try:
            json.loads(value)
            return value
        except (TypeError, ValueError):
            return json.dumps(value)
    return json.dumps(value, ensure_ascii=False, default=str)


def _parse_jsonb(raw: Any) -> Any:
    """Inverse of _json_or_none — parse a JSONB row value back."""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw  # asyncpg already decoded JSONB
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


# ─────────────────────────────────────────────────────────────────
# 1. PostgresBatchPersistence
# ─────────────────────────────────────────────────────────────────


_BATCH_RUN_COLS = (
    "batch_run_id, anthropic_batch_id, status, request_count, "
    "total_size_bytes, submitted_at, ended_at, expires_at, "
    "success_count, error_count, canceled_count, expired_count, "
    "metadata, created_by, created_at"
)


class PostgresBatchPersistence:
    """``BatchPersistence`` backed by alembic 0181."""

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._factory = conn_factory

    async def save_batch_run(self, run: BatchRun) -> None:
        async with _acquire(self._factory) as conn:
            await conn.execute(
                f"""
                INSERT INTO batch_runs ({_BATCH_RUN_COLS})
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                ON CONFLICT (batch_run_id) DO UPDATE SET
                    anthropic_batch_id = EXCLUDED.anthropic_batch_id,
                    status = EXCLUDED.status,
                    request_count = EXCLUDED.request_count,
                    total_size_bytes = EXCLUDED.total_size_bytes,
                    submitted_at = EXCLUDED.submitted_at,
                    ended_at = EXCLUDED.ended_at,
                    expires_at = EXCLUDED.expires_at,
                    success_count = EXCLUDED.success_count,
                    error_count = EXCLUDED.error_count,
                    canceled_count = EXCLUDED.canceled_count,
                    expired_count = EXCLUDED.expired_count,
                    metadata = EXCLUDED.metadata,
                    created_by = EXCLUDED.created_by
                """,
                run.batch_run_id,
                run.anthropic_batch_id,
                run.status,
                run.request_count,
                run.total_size_bytes,
                run.submitted_at,
                run.ended_at,
                run.expires_at,
                run.success_count,
                run.error_count,
                run.canceled_count,
                run.expired_count,
                _json_or_none(run.metadata),
                run.created_by,
                run.created_at,
            )

    async def get_batch_run(self, batch_run_id: str) -> BatchRun | None:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                f"SELECT {_BATCH_RUN_COLS} FROM batch_runs WHERE batch_run_id = $1",
                batch_run_id,
            )
        return _row_to_batch_run(row) if row else None

    async def list_batch_runs(
        self, status: BatchRunStatus | None = None
    ) -> list[BatchRun]:
        async with _acquire(self._factory) as conn:
            if status is not None:
                rows = await conn.fetch(
                    f"SELECT {_BATCH_RUN_COLS} FROM batch_runs "
                    "WHERE status = $1 ORDER BY created_at DESC",
                    status,
                )
            else:
                rows = await conn.fetch(
                    f"SELECT {_BATCH_RUN_COLS} FROM batch_runs ORDER BY created_at DESC"
                )
        return [_row_to_batch_run(r) for r in rows]

    async def save_batch_result(self, result: BatchResult) -> None:
        async with _acquire(self._factory) as conn:
            await conn.execute(
                """
                INSERT INTO batch_results (
                    batch_run_id, custom_id, task_id, status, response, error,
                    final_text, input_tokens, output_tokens,
                    cache_read_tokens, cache_creation_tokens, completed_at, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (batch_run_id, custom_id) DO UPDATE SET
                    task_id = EXCLUDED.task_id,
                    status = EXCLUDED.status,
                    response = EXCLUDED.response,
                    error = EXCLUDED.error,
                    final_text = EXCLUDED.final_text,
                    input_tokens = EXCLUDED.input_tokens,
                    output_tokens = EXCLUDED.output_tokens,
                    cache_read_tokens = EXCLUDED.cache_read_tokens,
                    cache_creation_tokens = EXCLUDED.cache_creation_tokens,
                    completed_at = EXCLUDED.completed_at
                """,
                result.batch_run_id,
                result.custom_id,
                result.task_id,
                result.status,
                _json_or_none(result.response),
                _json_or_none(result.error),
                result.final_text,
                result.input_tokens,
                result.output_tokens,
                result.cache_read_tokens,
                result.cache_creation_tokens,
                result.completed_at,
                datetime.now(timezone.utc),
            )

    async def list_batch_results(self, batch_run_id: str) -> list[BatchResult]:
        async with _acquire(self._factory) as conn:
            rows = await conn.fetch(
                """
                SELECT batch_run_id, custom_id, task_id, status, response, error,
                       final_text, input_tokens, output_tokens,
                       cache_read_tokens, cache_creation_tokens, completed_at
                FROM batch_results WHERE batch_run_id = $1
                ORDER BY custom_id
                """,
                batch_run_id,
            )
        return [_row_to_batch_result(r) for r in rows]

    async def find_result_by_task_id(
        self, task_id: str, tenant_id: str | None = None
    ) -> BatchResult | None:
        if tenant_id is not None:
            raise NotImplementedError(
                "tenant-scoped batch result lookup requires the R80 SQL migration"
            )
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                SELECT batch_run_id, custom_id, task_id, status, response, error,
                       final_text, input_tokens, output_tokens,
                       cache_read_tokens, cache_creation_tokens, completed_at
                FROM batch_results WHERE task_id = $1
                ORDER BY completed_at DESC NULLS LAST LIMIT 1
                """,
                task_id,
            )
        return _row_to_batch_result(row) if row else None


def _row_to_batch_run(row: Any) -> BatchRun:
    return BatchRun(
        batch_run_id=row["batch_run_id"],
        status=row["status"],
        request_count=row["request_count"],
        total_size_bytes=row["total_size_bytes"] or 0,
        anthropic_batch_id=row["anthropic_batch_id"],
        submitted_at=row["submitted_at"],
        ended_at=row["ended_at"],
        expires_at=row["expires_at"],
        success_count=row["success_count"] or 0,
        error_count=row["error_count"] or 0,
        canceled_count=row["canceled_count"] or 0,
        expired_count=row["expired_count"] or 0,
        metadata=_parse_jsonb(row["metadata"]) or {},
        created_by=row["created_by"],
        tenant_id=(_parse_jsonb(row["metadata"]) or {}).get("tenant_id"),
        created_at=row["created_at"],
    )


def _row_to_batch_result(row: Any) -> BatchResult:
    return BatchResult(
        batch_run_id=row["batch_run_id"],
        custom_id=row["custom_id"],
        status=row["status"],
        task_id=row["task_id"],
        response=_parse_jsonb(row["response"]),
        error=_parse_jsonb(row["error"]),
        final_text=row["final_text"] or "",
        input_tokens=row["input_tokens"] or 0,
        output_tokens=row["output_tokens"] or 0,
        cache_read_tokens=row["cache_read_tokens"] or 0,
        cache_creation_tokens=row["cache_creation_tokens"] or 0,
        completed_at=row["completed_at"],
    )


# ─────────────────────────────────────────────────────────────────
# 2. PostgresCostStore
# ─────────────────────────────────────────────────────────────────


class PostgresCostStore:
    """``CostStore`` backed by alembic 0183."""

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._factory = conn_factory

    async def save_estimate(self, estimate: CostEstimate) -> None:
        async with _acquire(self._factory) as conn:
            await conn.execute(
                """
                INSERT INTO cost_estimates (
                    estimate_id, call_id, model, is_batch,
                    input_tokens_estimated, output_tokens_estimated,
                    cost_usd_estimated, workspace, priority, task_type
                ) VALUES (gen_random_uuid()::text, $1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (estimate_id) DO NOTHING
                """,
                estimate.call_id,
                estimate.model,
                estimate.is_batch,
                estimate.input_tokens_estimated,
                estimate.output_tokens_estimated,
                estimate.cost_usd_estimated,
                estimate.workspace,
                estimate.priority,
                estimate.task_type,
            )

    async def update_actual(self, actual: CostActual) -> None:
        async with _acquire(self._factory) as conn:
            await conn.execute(
                """
                UPDATE cost_estimates SET
                    input_tokens_actual = $2,
                    output_tokens_actual = $3,
                    cache_read_tokens_actual = $4,
                    cache_creation_tokens_actual = $5,
                    cost_usd_actual = $6,
                    completed_at = NOW()
                WHERE call_id = $1
                """,
                actual.call_id,
                actual.input_tokens,
                actual.output_tokens,
                actual.cache_read_tokens,
                actual.cache_creation_tokens,
                actual.cost_usd,
            )

    async def spend_in_period(
        self,
        scope: ScopeKey,
        period: PeriodKind,
        *,
        now: datetime | None = None,
    ) -> float:
        now = now or datetime.now(timezone.utc)
        scope_clause, scope_args = self._scope_where(scope)

        if period == "per_batch":
            time_clause = ""
            time_args: tuple[Any, ...] = ()
        else:
            cutoff = _period_start(period, now)
            time_clause = f" AND created_at >= ${len(scope_args) + 1}"
            time_args = (cutoff,)

        sql = f"""
            SELECT COALESCE(SUM(
                CASE WHEN cost_usd_actual IS NOT NULL
                     THEN cost_usd_actual
                     ELSE cost_usd_estimated END
            ), 0)::float
            FROM cost_estimates
            WHERE {scope_clause}{time_clause}
        """
        async with _acquire(self._factory) as conn:
            value = await conn.fetchval(sql, *scope_args, *time_args)
        return float(value or 0.0)

    @staticmethod
    def _scope_where(scope: ScopeKey) -> tuple[str, tuple[Any, ...]]:
        """Translate a ScopeKey to a WHERE-clause + args tuple."""
        if scope.kind == "global":
            return "TRUE", ()
        column = {
            "workspace": "workspace",
            "priority": "priority",
            "task_type": "task_type",
            "model": "model",
        }.get(scope.kind)
        if column is None:
            return "FALSE", ()
        return f"{column} = $1", (scope.key,)

    async def upsert_budget(self, budget: BudgetCap) -> None:
        async with _acquire(self._factory) as conn:
            await conn.execute(
                """
                INSERT INTO cost_budgets (
                    budget_id, scope_kind, scope_key,
                    daily_limit_usd, monthly_limit_usd, per_batch_limit_usd,
                    enabled, created_at, updated_at
                ) VALUES (
                    gen_random_uuid()::text, $1, $2, $3, $4, $5, $6, NOW(), NOW()
                )
                ON CONFLICT (scope_kind, scope_key) DO UPDATE SET
                    daily_limit_usd = EXCLUDED.daily_limit_usd,
                    monthly_limit_usd = EXCLUDED.monthly_limit_usd,
                    per_batch_limit_usd = EXCLUDED.per_batch_limit_usd,
                    enabled = EXCLUDED.enabled,
                    updated_at = NOW()
                """,
                budget.scope.kind,
                budget.scope.key,
                budget.daily_limit_usd,
                budget.monthly_limit_usd,
                budget.per_batch_limit_usd,
                budget.enabled,
            )

    async def get_budget(self, scope: ScopeKey) -> BudgetCap | None:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                SELECT scope_kind, scope_key, daily_limit_usd, monthly_limit_usd,
                       per_batch_limit_usd, enabled
                FROM cost_budgets WHERE scope_kind = $1 AND scope_key = $2
                """,
                scope.kind,
                scope.key,
            )
        if row is None:
            return None
        return BudgetCap(
            scope=ScopeKey(kind=row["scope_kind"], key=row["scope_key"]),
            daily_limit_usd=row["daily_limit_usd"],
            monthly_limit_usd=row["monthly_limit_usd"],
            per_batch_limit_usd=row["per_batch_limit_usd"],
            enabled=row["enabled"],
        )

    async def list_budgets(
        self, *, enabled_only: bool = False
    ) -> list[BudgetCap]:
        async with _acquire(self._factory) as conn:
            if enabled_only:
                rows = await conn.fetch(
                    "SELECT scope_kind, scope_key, daily_limit_usd, "
                    "monthly_limit_usd, per_batch_limit_usd, enabled "
                    "FROM cost_budgets WHERE enabled = TRUE"
                )
            else:
                rows = await conn.fetch(
                    "SELECT scope_kind, scope_key, daily_limit_usd, "
                    "monthly_limit_usd, per_batch_limit_usd, enabled "
                    "FROM cost_budgets"
                )
        return [
            BudgetCap(
                scope=ScopeKey(kind=r["scope_kind"], key=r["scope_key"]),
                daily_limit_usd=r["daily_limit_usd"],
                monthly_limit_usd=r["monthly_limit_usd"],
                per_batch_limit_usd=r["per_batch_limit_usd"],
                enabled=r["enabled"],
            )
            for r in rows
        ]

    async def save_alert(self, alert: BudgetAlert) -> None:
        async with _acquire(self._factory) as conn:
            await conn.execute(
                """
                INSERT INTO cost_alerts (
                    alert_id, scope_kind, scope_key, period, level,
                    threshold_usd, observed_usd, action_taken, fired_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (alert_id) DO NOTHING
                """,
                alert.alert_id,
                alert.scope.kind,
                alert.scope.key,
                alert.period,
                alert.level,
                alert.threshold_usd,
                alert.observed_usd,
                alert.action,
                alert.fired_at,
            )

    async def list_alerts(
        self, scope: ScopeKey | None = None, *, since: datetime | None = None
    ) -> list[BudgetAlert]:
        clauses = []
        args: list[Any] = []
        if scope is not None:
            args.extend((scope.kind, scope.key))
            clauses.append(f"scope_kind = ${len(args) - 1} AND scope_key = ${len(args)}")
        if since is not None:
            args.append(since)
            clauses.append(f"fired_at >= ${len(args)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        async with _acquire(self._factory) as conn:
            rows = await conn.fetch(
                f"""
                SELECT alert_id, scope_kind, scope_key, period, level,
                       threshold_usd, observed_usd, action_taken, fired_at
                FROM cost_alerts {where}
                ORDER BY fired_at DESC
                """,
                *args,
            )
        return [
            BudgetAlert(
                alert_id=r["alert_id"],
                scope=ScopeKey(kind=r["scope_kind"], key=r["scope_key"]),
                period=r["period"],
                level=r["level"],
                threshold_usd=r["threshold_usd"],
                observed_usd=r["observed_usd"],
                action=r["action_taken"],
                fired_at=r["fired_at"],
            )
            for r in rows
        ]


# ─────────────────────────────────────────────────────────────────
# 3. PostgresExternalToolRegistryStore
# ─────────────────────────────────────────────────────────────────


_DEFINITIONS_BY_NAME = {d.tool_name: d for d in DEFAULT_TOOL_DEFINITIONS}


class PostgresExternalToolRegistryStore:
    """``ExternalToolRegistryStore`` backed by alembic 0184.

    Persists deployment bindings (operator-configured Docker images /
    REST URLs / binary paths). The static ``ExternalToolDefinition``
    metadata stays in code; this layer only persists what changes.
    """

    def __init__(
        self,
        conn_factory: ConnFactory,
        *,
        definitions: dict[str, ExternalToolDefinition] | None = None,
    ) -> None:
        self._factory = conn_factory
        self._defs = definitions or _DEFINITIONS_BY_NAME

    async def upsert_binding(self, binding: ExternalToolBinding) -> None:
        async with _acquire(self._factory) as conn:
            await conn.execute(
                """
                INSERT INTO external_tool_registry (
                    tool_name, integration_type, license_tier, sandbox_required,
                    config, enabled, deployed_at, last_health_check, health_status,
                    description, created_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW(), NOW()
                )
                ON CONFLICT (tool_name) DO UPDATE SET
                    integration_type = EXCLUDED.integration_type,
                    license_tier = EXCLUDED.license_tier,
                    sandbox_required = EXCLUDED.sandbox_required,
                    config = EXCLUDED.config,
                    enabled = EXCLUDED.enabled,
                    deployed_at = EXCLUDED.deployed_at,
                    last_health_check = EXCLUDED.last_health_check,
                    health_status = EXCLUDED.health_status,
                    description = EXCLUDED.description,
                    updated_at = NOW()
                """,
                binding.definition.tool_name,
                binding.definition.integration_type,
                binding.definition.license_tier,
                binding.definition.sandbox_required,
                _json_or_none(binding.config),
                binding.enabled,
                binding.deployed_at,
                binding.last_health_check,
                binding.health_status,
                binding.definition.description,
            )

    async def get_binding(self, tool_name: str) -> ExternalToolBinding | None:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                SELECT tool_name, integration_type, license_tier, sandbox_required,
                       config, enabled, deployed_at, last_health_check, health_status,
                       description
                FROM external_tool_registry WHERE tool_name = $1
                """,
                tool_name,
            )
        return self._row_to_binding(row) if row else None

    async def list_bindings(
        self, *, enabled_only: bool = False
    ) -> list[ExternalToolBinding]:
        async with _acquire(self._factory) as conn:
            if enabled_only:
                rows = await conn.fetch(
                    """
                    SELECT tool_name, integration_type, license_tier, sandbox_required,
                           config, enabled, deployed_at, last_health_check,
                           health_status, description
                    FROM external_tool_registry WHERE enabled = TRUE
                    """
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT tool_name, integration_type, license_tier, sandbox_required,
                           config, enabled, deployed_at, last_health_check,
                           health_status, description
                    FROM external_tool_registry
                    """
                )
        return [self._row_to_binding(r) for r in rows]

    async def set_health(
        self, tool_name: str, status: HealthStatus, checked_at: datetime
    ) -> None:
        async with _acquire(self._factory) as conn:
            await conn.execute(
                """
                UPDATE external_tool_registry
                SET health_status = $2, last_health_check = $3, updated_at = NOW()
                WHERE tool_name = $1
                """,
                tool_name,
                status,
                checked_at,
            )

    def _row_to_binding(self, row: Any) -> ExternalToolBinding:
        # Reconstruct the ExternalToolDefinition from the static registry
        # using the stored tool_name. Falls back to building an ad-hoc
        # definition from the row if the tool name isn't in the static
        # registry (drift case — operator-added tool not yet known to code).
        existing = self._defs.get(row["tool_name"])
        if existing is None:
            existing = ExternalToolDefinition(
                tool_name=row["tool_name"],
                integration_type=row["integration_type"],
                license_tier=row["license_tier"],
                sandbox_required=row["sandbox_required"],
                description=row["description"] or "",
                default_config={},
            )
        return ExternalToolBinding(
            definition=existing,
            config=_parse_jsonb(row["config"]) or {},
            enabled=row["enabled"],
            deployed_at=row["deployed_at"],
            last_health_check=row["last_health_check"],
            health_status=row["health_status"] or "unknown",
        )


# ─────────────────────────────────────────────────────────────────
# 4. PostgresDeadLetterQueue
# ─────────────────────────────────────────────────────────────────


class PostgresDeadLetterQueue:
    """``DeadLetterQueue`` backed by alembic 0185."""

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._factory = conn_factory

    async def deposit(self, entry: DLQEntry) -> None:
        async with _acquire(self._factory) as conn:
            await conn.execute(
                """
                INSERT INTO dlq_entries (
                    entry_id, workspace, model, classification, attempts_made,
                    last_status_code, last_exception_repr, last_reason,
                    request_metadata, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (entry_id) DO NOTHING
                """,
                entry.entry_id,
                entry.workspace,
                entry.model,
                entry.classification,
                entry.attempts_made,
                entry.last_status_code,
                entry.last_exception_repr,
                entry.last_reason,
                _json_or_none(entry.request_metadata),
                entry.created_at,
            )

    async def list_entries(
        self, *, since: datetime | None = None
    ) -> list[DLQEntry]:
        async with _acquire(self._factory) as conn:
            if since is not None:
                rows = await conn.fetch(
                    """
                    SELECT entry_id, workspace, model, classification, attempts_made,
                           last_status_code, last_exception_repr, last_reason,
                           request_metadata, created_at
                    FROM dlq_entries WHERE created_at >= $1
                    ORDER BY created_at DESC
                    """,
                    since,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT entry_id, workspace, model, classification, attempts_made,
                           last_status_code, last_exception_repr, last_reason,
                           request_metadata, created_at
                    FROM dlq_entries ORDER BY created_at DESC
                    """
                )
        return [
            DLQEntry(
                entry_id=r["entry_id"],
                workspace=r["workspace"],
                model=r["model"],
                classification=r["classification"],
                attempts_made=r["attempts_made"],
                last_status_code=r["last_status_code"],
                last_exception_repr=r["last_exception_repr"],
                last_reason=r["last_reason"],
                request_metadata=_parse_jsonb(r["request_metadata"]) or {},
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def remove(self, entry_id: str) -> bool:
        async with _acquire(self._factory) as conn:
            result = await conn.execute(
                "DELETE FROM dlq_entries WHERE entry_id = $1", entry_id
            )
        # asyncpg returns "DELETE N" — we want True if N>0
        if isinstance(result, str):
            try:
                return int(result.rsplit(" ", 1)[-1]) > 0
            except (ValueError, IndexError):
                return False
        return bool(result)
