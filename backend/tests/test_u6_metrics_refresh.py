"""Tests for the U6-0 GAP-6b DB-derived metrics refresh (offline + one throwaway-PG aggregation check)."""
from __future__ import annotations

import pytest

from backend import metrics
from backend.agents import u6_metrics_refresh

_ENV = u6_metrics_refresh._ENABLE_ENV


class _RaisingConn:
    """A connection whose GROUP BY fetch() raises (simulating a not-yet-migrated table) but fetchval() returns 0."""

    async def fetch(self, *_a):
        raise RuntimeError("undefined table simulation")

    async def fetchval(self, *_a):
        return 0


class _FakePool:
    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_a):
                return False

        return _CM()


async def _nosleep(_seconds: float) -> None:
    return None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False), ("", False), ("0", False), ("false", False),
        ("1", True), ("true", True), ("YES", True), ("on", True),
    ],
)
def test_u6_metrics_enabled_flag(monkeypatch: pytest.MonkeyPatch, value, expected) -> None:
    if value is None:
        monkeypatch.delenv(_ENV, raising=False)
    else:
        monkeypatch.setenv(_ENV, value)
    assert u6_metrics_refresh.u6_metrics_enabled() is expected


@pytest.mark.asyncio
async def test_disabled_loop_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)

    def _boom_get_pool():
        raise AssertionError("get_pool called while disabled")

    async def _boom_refresh(_pool):
        raise AssertionError("refresh called while disabled")

    monkeypatch.setattr(u6_metrics_refresh, "refresh_u6_metrics_once", _boom_refresh)
    ticks = await u6_metrics_refresh.run_u6_metrics_refresh_loop(
        get_pool=_boom_get_pool, should_continue=lambda: True, sleep=_nosleep,
    )
    assert ticks == 0


@pytest.mark.asyncio
async def test_enabled_loop_ticks_and_records_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    metrics.reset_for_tests()
    seen_pools: list = []

    async def _spy_refresh(pool):
        seen_pools.append(pool)
        return "ok"

    monkeypatch.setattr(u6_metrics_refresh, "refresh_u6_metrics_once", _spy_refresh)
    ticks = await u6_metrics_refresh.run_u6_metrics_refresh_loop(
        get_pool=lambda: "POOL", should_continue=lambda: True, max_ticks=3, sleep=_nosleep,
    )
    assert ticks == 3
    assert seen_pools == ["POOL", "POOL", "POOL"]   # get_pool called lazily each tick, its result passed to refresh
    assert metrics.REGISTRY.get_sample_value("omnisight_u6_metrics_refresh_total", {"outcome": "ok"}) == 3


@pytest.mark.asyncio
async def test_loop_records_error_when_get_pool_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    metrics.reset_for_tests()

    def _no_pool():
        raise RuntimeError("no pool yet (SQLite/no-DSN startup)")

    async def _unreached_refresh(_pool):
        raise AssertionError("refresh must not run without a pool")

    monkeypatch.setattr(u6_metrics_refresh, "refresh_u6_metrics_once", _unreached_refresh)
    ticks = await u6_metrics_refresh.run_u6_metrics_refresh_loop(
        get_pool=_no_pool, should_continue=lambda: True, max_ticks=2, sleep=_nosleep,
    )
    assert ticks == 2   # the loop keeps ticking (never crashes the lifespan) and records the failure
    assert metrics.REGISTRY.get_sample_value("omnisight_u6_metrics_refresh_total", {"outcome": "error"}) == 2


@pytest.mark.asyncio
async def test_enabled_invalid_interval_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    with pytest.raises(ValueError, match="interval_s"):
        await u6_metrics_refresh.run_u6_metrics_refresh_loop(get_pool=lambda: "P", interval_s=0.0)


@pytest.mark.asyncio
async def test_refresh_partial_failure_marks_error_and_preserves(monkeypatch: pytest.MonkeyPatch) -> None:
    # A broken/absent table (fetch raises) must NOT zero its gauge, NOT bump freshness, and NOT abort the other
    # aggregations; the tick is 'error'.  The fetchval-based counts still succeed (0 here).
    metrics.reset_for_tests()
    metrics.u6_grant_count.labels(state="pending").set(42)   # a prior value that must be PRESERVED on failure
    before_fresh = metrics.REGISTRY.get_sample_value("omnisight_u6_metrics_last_success_timestamp_seconds")
    outcome = await u6_metrics_refresh.refresh_u6_metrics_once(_FakePool(_RaisingConn()))
    assert outcome == "error"
    # the by-state fetch failed -> the pre-set value is preserved (not zeroed)
    assert metrics.REGISTRY.get_sample_value("omnisight_u6_grant_count", {"state": "pending"}) == 42
    # the fetchval-based counts still ran and set 0
    assert metrics.REGISTRY.get_sample_value("omnisight_u6_execution_results_count") == 0
    # freshness NOT advanced on a partial failure
    assert metrics.REGISTRY.get_sample_value("omnisight_u6_metrics_last_success_timestamp_seconds") == before_fresh


@pytest.mark.asyncio
async def test_refresh_reflects_the_db_state(pg_test_pool) -> None:
    # On real PG: the refresh gauges must equal an INDEPENDENT aggregation of the same committed rows (read-only; no
    # seeding needed -> isolation-safe).  Proves the DB-derived model is correct + cross-process (a pool connection).
    metrics.reset_for_tests()
    outcome = await u6_metrics_refresh.refresh_u6_metrics_once(pg_test_pool)
    assert outcome == "ok"
    async with pg_test_pool.acquire() as conn:
        for table, gauge_name, states in (
            ("challenges", "omnisight_u6_challenge_count", u6_metrics_refresh._CHALLENGE_STATES),
            ("action_grants", "omnisight_u6_grant_count", u6_metrics_refresh._GRANT_STATES),
            ("resume_jobs", "omnisight_u6_resume_count", u6_metrics_refresh._RESUME_STATES),
        ):
            rows = await conn.fetch(f"SELECT state, count(*) AS n FROM {table} GROUP BY state")
            counts = {r["state"]: int(r["n"]) for r in rows}
            for state in states:
                got = metrics.REGISTRY.get_sample_value(gauge_name, {"state": state})
                assert got == counts.get(state, 0), f"{gauge_name}{{{state}}}: gauge {got} != db {counts.get(state, 0)}"
        for table, gauge_name in (
            ("execution_results", "omnisight_u6_execution_results_count"),
            ("execution_attempts", "omnisight_u6_execution_attempts_count"),
        ):
            n = int(await conn.fetchval(f"SELECT count(*) FROM {table}"))
            assert metrics.REGISTRY.get_sample_value(gauge_name) == n
    assert metrics.REGISTRY.get_sample_value("omnisight_u6_metrics_last_success_timestamp_seconds") > 0
