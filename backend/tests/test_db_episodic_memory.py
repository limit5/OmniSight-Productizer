"""Phase-3-Runtime-v2 SP-3.12 — contract tests for ported episodic_memory
db.py functions + FTS5→tsvector search.

Coverage:
  * Six functions: insert / get / list / delete / count / rebuild_fts.
  * Search via ``tsv @@ plainto_tsquery('english', ...)`` on the STORED
    generated column added in alembic 0017 (SP-2.1).
  * **Search result-set equivalence** (load-bearing): the same corpus
    that returned matches under SQLite FTS5 returns the same match
    set on PG. Ranking ORDER may differ (BM25 → ts_rank drift was
    pre-approved by operator), so tests check SET membership, not
    position.
  * access_count increments on successful search hits.
  * Filter fields (soc_vendor / sdk_version / min_quality) compose.
  * tsvector auto-maintenance: INSERT populates tsv without an
    explicit column write.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
OMNI_TEST_PG_URL).
"""

from __future__ import annotations

import pytest

from backend import db


def _mem(**overrides) -> dict:
    base = {
        "id": "mem-test",
        "error_signature": "kernel panic in driver",
        "solution": "add spinlock guard in irq handler",
        "soc_vendor": "rockchip",
        "sdk_version": "1.2.3",
        "hardware_rev": "rev-A",
        "source_task_id": "t1",
        "source_agent_id": "a1",
        "gerrit_change_id": "I0001",
        "tags": ["kernel", "irq"],
        "quality_score": 0.9,
    }
    base.update(overrides)
    return base


# ─── CRUD ─────────────────────────────────────────────────────────


class TestEpisodicMemoryCrud:
    @pytest.mark.asyncio
    async def test_insert_then_get(self, pg_test_conn) -> None:
        await db.insert_episodic_memory(pg_test_conn, _mem(id="m1"))
        got = await db.get_episodic_memory(pg_test_conn, "m1")
        assert got is not None
        assert got["error_signature"] == "kernel panic in driver"
        assert got["tags"] == ["kernel", "irq"]  # JSON decoded
        # tsv is internal — marshaller strips it from the public dict.
        assert "tsv" not in got

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, pg_test_conn) -> None:
        assert await db.get_episodic_memory(pg_test_conn, "never") is None

    @pytest.mark.asyncio
    async def test_list_ordered_newest_first(self, pg_test_conn) -> None:
        import asyncio
        for i in range(3):
            await db.insert_episodic_memory(pg_test_conn, _mem(
                id=f"m-ord-{i}",
                error_signature=f"unique error {i}",
            ))
            # created_at is clock_timestamp() at second resolution —
            # sleep past the boundary so order is well-defined.
            await asyncio.sleep(1.05)
        rows = await db.list_episodic_memories(pg_test_conn)
        assert [r["id"] for r in rows] == ["m-ord-2", "m-ord-1", "m-ord-0"]

    @pytest.mark.asyncio
    async def test_list_filter_by_vendor(self, pg_test_conn) -> None:
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-rk", soc_vendor="rockchip",
        ))
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-fh", soc_vendor="fullhan",
        ))
        rows = await db.list_episodic_memories(pg_test_conn, soc_vendor="fullhan")
        assert [r["id"] for r in rows] == ["m-fh"]

    @pytest.mark.asyncio
    async def test_delete_existing_returns_true(self, pg_test_conn) -> None:
        await db.insert_episodic_memory(pg_test_conn, _mem(id="m-del"))
        assert await db.delete_episodic_memory(pg_test_conn, "m-del") is True
        assert await db.get_episodic_memory(pg_test_conn, "m-del") is None

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self, pg_test_conn) -> None:
        assert await db.delete_episodic_memory(pg_test_conn, "never") is False

    @pytest.mark.asyncio
    async def test_count_matches_inserts(self, pg_test_conn) -> None:
        assert await db.episodic_memory_count(pg_test_conn) == 0
        for i in range(3):
            await db.insert_episodic_memory(pg_test_conn, _mem(id=f"m-c{i}"))
        assert await db.episodic_memory_count(pg_test_conn) == 3


# ─── tsvector auto-maintenance ───────────────────────────────────


class TestEpisodicMemoryTsvector:
    @pytest.mark.asyncio
    async def test_tsv_is_populated_on_insert(self, pg_test_conn) -> None:
        # The STORED generated column must be non-null for any row
        # with non-null source fields. This is the invariant that lets
        # search work without any explicit FTS maintenance code.
        await db.insert_episodic_memory(pg_test_conn, _mem(id="m-tsv"))
        row = await pg_test_conn.fetchrow(
            "SELECT tsv IS NOT NULL AS has_tsv, "
            "       length(tsv::text) > 0 AS nonempty "
            "FROM episodic_memory WHERE id = $1",
            "m-tsv",
        )
        assert row["has_tsv"] is True
        assert row["nonempty"] is True

    @pytest.mark.asyncio
    async def test_rebuild_episodic_fts_returns_count(
        self, pg_test_conn,
    ) -> None:
        # Post-port contract: rebuild_episodic_fts runs REINDEX on the
        # GIN index and returns the row count. It's an ops hook; the
        # return value is the only observable.
        for i in range(3):
            await db.insert_episodic_memory(pg_test_conn, _mem(id=f"m-rb{i}"))
        n = await db.rebuild_episodic_fts(pg_test_conn)
        assert n == 3


# ─── Search behaviour ───────────────────────────────────────────


class TestEpisodicMemorySearch:
    @pytest.mark.asyncio
    async def test_search_matches_by_error_signature(
        self, pg_test_conn,
    ) -> None:
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-panic",
            error_signature="kernel panic segfault in isp_init",
            solution="order NPU before ISP in probe",
        ))
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-unrelated",
            error_signature="gpio timeout waiting for ack",
            solution="increase i2c bus speed",
        ))
        rows = await db.search_episodic_memory(pg_test_conn, "panic isp")
        ids = {r["id"] for r in rows}
        assert "m-panic" in ids
        assert "m-unrelated" not in ids

    @pytest.mark.asyncio
    async def test_search_matches_by_solution_text(
        self, pg_test_conn,
    ) -> None:
        # tsvector expression covers error_signature + solution +
        # soc_vendor + tags; a query matching the solution alone
        # should still return the row.
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-sol",
            error_signature="generic fail",
            solution="enable CONFIG_ROCKCHIP_NPU in defconfig",
        ))
        rows = await db.search_episodic_memory(pg_test_conn, "defconfig")
        assert {r["id"] for r in rows} == {"m-sol"}

    @pytest.mark.asyncio
    async def test_search_empty_on_no_match(self, pg_test_conn) -> None:
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-nomatch", error_signature="foo", solution="bar",
        ))
        rows = await db.search_episodic_memory(
            pg_test_conn, "unrelated_term_zzzz",
        )
        assert rows == []

    @pytest.mark.asyncio
    async def test_search_respects_vendor_filter(
        self, pg_test_conn,
    ) -> None:
        # Same error signature, different vendors — vendor filter
        # must drop the non-matching row.
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-rk-panic", soc_vendor="rockchip",
            error_signature="kernel panic",
        ))
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-fh-panic", soc_vendor="fullhan",
            error_signature="kernel panic",
        ))
        rows = await db.search_episodic_memory(
            pg_test_conn, "kernel panic", soc_vendor="rockchip",
        )
        assert [r["id"] for r in rows] == ["m-rk-panic"]

    @pytest.mark.asyncio
    async def test_search_respects_sdk_filter(
        self, pg_test_conn,
    ) -> None:
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-sdk-1", sdk_version="1.0",
            error_signature="build error linker",
        ))
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-sdk-2", sdk_version="2.0",
            error_signature="build error linker",
        ))
        rows = await db.search_episodic_memory(
            pg_test_conn, "linker", sdk_version="2.0",
        )
        assert [r["id"] for r in rows] == ["m-sdk-2"]

    @pytest.mark.asyncio
    async def test_search_respects_min_quality(
        self, pg_test_conn,
    ) -> None:
        # Phase 67-E: tier-1 path wants quality gating in SQL.
        # Regression guard for the ``quality_score >=`` predicate.
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-low", quality_score=0.3,
            error_signature="cache miss frequent",
        ))
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-high", quality_score=0.95,
            error_signature="cache miss frequent",
        ))
        rows = await db.search_episodic_memory(
            pg_test_conn, "cache miss", min_quality=0.85,
        )
        assert [r["id"] for r in rows] == ["m-high"]

    @pytest.mark.asyncio
    async def test_search_respects_limit(self, pg_test_conn) -> None:
        for i in range(5):
            await db.insert_episodic_memory(pg_test_conn, _mem(
                id=f"m-lim-{i}",
                error_signature=f"shared token error number {i}",
            ))
        rows = await db.search_episodic_memory(
            pg_test_conn, "shared token", limit=2,
        )
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_search_increments_access_count(
        self, pg_test_conn,
    ) -> None:
        # access_count is how the memory_decay worker decides which
        # memories are "hot" vs "cold". Regression guard: the access
        # counter MUST tick on every search hit, best-effort (a failed
        # UPDATE must not suppress the result).
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-hit", error_signature="unique_counter_probe",
        ))
        row = await pg_test_conn.fetchrow(
            "SELECT access_count FROM episodic_memory WHERE id = $1",
            "m-hit",
        )
        assert row["access_count"] == 0
        await db.search_episodic_memory(pg_test_conn, "unique_counter_probe")
        row = await pg_test_conn.fetchrow(
            "SELECT access_count FROM episodic_memory WHERE id = $1",
            "m-hit",
        )
        assert row["access_count"] == 1

    @pytest.mark.asyncio
    async def test_search_ranks_relevant_higher(
        self, pg_test_conn,
    ) -> None:
        # Ranking drift from BM25 to ts_rank is pre-approved, so this
        # test does NOT enforce exact rank. It only enforces the weaker
        # contract the retrieval path actually depends on: a row whose
        # error_signature matches the query more "strongly" (more
        # token overlap) ranks ABOVE a row that only grazes one token.
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-strong",
            error_signature="gpio i2c bus arbitration timeout error",
            solution="retry with longer timeout",
        ))
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-weak",
            error_signature="unrelated message mentioning arbitration once",
            solution="ignore",
        ))
        rows = await db.search_episodic_memory(
            pg_test_conn, "gpio i2c arbitration timeout",
        )
        assert rows[0]["id"] == "m-strong"
