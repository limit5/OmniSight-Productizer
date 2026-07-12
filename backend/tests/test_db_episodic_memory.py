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

U6-0 T8-A (OP-2610) additions — mind the two lanes:

  * OFFLINE (SQLite, always runs): the ``db.py::_migrate`` path on a
    populated pre-0260 DB (columns + legacy quarantine + omnisight-self
    seed + fail-fast), fresh-``_SCHEMA`` vs ``_migrate`` episodic column
    parity, the alembic 0260 SQLite no-op (fake-bind, 0017 pattern),
    and ``search_verified_tenant_solutions``'s empty-tenant ValueError
    guard (raised BEFORE the connection is touched).
  * PG-MATRIX (``pg_test_conn``; runs only in the pg-live-integration
    CI job): insert safe-defaults, the verified+tenant+source+
    visibility row filter, and the verified⇒gerrit CHECK. The row
    FILTER is NOT provable offline — do not move those tests.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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


# ══════════════════════════════════════════════════════════════════
#  U6-0 T8-A (OP-2610) — source-aware substrate
# ══════════════════════════════════════════════════════════════════

_T8A_COLUMNS = (
    "tenant_id", "source", "verification_authority",
    "verified", "owner_user_id", "visibility",
)

# The episodic_memory CREATE TABLE exactly as it stood BEFORE 0260
# (db.py::_SCHEMA at develop 9e167666) — used to simulate an existing
# dev DB that must be carried forward by _migrate()'s ALTER path.
_PRE_0260_EPISODIC_SQL = """
CREATE TABLE episodic_memory (
    id              TEXT PRIMARY KEY,
    error_signature TEXT NOT NULL,
    solution        TEXT NOT NULL,
    soc_vendor      TEXT NOT NULL DEFAULT '',
    sdk_version     TEXT NOT NULL DEFAULT '',
    hardware_rev    TEXT NOT NULL DEFAULT '',
    source_task_id  TEXT,
    source_agent_id TEXT,
    gerrit_change_id TEXT,
    tags            TEXT NOT NULL DEFAULT '[]',
    quality_score   REAL NOT NULL DEFAULT 0.0,
    access_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def _point_db_at(path: Path) -> None:
    """Re-target the module-global SQLite path (same pattern as
    ``test_migrator_schema_coverage._live_schema_tables``)."""
    os.environ["OMNISIGHT_DATABASE_PATH"] = str(path)
    from backend import config as _cfg
    _cfg.settings.database_path = str(path)
    db._DB_PATH = db._resolve_db_path()


def _make_pre_0260_db(path: Path) -> None:
    """A populated pre-0260 dev DB: old-shape episodic_memory + rows."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(_PRE_0260_EPISODIC_SQL)
        conn.executemany(
            "INSERT INTO episodic_memory (id, error_signature, solution, "
            "quality_score, gerrit_change_id) VALUES (?, ?, ?, ?, ?)",
            [
                ("legacy-1", "old error one", "old solution one", 0.4, None),
                # quality 1.0 + a gerrit_change_id was mintable by the
                # unauth webhook — must be quarantined, NOT grandfathered.
                ("legacy-2", "old error two", "old solution two", 1.0, "I99"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


async def _episodic_columns(conn) -> dict[str, str]:
    async with conn.execute("PRAGMA table_info(episodic_memory)") as cur:
        return {r[1]: r[2].upper() for r in await cur.fetchall()}


# ─── OFFLINE lane: SQLite _migrate path ──────────────────────────


class TestSqliteMigrateSourceAware:
    @pytest.mark.asyncio
    async def test_migrate_adds_columns_quarantines_and_seeds(
        self, tmp_path: Path,
    ) -> None:
        """Pre-0260 populated DB + ``db.init()`` (PRAGMA foreign_keys=ON)
        ⇒ the 6 columns exist, legacy rows are quarantined, the
        omnisight-self tenant is seeded, the tenant index exists, and
        the REQUIRED fail-fast passed (init did not raise)."""
        db_path = tmp_path / "pre0260.db"
        _make_pre_0260_db(db_path)
        _point_db_at(db_path)
        await db.init()
        try:
            conn = db._conn()
            cols = await _episodic_columns(conn)
            for c in _T8A_COLUMNS:
                assert c in cols, f"_migrate() did not add {c}"

            async with conn.execute(
                "SELECT id, tenant_id, source, verified, visibility, "
                "verification_authority FROM episodic_memory ORDER BY id"
            ) as cur:
                rows = [tuple(r) for r in await cur.fetchall()]
            assert rows == [
                ("legacy-1", "t-default", "legacy", 0, "tenant_shared", None),
                ("legacy-2", "t-default", "legacy", 0, "tenant_shared", None),
            ], "legacy rows must be quarantined (verified=0/source=legacy)"

            async with conn.execute(
                "SELECT id FROM tenants WHERE id = 'omnisight-self'"
            ) as cur:
                assert await cur.fetchone() is not None, (
                    "omnisight-self tenant must be seeded before any "
                    "FK-carrying writer targets it"
                )

            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_episodic_tenant'"
            ) as cur:
                assert await cur.fetchone() is not None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_missing_episodic_column_fails_fast(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """The _migrate() loop swallows non-duplicate ALTER errors as
        warnings (fail-open); the REQUIRED set is the backstop. A
        silently-failed episodic ALTER must abort startup with
        RuntimeError — never continue into unquarantined reads."""
        db_path = tmp_path / "sabotaged.db"
        _make_pre_0260_db(db_path)
        _point_db_at(db_path)

        orig = db._render_add_column

        def sabotaged(table: str, column) -> str:
            if table == "episodic_memory" and column.name == "tenant_id":
                raise RuntimeError("simulated ALTER failure")
            return orig(table, column)

        monkeypatch.setattr(db, "_render_add_column", sabotaged)
        try:
            with pytest.raises(
                RuntimeError,
                match="episodic_memory.tenant_id missing after migration",
            ):
                await db.init()
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_fresh_schema_and_migrate_end_states_match(
        self, tmp_path: Path,
    ) -> None:
        """Drift guard between the TWO SQLite schema sources: a fresh
        ``_SCHEMA`` CREATE and a migrated pre-0260 DB must expose the
        same episodic_memory column set — and ``verified`` must be
        INTEGER in BOTH (the PG-gated column-parity test compares
        names only, so a Boolean/Integer split would slip through)."""
        fresh_path = tmp_path / "fresh.db"
        _point_db_at(fresh_path)
        await db.init()
        try:
            fresh_cols = await _episodic_columns(db._conn())
        finally:
            await db.close()

        migrated_path = tmp_path / "migrated.db"
        _make_pre_0260_db(migrated_path)
        _point_db_at(migrated_path)
        await db.init()
        try:
            migrated_cols = await _episodic_columns(db._conn())
        finally:
            await db.close()

        assert set(fresh_cols) == set(migrated_cols), (
            "episodic_memory columns drifted between _SCHEMA and _migrate"
        )
        assert fresh_cols["verified"] == "INTEGER"
        assert migrated_cols["verified"] == "INTEGER"


# ─── OFFLINE lane: default-secure helper guard ───────────────────


class TestSearchVerifiedTenantSolutionsGuard:
    @pytest.mark.asyncio
    async def test_empty_tenant_raises_before_touching_conn(self) -> None:
        """Fail-closed: an unresolved tenant must raise, not fall
        through to a global read. The guard fires BEFORE any conn
        call, so ``conn`` is a MagicMock we can assert was untouched.
        (Only the guard is offline-provable; the row FILTER is proven
        in the PG lane below.)"""
        conn = MagicMock()
        with pytest.raises(ValueError, match="tenant_id is required"):
            await db.search_verified_tenant_solutions(
                conn, "q", tenant_id="",
            )
        conn.fetch.assert_not_called()
        conn.execute.assert_not_called()


# ─── OFFLINE lane: alembic 0260 dialect split (0017 fake-bind pattern) ──


_MIGRATION_0260_PATH = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions"
    / "0260_episodic_memory_source_aware.py"
)


class TestAlembic0260DialectSplit:
    """Do NOT drive ``alembic upgrade head`` on fresh SQLite (early
    revs use PG-only DDL) — fake-bind unit test per
    ``test_alembic_0017_sqlite_noop.py``."""

    @pytest.fixture()
    def migration_mod(self) -> Any:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_0260_t8a_test", _MIGRATION_0260_PATH,
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_revision_chain(self, migration_mod: Any) -> None:
        assert migration_mod.revision == "0260"
        assert migration_mod.down_revision == "0259"

    def test_upgrade_is_noop_on_sqlite(
        self, migration_mod: Any, monkeypatch,
    ) -> None:
        bind = MagicMock()
        bind.dialect.name = "sqlite"
        monkeypatch.setattr(migration_mod.op, "get_bind", lambda: bind)
        migration_mod.upgrade()
        bind.exec_driver_sql.assert_not_called()

    def test_downgrade_is_noop_on_sqlite(
        self, migration_mod: Any, monkeypatch,
    ) -> None:
        bind = MagicMock()
        bind.dialect.name = "sqlite"
        monkeypatch.setattr(migration_mod.op, "get_bind", lambda: bind)
        migration_mod.downgrade()
        bind.exec_driver_sql.assert_not_called()

    def test_upgrade_issues_ordered_ddl_on_pg(
        self, migration_mod: Any, monkeypatch,
    ) -> None:
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        monkeypatch.setattr(migration_mod.op, "get_bind", lambda: bind)
        migration_mod.upgrade()
        stmts = [
            c.args[0] for c in bind.exec_driver_sql.call_args_list
        ]
        joined = "\n".join(stmts)
        # Seed comes FIRST (before the tenants FK exists).
        assert "omnisight-self" in stmts[0]
        assert "ON CONFLICT DO NOTHING" in stmts[0]
        # 6 column adds, quarantining defaults.
        adds = [s for s in stmts if "ADD COLUMN IF NOT EXISTS" in s]
        assert len(adds) == 6
        assert "DEFAULT 't-default'" in joined
        assert "verified BOOLEAN NOT NULL DEFAULT FALSE" in joined
        # Separate ADD CONSTRAINT (0244 style): the verified⇒gerrit
        # gate, the two IN-checks, and the tenants FK.
        constraints = [s for s in stmts if "ADD CONSTRAINT" in s]
        assert len(constraints) == 4
        assert (
            "NOT verified OR (source = 'service_gerrit_merge' "
            "AND verification_authority IS NOT DISTINCT FROM 'gerrit')"
        ) in joined
        assert "FOREIGN KEY (tenant_id) REFERENCES tenants(id)" in joined
        # Hot-filter index + explicit legacy backfill.
        assert "idx_episodic_tenant_verified" in joined
        backfills = [
            s for s in stmts
            if s.startswith("UPDATE episodic_memory")
        ]
        assert len(backfills) == 1
        assert "verified = FALSE" in backfills[0]
        assert "source = 'legacy'" in backfills[0]
        # Backfill runs LAST — after the columns it writes exist.
        assert stmts[-1] == backfills[0]


# ─── PG-MATRIX lane (pg_test_conn — CI pg-live-integration only) ──


async def _seed_tenant(conn, tenant_id: str) -> None:
    await conn.execute(
        "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free') "
        "ON CONFLICT DO NOTHING",
        tenant_id, f"Test {tenant_id}",
    )


def _verified_mem(**overrides) -> dict:
    """A row shaped like T8-B's Gerrit-verified service write — the
    ONLY shape the 0260 CHECK lets carry verified=TRUE."""
    base = _mem(
        source="service_gerrit_merge",
        verification_authority="gerrit",
        verified=True,
        tenant_id="t-default",
        visibility="tenant_shared",
    )
    base.update(overrides)
    return base


class TestSourceAwareInsertDefaultsPg:
    @pytest.mark.asyncio
    async def test_insert_without_new_fields_is_quarantined(
        self, pg_test_conn,
    ) -> None:
        """An un-updated caller (the 3 pre-T8-B writers) can NEVER
        implicitly mint a verified row."""
        await _seed_tenant(pg_test_conn, "t-default")
        await db.insert_episodic_memory(pg_test_conn, _mem(id="m-quar"))
        row = await pg_test_conn.fetchrow(
            "SELECT tenant_id, source, verified, visibility, "
            "verification_authority, owner_user_id "
            "FROM episodic_memory WHERE id = $1", "m-quar",
        )
        assert row["verified"] is False
        assert row["source"] == "legacy"
        assert row["visibility"] == "tenant_shared"
        assert row["tenant_id"] == "t-default"
        assert row["verification_authority"] is None
        assert row["owner_user_id"] is None

    @pytest.mark.asyncio
    async def test_verified_true_with_non_gerrit_source_rejected(
        self, pg_test_conn,
    ) -> None:
        """The 0260 verified⇒gerrit CHECK: a buggy/model writer that
        passes verified=True without the service_gerrit_merge+gerrit
        pair is rejected at the DB level."""
        import asyncpg
        await _seed_tenant(pg_test_conn, "t-default")
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await db.insert_episodic_memory(pg_test_conn, _mem(
                id="m-forge", verified=True, source="model_save_solution",
            ))

    @pytest.mark.asyncio
    async def test_verified_true_without_authority_rejected(
        self, pg_test_conn,
    ) -> None:
        import asyncpg
        await _seed_tenant(pg_test_conn, "t-default")
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await db.insert_episodic_memory(pg_test_conn, _mem(
                id="m-noauth", verified=True, source="service_gerrit_merge",
                verification_authority=None,
            ))


class TestSearchVerifiedTenantSolutionsPg:
    @pytest.mark.asyncio
    async def test_filter_excludes_every_untrusted_shape(
        self, pg_test_conn,
    ) -> None:
        """Only verified + source='service_gerrit_merge' +
        visibility='tenant_shared' + same-tenant rows come back; a
        quarantined legacy row, a model-source row, a private row, and
        another tenant's verified row are NEVER returned."""
        await _seed_tenant(pg_test_conn, "t-default")
        await _seed_tenant(pg_test_conn, "t-other")
        sig = "npu compile timeout in graph partitioner"
        await db.insert_episodic_memory(pg_test_conn, _verified_mem(
            id="m-good", error_signature=sig,
        ))
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-legacy", error_signature=sig,  # quarantined default
        ))
        await db.insert_episodic_memory(pg_test_conn, _mem(
            id="m-model", error_signature=sig, source="model_save_solution",
        ))
        await db.insert_episodic_memory(pg_test_conn, _verified_mem(
            id="m-private", error_signature=sig, visibility="private",
        ))
        await db.insert_episodic_memory(pg_test_conn, _verified_mem(
            id="m-other-tenant", error_signature=sig, tenant_id="t-other",
        ))

        rows = await db.search_verified_tenant_solutions(
            pg_test_conn, "npu compile timeout", tenant_id="t-default",
        )
        assert {r["id"] for r in rows} == {"m-good"}

        # The legacy/global reader is UNCHANGED (dormant substrate):
        # it still sees all five rows.
        legacy = await db.search_episodic_memory(
            pg_test_conn, "npu compile timeout",
        )
        assert {r["id"] for r in legacy} == {
            "m-good", "m-legacy", "m-model", "m-private", "m-other-tenant",
        }

    @pytest.mark.asyncio
    async def test_ranking_preserved_and_access_count_bumped(
        self, pg_test_conn,
    ) -> None:
        await _seed_tenant(pg_test_conn, "t-default")
        await db.insert_episodic_memory(pg_test_conn, _verified_mem(
            id="m-strong-v",
            error_signature="gpio i2c bus arbitration timeout error",
        ))
        await db.insert_episodic_memory(pg_test_conn, _verified_mem(
            id="m-weak-v",
            error_signature="unrelated message mentioning arbitration once",
        ))
        rows = await db.search_verified_tenant_solutions(
            pg_test_conn, "gpio i2c arbitration timeout",
            tenant_id="t-default",
        )
        assert rows[0]["id"] == "m-strong-v"
        row = await pg_test_conn.fetchrow(
            "SELECT access_count FROM episodic_memory WHERE id = $1",
            "m-strong-v",
        )
        assert row["access_count"] == 1

    @pytest.mark.asyncio
    async def test_vendor_sdk_quality_filters_compose(
        self, pg_test_conn,
    ) -> None:
        await _seed_tenant(pg_test_conn, "t-default")
        await db.insert_episodic_memory(pg_test_conn, _verified_mem(
            id="m-rk-v", soc_vendor="rockchip", quality_score=0.9,
            error_signature="isp pipeline stall",
        ))
        await db.insert_episodic_memory(pg_test_conn, _verified_mem(
            id="m-fh-v", soc_vendor="fullhan", quality_score=0.9,
            error_signature="isp pipeline stall",
        ))
        rows = await db.search_verified_tenant_solutions(
            pg_test_conn, "isp pipeline stall",
            tenant_id="t-default", soc_vendor="rockchip", min_quality=0.5,
        )
        assert [r["id"] for r in rows] == ["m-rk-v"]
