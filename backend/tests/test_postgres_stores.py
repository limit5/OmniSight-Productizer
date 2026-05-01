"""Postgres store impl tests — mock asyncpg connection.

Doesn't require a real Postgres instance. The mock connection records
SQL + args, returns scripted fetch responses. Tests validate:

  - SQL shape (INSERT / SELECT / UPDATE / DELETE statements present)
  - args passed match the dataclass fields
  - row → dataclass round-trip via fake fetch responses
  - JSONB serialize / parse round-trip
  - all 4 store impls satisfy their respective Protocol contracts

Real-PG integration tests live separately behind a pytest mark + env
var (skipped in the unit suite).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from backend.agents.batch_client import BatchResult, BatchRun
from backend.agents.cost_guard import (
    BudgetAlert,
    BudgetCap,
    CostActual,
    CostEstimate,
    ScopeKey,
)
from backend.agents.external_tool_registry import (
    DEFAULT_TOOL_DEFINITIONS,
    ExternalToolBinding,
)
from backend.agents.postgres_stores import (
    PostgresBatchPersistence,
    PostgresCostStore,
    PostgresDeadLetterQueue,
    PostgresExternalToolRegistryStore,
    _json_or_none,
    _parse_jsonb,
)
from backend.agents.rate_limiter import DLQEntry


# ─── Mock asyncpg shape ──────────────────────────────────────────


class _MockConn:
    """Minimal asyncpg.Connection stand-in.

    Records every execute / fetch / fetchrow / fetchval call as
    ``(method, sql, args)`` tuples in ``self.calls``. Scripted
    responses: per-call iterator, raises if exhausted unexpectedly.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple]] = []
        self.execute_returns: list[str] = []
        self.fetch_returns: list[list[dict]] = []
        self.fetchrow_returns: list[dict | None] = []
        self.fetchval_returns: list[Any] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append(("execute", sql, args))
        return self.execute_returns.pop(0) if self.execute_returns else "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        self.calls.append(("fetch", sql, args))
        return self.fetch_returns.pop(0) if self.fetch_returns else []

    async def fetchrow(self, sql: str, *args: Any) -> dict | None:
        self.calls.append(("fetchrow", sql, args))
        return self.fetchrow_returns.pop(0) if self.fetchrow_returns else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append(("fetchval", sql, args))
        return self.fetchval_returns.pop(0) if self.fetchval_returns else 0

    def first_call(self, method: str | None = None) -> tuple[str, str, tuple]:
        for c in self.calls:
            if method is None or c[0] == method:
                return c
        raise AssertionError(f"no {method} call recorded")


class _MockAcquireCM:
    """Mimics ``async with pool.acquire() as conn``."""

    def __init__(self, conn: _MockConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _MockConn:
        return self.conn

    async def __aexit__(self, *_args) -> bool:
        return False


def _make_store_with_conn(cls, **kwargs):
    conn = _MockConn()
    factory = lambda: _MockAcquireCM(conn)
    return cls(conn_factory=factory, **kwargs), conn


# ─── _json_or_none / _parse_jsonb helpers ────────────────────────


def test_json_or_none_passes_none():
    assert _json_or_none(None) is None


def test_json_or_none_serializes_dict():
    assert json.loads(_json_or_none({"a": 1})) == {"a": 1}


def test_json_or_none_passes_through_valid_json_string():
    assert _json_or_none('{"a":1}') == '{"a":1}'


def test_json_or_none_wraps_non_json_string():
    """A bare string that's not valid JSON gets re-wrapped (defensive)."""
    out = _json_or_none("hello")
    assert out is not None
    assert json.loads(out) == "hello"


def test_parse_jsonb_passes_none():
    assert _parse_jsonb(None) is None


def test_parse_jsonb_decodes_string():
    assert _parse_jsonb('{"a":1}') == {"a": 1}


def test_parse_jsonb_passthrough_dict():
    """asyncpg may already decode JSONB to dict — pass through."""
    src = {"x": [1, 2]}
    assert _parse_jsonb(src) == src


# ─── PostgresBatchPersistence ────────────────────────────────────


@pytest.mark.asyncio
async def test_pg_batch_save_run_calls_insert_with_args():
    store, conn = _make_store_with_conn(PostgresBatchPersistence)
    now = datetime.now(timezone.utc)
    run = BatchRun(
        batch_run_id="br_1",
        status="submitted",
        request_count=5,
        anthropic_batch_id="batch_a",
        submitted_at=now,
        success_count=3,
        metadata={"phase": "HD.1"},
        created_by="agent-bot",
        created_at=now,
    )
    await store.save_batch_run(run)
    method, sql, args = conn.first_call("execute")
    assert "INSERT INTO batch_runs" in sql
    assert "ON CONFLICT" in sql  # upsert behaviour
    # args order matches the SQL placeholders
    assert args[0] == "br_1"
    assert args[1] == "batch_a"
    assert args[2] == "submitted"
    # metadata serialized to JSON string
    assert json.loads(args[12]) == {"phase": "HD.1"}


@pytest.mark.asyncio
async def test_pg_batch_get_run_round_trip():
    store, conn = _make_store_with_conn(PostgresBatchPersistence)
    now = datetime.now(timezone.utc)
    conn.fetchrow_returns.append({
        "batch_run_id": "br_x",
        "anthropic_batch_id": "batch_q",
        "status": "ended",
        "request_count": 10,
        "total_size_bytes": 12345,
        "submitted_at": now,
        "ended_at": now,
        "expires_at": None,
        "success_count": 9,
        "error_count": 1,
        "canceled_count": 0,
        "expired_count": 0,
        "metadata": {"phase": "HD.5"},
        "created_by": "bot",
        "created_at": now,
    })
    run = await store.get_batch_run("br_x")
    assert run is not None
    assert run.batch_run_id == "br_x"
    assert run.status == "ended"
    assert run.success_count == 9
    assert run.metadata == {"phase": "HD.5"}


@pytest.mark.asyncio
async def test_pg_batch_get_run_missing_returns_none():
    store, conn = _make_store_with_conn(PostgresBatchPersistence)
    conn.fetchrow_returns.append(None)
    run = await store.get_batch_run("nope")
    assert run is None


@pytest.mark.asyncio
async def test_pg_batch_save_result_uses_composite_pk():
    store, conn = _make_store_with_conn(PostgresBatchPersistence)
    result = BatchResult(
        batch_run_id="br_1",
        custom_id="c1",
        status="succeeded",
        task_id="t1",
        response={"content": "ok"},
        final_text="ok",
        input_tokens=5,
        output_tokens=2,
        completed_at=datetime.now(timezone.utc),
    )
    await store.save_batch_result(result)
    method, sql, args = conn.first_call("execute")
    assert "INSERT INTO batch_results" in sql
    assert "ON CONFLICT (batch_run_id, custom_id)" in sql
    assert args[0] == "br_1"
    assert args[1] == "c1"
    assert args[2] == "t1"
    assert args[3] == "succeeded"
    assert json.loads(args[4]) == {"content": "ok"}


@pytest.mark.asyncio
async def test_pg_batch_find_by_task_id():
    store, conn = _make_store_with_conn(PostgresBatchPersistence)
    now = datetime.now(timezone.utc)
    conn.fetchrow_returns.append({
        "batch_run_id": "br_1",
        "custom_id": "c1",
        "task_id": "lookup_me",
        "status": "succeeded",
        "response": None,
        "error": None,
        "final_text": "result",
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "completed_at": now,
    })
    found = await store.find_result_by_task_id("lookup_me")
    assert found is not None
    assert found.task_id == "lookup_me"
    method, sql, args = conn.first_call("fetchrow")
    assert "WHERE task_id = $1" in sql
    assert args == ("lookup_me",)


@pytest.mark.asyncio
async def test_pg_batch_list_runs_with_status_filter():
    store, conn = _make_store_with_conn(PostgresBatchPersistence)
    conn.fetch_returns.append([])
    await store.list_batch_runs(status="submitted")
    method, sql, args = conn.first_call("fetch")
    assert "WHERE status = $1" in sql
    assert args == ("submitted",)


# ─── PostgresCostStore ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_pg_cost_save_estimate():
    store, conn = _make_store_with_conn(PostgresCostStore)
    est = CostEstimate(
        call_id="call_1",
        model="claude-sonnet-4-6",
        is_batch=True,
        input_tokens_estimated=5000,
        output_tokens_estimated=2000,
        cost_usd_estimated=0.0225,
        workspace="prod",
        priority="HD",
        task_type="hd_parse_kicad",
    )
    await store.save_estimate(est)
    method, sql, args = conn.first_call("execute")
    assert "INSERT INTO cost_estimates" in sql
    assert args[0] == "call_1"
    assert args[1] == "claude-sonnet-4-6"
    assert args[2] is True  # is_batch
    assert args[6] == "prod"
    assert args[7] == "HD"


@pytest.mark.asyncio
async def test_pg_cost_update_actual():
    store, conn = _make_store_with_conn(PostgresCostStore)
    actual = CostActual(
        call_id="call_1",
        input_tokens=5500, output_tokens=1900,
        cache_read_tokens=100,
        cache_creation_tokens=50,
        cost_usd=0.025,
    )
    await store.update_actual(actual)
    method, sql, args = conn.first_call("execute")
    assert "UPDATE cost_estimates" in sql
    assert args[0] == "call_1"
    assert args[1] == 5500
    assert args[5] == 0.025


@pytest.mark.asyncio
async def test_pg_cost_spend_in_period_global_scope():
    store, conn = _make_store_with_conn(PostgresCostStore)
    conn.fetchval_returns.append(42.5)
    spent = await store.spend_in_period(
        ScopeKey(kind="global", key="*"), "daily",
    )
    assert spent == 42.5
    method, sql, args = conn.first_call("fetchval")
    assert "TRUE" in sql  # global scope WHERE clause is "TRUE"
    assert "created_at >=" in sql  # daily filter present


@pytest.mark.asyncio
async def test_pg_cost_spend_in_period_priority_scope():
    store, conn = _make_store_with_conn(PostgresCostStore)
    conn.fetchval_returns.append(10.0)
    await store.spend_in_period(
        ScopeKey(kind="priority", key="HD"), "monthly",
    )
    method, sql, args = conn.first_call("fetchval")
    assert "priority = $1" in sql
    assert args[0] == "HD"


@pytest.mark.asyncio
async def test_pg_cost_per_batch_scope_no_time_filter():
    store, conn = _make_store_with_conn(PostgresCostStore)
    conn.fetchval_returns.append(0.5)
    await store.spend_in_period(
        ScopeKey(kind="priority", key="HD"), "per_batch",
    )
    method, sql, args = conn.first_call("fetchval")
    assert "created_at >=" not in sql  # per_batch is point-in-time


@pytest.mark.asyncio
async def test_pg_cost_upsert_budget():
    store, conn = _make_store_with_conn(PostgresCostStore)
    cap = BudgetCap(
        scope=ScopeKey(kind="priority", key="HD"),
        daily_limit_usd=10.0,
        monthly_limit_usd=200.0,
        per_batch_limit_usd=2.0,
    )
    await store.upsert_budget(cap)
    method, sql, args = conn.first_call("execute")
    assert "INSERT INTO cost_budgets" in sql
    assert "ON CONFLICT (scope_kind, scope_key)" in sql
    assert args[0] == "priority"
    assert args[1] == "HD"
    assert args[2] == 10.0


@pytest.mark.asyncio
async def test_pg_cost_get_budget_round_trip():
    store, conn = _make_store_with_conn(PostgresCostStore)
    conn.fetchrow_returns.append({
        "scope_kind": "workspace",
        "scope_key": "prod",
        "daily_limit_usd": 50.0,
        "monthly_limit_usd": 500.0,
        "per_batch_limit_usd": None,
        "enabled": True,
    })
    cap = await store.get_budget(ScopeKey("workspace", "prod"))
    assert cap is not None
    assert cap.daily_limit_usd == 50.0
    assert cap.per_batch_limit_usd is None


@pytest.mark.asyncio
async def test_pg_cost_save_alert():
    store, conn = _make_store_with_conn(PostgresCostStore)
    alert = BudgetAlert(
        alert_id="a1",
        scope=ScopeKey("priority", "HD"),
        period="daily",
        level="cap_100",
        threshold_usd=10.0,
        observed_usd=10.5,
        action="throttle",
        fired_at=datetime.now(timezone.utc),
    )
    await store.save_alert(alert)
    method, sql, args = conn.first_call("execute")
    assert "INSERT INTO cost_alerts" in sql
    assert args[0] == "a1"
    assert args[3] == "daily"
    assert args[4] == "cap_100"


@pytest.mark.asyncio
async def test_pg_cost_list_alerts_no_filters():
    store, conn = _make_store_with_conn(PostgresCostStore)
    conn.fetch_returns.append([])
    await store.list_alerts()
    method, sql, args = conn.first_call("fetch")
    assert "WHERE" not in sql
    assert "ORDER BY fired_at DESC" in sql


@pytest.mark.asyncio
async def test_pg_cost_list_alerts_with_scope_and_since():
    store, conn = _make_store_with_conn(PostgresCostStore)
    conn.fetch_returns.append([])
    cutoff = datetime(2026, 5, 1, tzinfo=timezone.utc)
    await store.list_alerts(ScopeKey("priority", "HD"), since=cutoff)
    method, sql, args = conn.first_call("fetch")
    assert "scope_kind" in sql
    assert "fired_at >=" in sql
    assert args == ("priority", "HD", cutoff)


# ─── PostgresExternalToolRegistryStore ───────────────────────────


@pytest.mark.asyncio
async def test_pg_registry_upsert_binding():
    store, conn = _make_store_with_conn(PostgresExternalToolRegistryStore)
    vision_def = next(d for d in DEFAULT_TOOL_DEFINITIONS if d.tool_name == "VisionParse")
    binding = ExternalToolBinding(
        definition=vision_def,
        config={"module": "vision_parse", "callable": "VisionParser"},
        enabled=True,
    )
    await store.upsert_binding(binding)
    method, sql, args = conn.first_call("execute")
    assert "INSERT INTO external_tool_registry" in sql
    assert args[0] == "VisionParse"
    assert args[1] == "python_lib"
    assert args[2] == "mit_apache_bsd"


@pytest.mark.asyncio
async def test_pg_registry_get_binding_round_trip():
    store, conn = _make_store_with_conn(PostgresExternalToolRegistryStore)
    conn.fetchrow_returns.append({
        "tool_name": "VisionParse",
        "integration_type": "python_lib",
        "license_tier": "mit_apache_bsd",
        "sandbox_required": False,
        "config": {"module": "vision_parse"},
        "enabled": True,
        "deployed_at": datetime.now(timezone.utc),
        "last_health_check": None,
        "health_status": "healthy",
        "description": "x",
    })
    b = await store.get_binding("VisionParse")
    assert b is not None
    assert b.definition.tool_name == "VisionParse"
    assert b.config["module"] == "vision_parse"
    assert b.health_status == "healthy"


@pytest.mark.asyncio
async def test_pg_registry_unknown_tool_falls_back_to_ad_hoc_def():
    """If DB has a tool_name not in static DEFAULT_TOOL_DEFINITIONS,
    we still build a binding (operator added a tool we don't know yet)."""
    store, conn = _make_store_with_conn(PostgresExternalToolRegistryStore)
    conn.fetchrow_returns.append({
        "tool_name": "NewTool",
        "integration_type": "python_lib",
        "license_tier": "mit_apache_bsd",
        "sandbox_required": False,
        "config": {},
        "enabled": True,
        "deployed_at": None,
        "last_health_check": None,
        "health_status": None,
        "description": "operator-added",
    })
    b = await store.get_binding("NewTool")
    assert b is not None
    assert b.definition.tool_name == "NewTool"
    assert b.health_status == "unknown"


@pytest.mark.asyncio
async def test_pg_registry_list_enabled_only():
    store, conn = _make_store_with_conn(PostgresExternalToolRegistryStore)
    conn.fetch_returns.append([])
    await store.list_bindings(enabled_only=True)
    method, sql, args = conn.first_call("fetch")
    assert "WHERE enabled = TRUE" in sql


@pytest.mark.asyncio
async def test_pg_registry_set_health():
    store, conn = _make_store_with_conn(PostgresExternalToolRegistryStore)
    now = datetime.now(timezone.utc)
    await store.set_health("VisionParse", "healthy", now)
    method, sql, args = conn.first_call("execute")
    assert "UPDATE external_tool_registry" in sql
    assert args == ("VisionParse", "healthy", now)


# ─── PostgresDeadLetterQueue ─────────────────────────────────────


@pytest.mark.asyncio
async def test_pg_dlq_deposit():
    store, conn = _make_store_with_conn(PostgresDeadLetterQueue)
    entry = DLQEntry(
        entry_id="dlq_1",
        workspace="batch",
        model="claude-sonnet-4-6",
        classification="rate_limited",
        attempts_made=5,
        last_status_code=429,
        last_exception_repr="RateLimitError(...)",
        last_reason="too many requests",
        request_metadata={"task_id": "t42"},
        created_at=datetime.now(timezone.utc),
    )
    await store.deposit(entry)
    method, sql, args = conn.first_call("execute")
    assert "INSERT INTO dlq_entries" in sql
    assert args[0] == "dlq_1"
    assert args[2] == "claude-sonnet-4-6"
    assert args[3] == "rate_limited"
    assert json.loads(args[8]) == {"task_id": "t42"}


@pytest.mark.asyncio
async def test_pg_dlq_list_entries_with_since():
    store, conn = _make_store_with_conn(PostgresDeadLetterQueue)
    conn.fetch_returns.append([])
    cutoff = datetime(2026, 5, 1, tzinfo=timezone.utc)
    await store.list_entries(since=cutoff)
    method, sql, args = conn.first_call("fetch")
    assert "WHERE created_at >= $1" in sql
    assert args == (cutoff,)


@pytest.mark.asyncio
async def test_pg_dlq_list_entries_no_filter():
    store, conn = _make_store_with_conn(PostgresDeadLetterQueue)
    conn.fetch_returns.append([{
        "entry_id": "x",
        "workspace": "w",
        "model": "m",
        "classification": "retryable",
        "attempts_made": 3,
        "last_status_code": 503,
        "last_exception_repr": None,
        "last_reason": "boom",
        "request_metadata": {"k": 1},
        "created_at": datetime.now(timezone.utc),
    }])
    out = await store.list_entries()
    assert len(out) == 1
    assert out[0].entry_id == "x"
    assert out[0].request_metadata == {"k": 1}


@pytest.mark.asyncio
async def test_pg_dlq_remove_existing_returns_true():
    store, conn = _make_store_with_conn(PostgresDeadLetterQueue)
    conn.execute_returns.append("DELETE 1")
    assert await store.remove("dlq_1") is True


@pytest.mark.asyncio
async def test_pg_dlq_remove_missing_returns_false():
    store, conn = _make_store_with_conn(PostgresDeadLetterQueue)
    conn.execute_returns.append("DELETE 0")
    assert await store.remove("dlq_z") is False


@pytest.mark.asyncio
async def test_pg_dlq_remove_handles_unexpected_response():
    """Defensive — if asyncpg ever changes response format, fall back gracefully."""
    store, conn = _make_store_with_conn(PostgresDeadLetterQueue)
    conn.execute_returns.append("WAT")
    assert await store.remove("dlq_x") is False


# ─── Protocol conformance smoke tests ────────────────────────────


def test_pg_batch_persistence_satisfies_protocol():
    """PostgresBatchPersistence has every method on the BatchPersistence Protocol."""
    from backend.agents.batch_client import BatchPersistence as Proto
    pg = PostgresBatchPersistence(conn_factory=lambda: None)
    for name in (
        "save_batch_run", "get_batch_run", "list_batch_runs",
        "save_batch_result", "list_batch_results", "find_result_by_task_id",
    ):
        assert callable(getattr(pg, name)), f"missing {name}"


def test_pg_cost_store_satisfies_protocol():
    pg = PostgresCostStore(conn_factory=lambda: None)
    for name in (
        "save_estimate", "update_actual", "spend_in_period",
        "upsert_budget", "get_budget", "list_budgets",
        "save_alert", "list_alerts",
    ):
        assert callable(getattr(pg, name)), f"missing {name}"


def test_pg_registry_store_satisfies_protocol():
    pg = PostgresExternalToolRegistryStore(conn_factory=lambda: None)
    for name in (
        "upsert_binding", "get_binding", "list_bindings", "set_health",
    ):
        assert callable(getattr(pg, name)), f"missing {name}"


def test_pg_dlq_satisfies_protocol():
    pg = PostgresDeadLetterQueue(conn_factory=lambda: None)
    for name in ("deposit", "list_entries", "remove"):
        assert callable(getattr(pg, name)), f"missing {name}"
