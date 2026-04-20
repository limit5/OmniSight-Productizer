"""Phase-3-Runtime-v2 SP-3.2 — contract tests for ported task
db.py functions.

Replaces the SQLite-backed ``test_task_upsert_get_list_delete`` and
``test_task_comments_insert_list`` in ``test_db.py`` (moved out because
the ported signatures require asyncpg + pool — see that file's header
comment for rationale).

Coverage:
  * Seven functions: list_tasks / get_task / upsert_task / delete_task
    / task_count / insert_task_comment / list_task_comments —
    happy path + empty state + JSON round-trip fidelity + upsert
    idempotency + delete semantics (including double-delete).
  * Error paths: misshapen data, concurrent upserts, missing keys.
  * Row marshalling: ``_task_row_to_dict`` on asyncpg.Record returns
    the exact shape routers/tasks.py handlers expect (JSON fields
    decoded, defaults present).
  * Comment ordering (ORDER BY timestamp DESC) + pagination limit.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
OMNI_TEST_PG_URL).
"""

from __future__ import annotations

import pytest

from backend import db


# ─── Helpers ─────────────────────────────────────────────────────


def _task_fixture(**overrides) -> dict:
    """Build a valid upsert_task input dict with sensible defaults.

    Individual tests override only the fields they care about.
    """
    base = {
        "id": "t-test",
        "title": "Alpha Task",
        "description": "fixture desc",
        "priority": "medium",
        "status": "backlog",
        "assigned_agent_id": None,
        "created_at": "2026-04-20T00:00:00",
        "completed_at": None,
        "ai_analysis": None,
        "suggested_agent_type": None,
        "suggested_sub_type": None,
        "parent_task_id": None,
        "child_task_ids": [],
        "external_issue_id": None,
        "issue_url": None,
        "acceptance_criteria": None,
        "labels": [],
        "depends_on": [],
        "external_issue_platform": None,
        "last_external_sync_at": None,
        "npi_phase_id": None,
    }
    base.update(overrides)
    return base


def _comment_fixture(**overrides) -> dict:
    base = {
        "id": "c-test",
        "task_id": "t-test",
        "author": "user",
        "content": "hello",
        "timestamp": "2026-04-20T00:00:00",
    }
    base.update(overrides)
    return base


# ─── Happy path: full CRUD round-trip ────────────────────────────


class TestTasksCrud:
    @pytest.mark.asyncio
    async def test_empty_count(self, pg_test_conn) -> None:
        # Fresh savepoint fixture → no tasks committed in this test's
        # visibility. The rollback on teardown makes this true per-test.
        assert await db.task_count(pg_test_conn) == 0

    @pytest.mark.asyncio
    async def test_upsert_then_get(self, pg_test_conn) -> None:
        await db.upsert_task(pg_test_conn, _task_fixture(id="t1", title="Build driver"))
        got = await db.get_task(pg_test_conn, "t1")
        assert got is not None
        assert got["id"] == "t1"
        assert got["title"] == "Build driver"
        assert got["priority"] == "medium"
        assert got["status"] == "backlog"

    @pytest.mark.asyncio
    async def test_json_fields_round_trip(self, pg_test_conn) -> None:
        # labels / child_task_ids / depends_on are serialised to JSON
        # at write, deserialised at read. Fidelity on nested lists is
        # what this guards against.
        fixture = _task_fixture(
            id="t-json",
            labels=["firmware", "urgent", "p0"],
            depends_on=["t-prev-1", "t-prev-2"],
            child_task_ids=["t-sub-1", "t-sub-2", "t-sub-3"],
        )
        await db.upsert_task(pg_test_conn, fixture)
        got = await db.get_task(pg_test_conn, "t-json")
        assert got["labels"] == ["firmware", "urgent", "p0"]
        assert got["depends_on"] == ["t-prev-1", "t-prev-2"]
        assert got["child_task_ids"] == ["t-sub-1", "t-sub-2", "t-sub-3"]

    @pytest.mark.asyncio
    async def test_json_defaults_when_omitted(self, pg_test_conn) -> None:
        # Minimum-shape upsert — only id/title supplied, everything
        # else falls through to defaults. labels / child_task_ids /
        # depends_on should come back as empty lists (JSON-decoded
        # from the 'json.dumps([])' stored at write time).
        await db.upsert_task(pg_test_conn, {
            "id": "t-min", "title": "Minimal",
        })
        got = await db.get_task(pg_test_conn, "t-min")
        assert got is not None
        assert got["labels"] == []
        assert got["child_task_ids"] == []
        assert got["depends_on"] == []
        assert got["priority"] == "medium"
        assert got["status"] == "backlog"

    @pytest.mark.asyncio
    async def test_upsert_updates_existing_without_duplicate(
        self, pg_test_conn,
    ) -> None:
        # Two upserts on the same id → count stays at 1, second upsert
        # wins. This is the ON CONFLICT DO UPDATE contract.
        await db.upsert_task(pg_test_conn, _task_fixture(
            id="t-upd", title="Original", priority="low",
        ))
        assert await db.task_count(pg_test_conn) == 1
        await db.upsert_task(pg_test_conn, _task_fixture(
            id="t-upd", title="Replaced", priority="high", status="in_progress",
        ))
        assert await db.task_count(pg_test_conn) == 1
        got = await db.get_task(pg_test_conn, "t-upd")
        assert got["title"] == "Replaced"
        assert got["priority"] == "high"
        assert got["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_list_multiple_rows(self, pg_test_conn) -> None:
        ids = [f"t-list-{i}" for i in range(5)]
        for tid in ids:
            await db.upsert_task(pg_test_conn, _task_fixture(id=tid))
        rows = await db.list_tasks(pg_test_conn)
        assert len(rows) == 5
        assert {r["id"] for r in rows} == set(ids)

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, pg_test_conn) -> None:
        assert await db.get_task(pg_test_conn, "nonexistent") is None

    @pytest.mark.asyncio
    async def test_external_issue_platform_round_trip(self, pg_test_conn) -> None:
        # Regression guard for the schema column test_schema.py used to
        # exercise against the compat wrapper.
        await db.upsert_task(pg_test_conn, _task_fixture(
            id="t-ext",
            external_issue_platform="github",
            last_external_sync_at="2026-04-13T00:00:00",
        ))
        got = await db.get_task(pg_test_conn, "t-ext")
        assert got["external_issue_platform"] == "github"
        assert got["last_external_sync_at"] == "2026-04-13T00:00:00"


# ─── Delete semantics ────────────────────────────────────────────


class TestTasksDelete:
    @pytest.mark.asyncio
    async def test_delete_existing_returns_true(self, pg_test_conn) -> None:
        await db.upsert_task(pg_test_conn, _task_fixture(id="t-del"))
        assert await db.delete_task(pg_test_conn, "t-del") is True
        assert await db.get_task(pg_test_conn, "t-del") is None

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self, pg_test_conn) -> None:
        # Idempotency: deleting a row that doesn't exist returns
        # False without raising. Matches delete_agent semantics — the
        # router delete_task handler catches 404 at the memory layer
        # before ever hitting this function.
        assert await db.delete_task(pg_test_conn, "never-existed") is False

    @pytest.mark.asyncio
    async def test_double_delete_is_idempotent(self, pg_test_conn) -> None:
        await db.upsert_task(pg_test_conn, _task_fixture(id="t-dd"))
        assert await db.delete_task(pg_test_conn, "t-dd") is True
        assert await db.delete_task(pg_test_conn, "t-dd") is False


# ─── Task comments ───────────────────────────────────────────────


class TestTaskComments:
    @pytest.mark.asyncio
    async def test_insert_single_comment(self, pg_test_conn) -> None:
        await db.upsert_task(pg_test_conn, _task_fixture(id="t-c1"))
        await db.insert_task_comment(pg_test_conn, _comment_fixture(
            id="c1", task_id="t-c1", content="first",
        ))
        rows = await db.list_task_comments(pg_test_conn, "t-c1")
        assert len(rows) == 1
        assert rows[0]["content"] == "first"

    @pytest.mark.asyncio
    async def test_list_orders_by_timestamp_desc(self, pg_test_conn) -> None:
        # Ordering contract: newest first. The router (routers/tasks.py
        # get_task_comments) returns the list as-is and the frontend
        # expects recent-first. Regression guard against someone
        # removing the ORDER BY.
        await db.upsert_task(pg_test_conn, _task_fixture(id="t-order"))
        for i in range(3):
            await db.insert_task_comment(pg_test_conn, _comment_fixture(
                id=f"c-ord-{i}", task_id="t-order",
                content=f"comment {i}",
                timestamp=f"2026-04-20T00:00:0{i}",
            ))
        rows = await db.list_task_comments(pg_test_conn, "t-order")
        assert len(rows) == 3
        assert rows[0]["content"] == "comment 2"
        assert rows[-1]["content"] == "comment 0"

    @pytest.mark.asyncio
    async def test_list_respects_limit(self, pg_test_conn) -> None:
        await db.upsert_task(pg_test_conn, _task_fixture(id="t-lim"))
        for i in range(5):
            await db.insert_task_comment(pg_test_conn, _comment_fixture(
                id=f"c-lim-{i}", task_id="t-lim",
                content=f"c{i}",
                timestamp=f"2026-04-20T00:00:0{i}",
            ))
        rows = await db.list_task_comments(pg_test_conn, "t-lim", limit=2)
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_list_scopes_by_task_id(self, pg_test_conn) -> None:
        # Comments are task-scoped; a query for task A must not return
        # comments belonging to task B even if inserted back-to-back.
        await db.upsert_task(pg_test_conn, _task_fixture(id="t-a"))
        await db.upsert_task(pg_test_conn, _task_fixture(id="t-b"))
        await db.insert_task_comment(pg_test_conn, _comment_fixture(
            id="c-a", task_id="t-a", content="on A",
            timestamp="2026-04-20T00:00:00",
        ))
        await db.insert_task_comment(pg_test_conn, _comment_fixture(
            id="c-b", task_id="t-b", content="on B",
            timestamp="2026-04-20T00:00:01",
        ))
        rows_a = await db.list_task_comments(pg_test_conn, "t-a")
        rows_b = await db.list_task_comments(pg_test_conn, "t-b")
        assert len(rows_a) == 1 and rows_a[0]["content"] == "on A"
        assert len(rows_b) == 1 and rows_b[0]["content"] == "on B"


# ─── Row marshalling contract ────────────────────────────────────


class TestTaskRowToDict:
    @pytest.mark.asyncio
    async def test_marshalled_dict_has_all_schema_columns(
        self, pg_test_conn,
    ) -> None:
        # The router returns the raw dict through a Task Pydantic
        # model; the model requires specific keys to be present. If a
        # migration drops/renames a column, this test is where it
        # surfaces — much earlier than in HTTP response validation.
        await db.upsert_task(pg_test_conn, _task_fixture(id="t-keys"))
        got = await db.get_task(pg_test_conn, "t-keys")
        assert got is not None
        required_keys = {
            "id", "title", "description", "priority", "status",
            "assigned_agent_id", "created_at", "completed_at",
            "ai_analysis", "suggested_agent_type", "suggested_sub_type",
            "parent_task_id", "child_task_ids", "external_issue_id",
            "issue_url", "acceptance_criteria", "labels", "depends_on",
            "external_issue_platform", "last_external_sync_at",
            "npi_phase_id",
        }
        missing = required_keys - set(got.keys())
        assert not missing, f"Schema drift: missing keys {missing}"


# ─── Concurrency (ported functions on separate pool conns) ───────


class TestTasksConcurrency:
    """Two concurrent borrowers from the pool writing different tasks.
    Proves the port doesn't hit the single-conn Lock bottleneck that
    motivated the migration in the first place."""

    @pytest.mark.asyncio
    async def test_parallel_upserts_no_contention(
        self, pg_test_pool,
    ) -> None:
        import asyncio

        async def _worker(tid: str) -> None:
            # Each worker borrows its own conn from the pool, inserts a
            # unique task, and releases. This would serialise through
            # asyncio.Lock in the compat wrapper; it runs in parallel
            # here.
            async with pg_test_pool.acquire() as conn:
                await db.upsert_task(conn, _task_fixture(
                    id=tid, title=f"Concurrent {tid}",
                ))

        await asyncio.gather(*[_worker(f"tc-{i}") for i in range(5)])

        async with pg_test_pool.acquire() as conn:
            all_rows = await db.list_tasks(conn)
            ids_inserted = {f"tc-{i}" for i in range(5)}
            got_ids = {r["id"] for r in all_rows}
            assert ids_inserted.issubset(got_ids), (
                f"Expected all 5 concurrent inserts to land; got "
                f"{got_ids - ids_inserted}"
            )
            # Cleanup — pg_test_pool is function-scoped but its inserts
            # commit (no savepoint wrapper).
            for i in range(5):
                await db.delete_task(conn, f"tc-{i}")


# ─── Error paths ─────────────────────────────────────────────────


class TestTasksErrorPaths:
    @pytest.mark.asyncio
    async def test_upsert_missing_required_key_raises(
        self, pg_test_conn,
    ) -> None:
        # upsert_task expects ``id`` / ``title`` in the data dict.
        # Missing ``id`` surfaces as a KeyError — clearer for the
        # caller than a silent NULL-PK violation at the DB.
        with pytest.raises(KeyError):
            await db.upsert_task(pg_test_conn, {"title": "no-id"})

    @pytest.mark.asyncio
    async def test_upsert_rejects_null_title_at_db_level(
        self, pg_test_conn,
    ) -> None:
        # Schema has title NOT NULL — write with explicit None should
        # surface as a PG NotNullViolationError, not silently corrupt.
        import asyncpg
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            await db.upsert_task(pg_test_conn, {
                "id": "t-null", "title": None,
            })

    @pytest.mark.asyncio
    async def test_insert_comment_missing_key_raises(
        self, pg_test_conn,
    ) -> None:
        # insert_task_comment expects five keys; missing any should
        # raise KeyError in Python before the DB sees it.
        with pytest.raises(KeyError):
            await db.insert_task_comment(pg_test_conn, {
                "id": "c-bad", "task_id": "t1",
                # missing: author, content, timestamp
            })
