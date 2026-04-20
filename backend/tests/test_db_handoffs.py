"""Phase-3-Runtime-v2 SP-3.3 — contract tests for ported handoff
db.py functions.

Replaces the SQLite-backed ``test_handoff_upsert_get_list`` in
``test_db.py`` (moved out because the ported signatures require
asyncpg + pool — see that file's header comment for rationale).

Coverage:
  * Three functions: upsert_handoff / get_handoff / list_handoffs —
    happy path + empty state + UPSERT replace-on-conflict semantics
    + ORDER BY created_at DESC ordering + missing-key empty-string
    return contract.
  * Error paths: NULL violations at the DB level, concurrent upserts
    on distinct task_ids.
  * Row marshalling: asyncpg.Record returns (task_id, agent_id,
    created_at) — the shape routers/tasks.py::get_task_handoffs +
    get_recent_handoffs consume.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
OMNI_TEST_PG_URL).
"""

from __future__ import annotations

import pytest

from backend import db


# ─── Happy path: upsert + get + list round-trip ──────────────────


class TestHandoffsUpsert:
    @pytest.mark.asyncio
    async def test_upsert_then_get(self, pg_test_conn) -> None:
        await db.upsert_handoff(pg_test_conn, "t1", "agent-a", "first handoff")
        got = await db.get_handoff(pg_test_conn, "t1")
        assert got == "first handoff"

    @pytest.mark.asyncio
    async def test_upsert_replaces_on_conflict(self, pg_test_conn) -> None:
        # PK is task_id → second upsert on same task_id wins (agent_id
        # + content both replaced). This is the ON CONFLICT (task_id)
        # DO UPDATE contract.
        await db.upsert_handoff(pg_test_conn, "t1", "agent-a", "original")
        await db.upsert_handoff(pg_test_conn, "t1", "agent-b", "revised")
        assert await db.get_handoff(pg_test_conn, "t1") == "revised"
        rows = await db.list_handoffs(pg_test_conn)
        assert len(rows) == 1
        assert rows[0]["agent_id"] == "agent-b"

    @pytest.mark.asyncio
    async def test_get_missing_returns_empty_string(
        self, pg_test_conn,
    ) -> None:
        # The return contract for callers (notably
        # handoff.load_handoff_for_task) is "" on missing, not None —
        # callers use ``if content:`` to branch. Regression guard.
        assert await db.get_handoff(pg_test_conn, "nonexistent") == ""


# ─── List ordering + shape ───────────────────────────────────────


class TestHandoffsList:
    @pytest.mark.asyncio
    async def test_list_empty_state(self, pg_test_conn) -> None:
        # pg_test_conn truncates handoffs inside the outer tx, so this
        # test always starts clean regardless of committed pollution.
        rows = await db.list_handoffs(pg_test_conn)
        assert rows == []

    @pytest.mark.asyncio
    async def test_list_returns_newest_first(self, pg_test_conn) -> None:
        # Ordering contract: ORDER BY created_at DESC. Insert three
        # handoffs on DIFFERENT task_ids with a small sleep between so
        # their timestamps differ at second resolution (the
        # to_char('YYYY-MM-DD HH24:MI:SS') default truncates to
        # seconds). Without distinct timestamps the ordering is
        # implementation-defined — the real-world workload has no
        # sub-second handoffs so this matches production behaviour.
        import asyncio
        for i in range(3):
            await db.upsert_handoff(
                pg_test_conn, f"t-ord-{i}", f"agent-{i}",
                f"content {i}",
            )
            await asyncio.sleep(1.05)
        rows = await db.list_handoffs(pg_test_conn)
        assert len(rows) == 3
        assert rows[0]["task_id"] == "t-ord-2"
        assert rows[-1]["task_id"] == "t-ord-0"

    @pytest.mark.asyncio
    async def test_list_shape_matches_router_expectation(
        self, pg_test_conn,
    ) -> None:
        # routers/tasks.py::get_task_handoffs does
        # ``[h for h in all_handoffs if h.get("task_id") == task_id]``
        # — which requires the ``task_id`` key to be present on each
        # row. Regression guard against someone dropping columns from
        # the SELECT list.
        await db.upsert_handoff(pg_test_conn, "t-shape", "agent-x", "hi")
        rows = await db.list_handoffs(pg_test_conn)
        assert len(rows) == 1
        row = rows[0]
        assert {"task_id", "agent_id", "created_at"} <= set(row.keys())
        assert row["task_id"] == "t-shape"
        assert row["agent_id"] == "agent-x"


# ─── Update-created_at-on-conflict contract ──────────────────────


class TestHandoffsCreatedAt:
    @pytest.mark.asyncio
    async def test_upsert_bumps_created_at_on_conflict(
        self, pg_test_conn,
    ) -> None:
        # The schema's UPDATE SET clause on CONFLICT also sets
        # ``created_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS')``.
        # Semantically ``handoffs.created_at`` behaves as "last-
        # written-at" rather than "first-created-at" — callers that
        # build timelines depend on this. Lock the contract so a later
        # refactor that drops the SET can't silently break ordering.
        import asyncio
        await db.upsert_handoff(pg_test_conn, "t-bump", "agent-a", "first")
        rows_before = await db.list_handoffs(pg_test_conn)
        ts_before = rows_before[0]["created_at"]
        await asyncio.sleep(1.05)
        await db.upsert_handoff(pg_test_conn, "t-bump", "agent-a", "second")
        rows_after = await db.list_handoffs(pg_test_conn)
        ts_after = rows_after[0]["created_at"]
        assert ts_after > ts_before, (
            f"Expected created_at to advance after upsert "
            f"({ts_before!r} → {ts_after!r})"
        )


# ─── Concurrency + error paths ───────────────────────────────────


class TestHandoffsConcurrency:
    @pytest.mark.asyncio
    async def test_parallel_upserts_distinct_task_ids(
        self, pg_test_pool,
    ) -> None:
        # Each worker borrows its own pool conn and upserts a unique
        # task_id. Would serialise through the compat wrapper's
        # asyncio.Lock; runs in parallel here. Matches the pattern in
        # test_db_agents.py / test_db_tasks.py.
        import asyncio

        async def _worker(task_id: str) -> None:
            async with pg_test_pool.acquire() as conn:
                await db.upsert_handoff(
                    conn, task_id, f"agent-{task_id}", f"content {task_id}",
                )

        await asyncio.gather(*[_worker(f"h-{i}") for i in range(5)])
        async with pg_test_pool.acquire() as conn:
            all_rows = await db.list_handoffs(conn)
            got_ids = {r["task_id"] for r in all_rows}
            expected = {f"h-{i}" for i in range(5)}
            assert expected.issubset(got_ids), (
                f"Missing concurrent upserts: {expected - got_ids}"
            )
            # Cleanup — pg_test_pool commits, so leftover rows would
            # leak into sibling tests that use the same pool.
            for tid in expected:
                await conn.execute(
                    "DELETE FROM handoffs WHERE task_id = $1", tid,
                )


class TestHandoffsErrorPaths:
    @pytest.mark.asyncio
    async def test_upsert_rejects_null_agent_id(self, pg_test_conn) -> None:
        # Schema has agent_id NOT NULL — explicit None should surface
        # as PG NotNullViolationError rather than silently corrupt.
        import asyncpg
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            await db.upsert_handoff(pg_test_conn, "t-null", None, "x")

    @pytest.mark.asyncio
    async def test_upsert_rejects_null_content(self, pg_test_conn) -> None:
        import asyncpg
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            await db.upsert_handoff(pg_test_conn, "t-null2", "agent-a", None)
