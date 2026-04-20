"""Phase-3-Runtime-v2 SP-3.8 — contract tests for ported simulations
db.py functions.

Coverage:
  * Four functions: insert_simulation / get_simulation /
    list_simulations / update_simulation.
  * **Whitelist invariant** (load-bearing): update_simulation drops
    any key NOT in _SIMULATION_COLUMNS. Malicious or buggy callers
    cannot write arbitrary columns.
  * Dynamic $N indexing in list_simulations (filter combinations).
  * NPU field round-trip (Phase 36 extension columns).
  * Ordering + limit semantics.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
OMNI_TEST_PG_URL).
"""

from __future__ import annotations

import pytest

from backend import db


def _sim_fixture(**overrides) -> dict:
    base = {
        "id": "sim-test",
        "task_id": "t-test",
        "agent_id": "a-test",
        "track": "algo",
        "module": "isp",
        "status": "running",
        "tests_total": 0,
        "tests_passed": 0,
        "tests_failed": 0,
        "coverage_pct": 0.0,
        "valgrind_errors": 0,
        "duration_ms": 0,
        "report_json": "{}",
        "artifact_id": None,
        "created_at": "2026-04-20T00:00:00",
    }
    base.update(overrides)
    return base


class TestSimulationsCrud:
    @pytest.mark.asyncio
    async def test_insert_then_get(self, pg_test_conn) -> None:
        await db.insert_simulation(pg_test_conn, _sim_fixture(id="sim-1"))
        sim = await db.get_simulation(pg_test_conn, "sim-1")
        assert sim is not None
        assert sim["id"] == "sim-1"
        assert sim["status"] == "running"
        assert sim["track"] == "algo"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, pg_test_conn) -> None:
        assert await db.get_simulation(pg_test_conn, "never") is None

    @pytest.mark.asyncio
    async def test_list_ordered_newest_first(self, pg_test_conn) -> None:
        for i in range(3):
            await db.insert_simulation(pg_test_conn, _sim_fixture(
                id=f"sim-ord-{i}",
                created_at=f"2026-04-20T00:00:0{i}",
            ))
        rows = await db.list_simulations(pg_test_conn)
        assert [r["id"] for r in rows] == [
            "sim-ord-2", "sim-ord-1", "sim-ord-0",
        ]

    @pytest.mark.asyncio
    async def test_list_respects_limit(self, pg_test_conn) -> None:
        for i in range(5):
            await db.insert_simulation(pg_test_conn, _sim_fixture(
                id=f"sim-lim-{i}",
                created_at=f"2026-04-20T00:00:0{i}",
            ))
        rows = await db.list_simulations(pg_test_conn, limit=2)
        assert len(rows) == 2


class TestSimulationsFilters:
    @pytest.mark.asyncio
    async def test_filter_by_task(self, pg_test_conn) -> None:
        await db.insert_simulation(pg_test_conn, _sim_fixture(
            id="sim-tA-1", task_id="t-A",
        ))
        await db.insert_simulation(pg_test_conn, _sim_fixture(
            id="sim-tB-1", task_id="t-B",
        ))
        rows = await db.list_simulations(pg_test_conn, task_id="t-A")
        assert [r["id"] for r in rows] == ["sim-tA-1"]

    @pytest.mark.asyncio
    async def test_filter_by_agent(self, pg_test_conn) -> None:
        await db.insert_simulation(pg_test_conn, _sim_fixture(
            id="sim-aX", agent_id="a-X",
        ))
        await db.insert_simulation(pg_test_conn, _sim_fixture(
            id="sim-aY", agent_id="a-Y",
        ))
        rows = await db.list_simulations(pg_test_conn, agent_id="a-Y")
        assert [r["id"] for r in rows] == ["sim-aY"]

    @pytest.mark.asyncio
    async def test_filter_by_status(self, pg_test_conn) -> None:
        await db.insert_simulation(pg_test_conn, _sim_fixture(
            id="sim-pass", status="passed",
        ))
        await db.insert_simulation(pg_test_conn, _sim_fixture(
            id="sim-fail", status="failed",
        ))
        rows = await db.list_simulations(pg_test_conn, status="failed")
        assert [r["id"] for r in rows] == ["sim-fail"]

    @pytest.mark.asyncio
    async def test_multiple_filters_combine_with_and(
        self, pg_test_conn,
    ) -> None:
        # Three active filters → 3 $N placeholders for filters + 1
        # for LIMIT. The port's dynamic $N indexing had to get this
        # right — regression guard.
        await db.insert_simulation(pg_test_conn, _sim_fixture(
            id="sim-match", task_id="t-1", agent_id="a-1",
            status="passed",
        ))
        await db.insert_simulation(pg_test_conn, _sim_fixture(
            id="sim-wrong-task", task_id="t-2", agent_id="a-1",
            status="passed",
        ))
        await db.insert_simulation(pg_test_conn, _sim_fixture(
            id="sim-wrong-status", task_id="t-1", agent_id="a-1",
            status="failed",
        ))
        rows = await db.list_simulations(
            pg_test_conn, task_id="t-1", agent_id="a-1", status="passed",
        )
        assert [r["id"] for r in rows] == ["sim-match"]


class TestSimulationsUpdate:
    @pytest.mark.asyncio
    async def test_update_happy_path(self, pg_test_conn) -> None:
        await db.insert_simulation(pg_test_conn, _sim_fixture(id="sim-u"))
        await db.update_simulation(pg_test_conn, "sim-u", {
            "status": "passed", "tests_passed": 10, "tests_failed": 0,
        })
        sim = await db.get_simulation(pg_test_conn, "sim-u")
        assert sim["status"] == "passed"
        assert sim["tests_passed"] == 10
        assert sim["tests_failed"] == 0

    @pytest.mark.asyncio
    async def test_update_whitelist_drops_unknown_columns(
        self, pg_test_conn,
    ) -> None:
        # Load-bearing safety test: keys outside _SIMULATION_COLUMNS
        # are silently dropped by the dict comprehension. Any refactor
        # that removes the whitelist is a potential injection vector.
        await db.insert_simulation(pg_test_conn, _sim_fixture(id="sim-wl"))
        await db.update_simulation(pg_test_conn, "sim-wl", {
            "status": "passed",
            "bogus_column": "should be ignored",
            "id": "forged-id",  # MUST NOT rewrite the PK
        })
        sim = await db.get_simulation(pg_test_conn, "sim-wl")
        assert sim is not None
        assert sim["status"] == "passed"
        assert sim["id"] == "sim-wl"  # PK unchanged
        # Forged id didn't create a new row either
        assert await db.get_simulation(pg_test_conn, "forged-id") is None

    @pytest.mark.asyncio
    async def test_update_empty_dict_noop(self, pg_test_conn) -> None:
        await db.insert_simulation(pg_test_conn, _sim_fixture(id="sim-empty"))
        await db.update_simulation(pg_test_conn, "sim-empty", {})
        sim = await db.get_simulation(pg_test_conn, "sim-empty")
        assert sim["status"] == "running"  # unchanged

    @pytest.mark.asyncio
    async def test_update_all_unknown_keys_noop(self, pg_test_conn) -> None:
        await db.insert_simulation(pg_test_conn, _sim_fixture(id="sim-bogus"))
        # If every key is unwhitelisted, the function should return
        # early WITHOUT emitting an empty SET clause that PG would
        # reject with a syntax error.
        await db.update_simulation(pg_test_conn, "sim-bogus", {
            "bogus1": 1, "bogus2": 2,
        })
        sim = await db.get_simulation(pg_test_conn, "sim-bogus")
        assert sim["status"] == "running"

    @pytest.mark.asyncio
    async def test_update_npu_fields_round_trip(self, pg_test_conn) -> None:
        # Phase 36 NPU columns: locked into the whitelist. Regression
        # guard against removing them on a cleanup refactor.
        await db.insert_simulation(pg_test_conn, _sim_fixture(
            id="sim-npu", track="npu",
        ))
        await db.update_simulation(pg_test_conn, "sim-npu", {
            "status": "passed",
            "npu_latency_ms": 12.5,
            "npu_throughput_fps": 80.0,
            "accuracy_delta": -0.5,
            "model_size_kb": 2048,
            "npu_framework": "rknn",
        })
        sim = await db.get_simulation(pg_test_conn, "sim-npu")
        assert sim["npu_latency_ms"] == pytest.approx(12.5)
        assert sim["npu_throughput_fps"] == pytest.approx(80.0)
        assert sim["accuracy_delta"] == pytest.approx(-0.5)
        assert sim["model_size_kb"] == 2048
        assert sim["npu_framework"] == "rknn"


class TestSimulationsErrorPaths:
    @pytest.mark.asyncio
    async def test_insert_missing_required_key_raises(
        self, pg_test_conn,
    ) -> None:
        # Track + module are NOT NULL; insert_simulation KeyErrors in
        # Python before the DB sees the malformed INSERT.
        with pytest.raises(KeyError):
            await db.insert_simulation(pg_test_conn, {
                "id": "sim-bad", "task_id": "t", "agent_id": "a",
                # missing "track" and "module"
            })

    @pytest.mark.asyncio
    async def test_insert_rejects_null_track(self, pg_test_conn) -> None:
        import asyncpg
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            await db.insert_simulation(pg_test_conn, _sim_fixture(
                id="sim-null-track", track=None,
            ))
