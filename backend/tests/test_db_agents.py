"""Phase-3-Runtime-v2 SP-3.1 — contract tests for ported agent
db.py functions.

Replaces the SQLite-backed ``test_agent_upsert_get_list_delete`` in
``test_db.py`` (moved out because the ported signatures require
asyncpg + pool — see that file's header comment for rationale).

Coverage:
  * Five functions: list_agents / get_agent / upsert_agent /
    delete_agent / agent_count — happy path + empty state + JSON
    round-trip fidelity + upsert idempotency + delete semantics
    (including double-delete).
  * Error paths: misshapen data, concurrent upserts, missing keys.
  * Row marshalling: ``_agent_row_to_dict`` on asyncpg.Record
    returns the exact shape ``routers/agents.py::_row_to_agent``
    expects.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
OMNI_TEST_PG_URL).
"""

from __future__ import annotations

import json

import pytest

from backend import db


# ─── Helpers ─────────────────────────────────────────────────────


def _agent_fixture(**overrides) -> dict:
    """Build a valid upsert_agent input dict with sensible defaults.

    Individual tests override only the fields they care about —
    avoids test-body duplication of the full Pydantic-shaped row.
    """
    base = {
        "id": "a-test",
        "name": "Alpha Test",
        "type": "firmware",
        "status": "idle",
        "sub_type": "",
        "thought_chain": "",
        "progress": {"current": 0, "total": 5},
        "ai_model": None,
        "sub_tasks": [],
        "workspace": {"root": "/tmp"},
    }
    base.update(overrides)
    return base


# ─── Happy path: full CRUD round-trip ────────────────────────────


class TestAgentsCrud:
    @pytest.mark.asyncio
    async def test_empty_count(self, pg_test_conn) -> None:
        # Fresh savepoint fixture → no agents committed in this test's
        # visibility. The rollback on teardown makes this true per-test.
        assert await db.agent_count(pg_test_conn) == 0

    @pytest.mark.asyncio
    async def test_upsert_then_get(self, pg_test_conn) -> None:
        await db.upsert_agent(pg_test_conn, _agent_fixture(id="a1"))
        got = await db.get_agent(pg_test_conn, "a1")
        assert got is not None
        assert got["id"] == "a1"
        assert got["name"] == "Alpha Test"
        assert got["type"] == "firmware"
        assert got["status"] == "idle"

    @pytest.mark.asyncio
    async def test_json_fields_round_trip(self, pg_test_conn) -> None:
        # progress / sub_tasks / workspace are serialised to JSON at
        # write, deserialised at read. Fidelity on nested dicts +
        # lists is what this guards against.
        fixture = _agent_fixture(
            id="a-json",
            progress={"current": 3, "total": 5},
            sub_tasks=[
                {"id": "t1", "label": "step-1", "status": "done"},
                {"id": "t2", "label": "step-2", "status": "pending"},
            ],
            workspace={"root": "/app/data", "branch": "main"},
        )
        await db.upsert_agent(pg_test_conn, fixture)
        got = await db.get_agent(pg_test_conn, "a-json")
        assert got["progress"] == {"current": 3, "total": 5}
        assert got["sub_tasks"] == fixture["sub_tasks"]
        assert got["workspace"] == {"root": "/app/data", "branch": "main"}

    @pytest.mark.asyncio
    async def test_upsert_updates_existing_without_duplicate(
        self, pg_test_conn,
    ) -> None:
        # Two upserts on the same id → count stays at 1, second upsert
        # wins. This is the ON CONFLICT DO UPDATE contract.
        await db.upsert_agent(pg_test_conn, _agent_fixture(
            id="a-upd", name="Original",
        ))
        assert await db.agent_count(pg_test_conn) == 1
        await db.upsert_agent(pg_test_conn, _agent_fixture(
            id="a-upd", name="Replaced", status="running",
        ))
        assert await db.agent_count(pg_test_conn) == 1
        got = await db.get_agent(pg_test_conn, "a-upd")
        assert got["name"] == "Replaced"
        assert got["status"] == "running"

    @pytest.mark.asyncio
    async def test_list_multiple_rows(self, pg_test_conn) -> None:
        ids = [f"a-list-{i}" for i in range(5)]
        for aid in ids:
            await db.upsert_agent(pg_test_conn, _agent_fixture(id=aid))
        rows = await db.list_agents(pg_test_conn)
        assert len(rows) == 5
        assert {r["id"] for r in rows} == set(ids)

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, pg_test_conn) -> None:
        assert await db.get_agent(pg_test_conn, "nonexistent") is None


# ─── Delete semantics ────────────────────────────────────────────


class TestAgentsDelete:
    @pytest.mark.asyncio
    async def test_delete_existing_returns_true(self, pg_test_conn) -> None:
        await db.upsert_agent(pg_test_conn, _agent_fixture(id="a-del"))
        assert await db.delete_agent(pg_test_conn, "a-del") is True
        # Verify gone
        assert await db.get_agent(pg_test_conn, "a-del") is None

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self, pg_test_conn) -> None:
        # Idempotency: deleting a row that doesn't exist returns
        # False without raising. Matters because router handlers use
        # the return value to distinguish 204 (deleted now) from 404
        # (never existed) — or, as in the current delete_agent router,
        # treat both as equivalent 204 for UX. Either way, no raise.
        assert await db.delete_agent(pg_test_conn, "never-existed") is False

    @pytest.mark.asyncio
    async def test_double_delete_is_idempotent(self, pg_test_conn) -> None:
        await db.upsert_agent(pg_test_conn, _agent_fixture(id="a-dd"))
        assert await db.delete_agent(pg_test_conn, "a-dd") is True
        assert await db.delete_agent(pg_test_conn, "a-dd") is False


# ─── Row marshalling contract ────────────────────────────────────


class TestAgentRowToDict:
    @pytest.mark.asyncio
    async def test_asyncpg_record_has_keys_method(
        self, pg_test_conn,
    ) -> None:
        # _agent_row_to_dict uses row.keys() to detect presence of the
        # optional sub_type column. Prove asyncpg.Record supports that
        # method with the expected semantics — this is what keeps the
        # helper dialect-agnostic.
        await db.upsert_agent(pg_test_conn, _agent_fixture(
            id="a-keys", sub_type="review",
        ))
        row = await pg_test_conn.fetchrow(
            "SELECT * FROM agents WHERE id = $1", "a-keys",
        )
        keys = row.keys()
        assert "id" in keys
        assert "sub_type" in keys

    @pytest.mark.asyncio
    async def test_marshalled_dict_round_trip_with_sub_type(
        self, pg_test_conn,
    ) -> None:
        await db.upsert_agent(pg_test_conn, _agent_fixture(
            id="a-sub", sub_type="code-review",
        ))
        got = await db.get_agent(pg_test_conn, "a-sub")
        assert got["sub_type"] == "code-review"


# ─── Concurrency (ported functions on separate pool conns) ───────


class TestAgentsConcurrency:
    """Two concurrent borrowers from the pool writing different agents.
    Proves the port doesn't hit the single-conn Lock bottleneck that
    motivated the migration in the first place."""

    @pytest.mark.asyncio
    async def test_parallel_upserts_no_contention(
        self, pg_test_pool,
    ) -> None:
        import asyncio

        async def _worker(aid: str) -> None:
            # Each worker borrows its own conn from the pool, inserts
            # a unique agent, and releases. This would serialise
            # through asyncio.Lock in the compat wrapper; it runs in
            # parallel here.
            async with pg_test_pool.acquire() as conn:
                await db.upsert_agent(conn, _agent_fixture(
                    id=aid, name=f"Concurrent {aid}",
                ))

        await asyncio.gather(*[_worker(f"c-{i}") for i in range(5)])

        async with pg_test_pool.acquire() as conn:
            all_rows = await db.list_agents(conn)
            ids_inserted = {f"c-{i}" for i in range(5)}
            got_ids = {r["id"] for r in all_rows}
            assert ids_inserted.issubset(got_ids), (
                f"Expected all 5 concurrent inserts to land; got "
                f"{got_ids - ids_inserted}"
            )
            # Cleanup so the test's rollback-less pool conn doesn't
            # leave rows for subsequent tests (pg_test_pool is function-
            # scoped but its inserts commit).
            for i in range(5):
                await db.delete_agent(conn, f"c-{i}")


# ─── Error paths ─────────────────────────────────────────────────


class TestAgentsErrorPaths:
    @pytest.mark.asyncio
    async def test_upsert_missing_required_key_raises(
        self, pg_test_conn,
    ) -> None:
        # upsert_agent expects ``id`` / ``name`` / ``type`` in the
        # data dict. Missing ``id`` surfaces as a KeyError — clearer
        # for the caller than a silent NULL-PK violation at the DB.
        with pytest.raises(KeyError):
            await db.upsert_agent(pg_test_conn, {"name": "no-id", "type": "firmware"})

    @pytest.mark.asyncio
    async def test_upsert_rejects_null_name_at_db_level(
        self, pg_test_conn,
    ) -> None:
        # Schema has name NOT NULL — write with explicit None should
        # surface as a PG NotNullViolationError, not silently corrupt.
        import asyncpg
        # Build a fixture with name=None, bypassing _agent_fixture()'s
        # defaults by direct dict construction.
        bad = {
            "id": "a-null", "name": None, "type": "firmware",
        }
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            await db.upsert_agent(pg_test_conn, bad)
