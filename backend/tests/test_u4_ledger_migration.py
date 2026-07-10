"""OP-2565 U4-A1 — immutable learned-item ledger contract tests.

Covers the ticket's AC (a)-(g) offline:

* (a) migration 0258 ``upgrade()``/``downgrade()`` run clean on an
  in-memory SQLite ``Operations`` context (module-load harness per the
  0249/0250 precedent — NOT the alembic CLI); PG via ``pg_test_pool``
  when ``OMNI_TEST_PG_URL`` is set, else skipped at fixture setup.
* (b) all 8 tables + partial-unique indexes + CHECKs + append-only
  triggers exist after upgrade.
* (c) UPDATE and DELETE on ledger rows RAISE (both dialects).
* (d) partial-unique dedupe per audience scope.
* (e) audience/tenant_id consistency CHECKs.
* (f) ``canonical_content_hash`` determinism + provenance-blindness.
* (g) ``learned_item_snapshots`` stays mutable.

No FK-enforcement assertions on SQLite (``PRAGMA foreign_keys`` is OFF
by default — such a test would silently never fire).
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0258 = (
    BACKEND_ROOT / "alembic" / "versions" / "0258_u4_learned_item_ledger.py"
)

ALL_TABLES = (
    "learned_item_versions",
    "learned_item_evidence",
    "memory_eval_runs",
    "memory_eval_cases",
    "memory_approvals",
    "memory_publications",
    "memory_transition_events",
    "learned_item_snapshots",
)
LEDGER_TABLES = ALL_TABLES[:-1]  # snapshots is the one mutable table

HASH_A = "a" * 64
HASH_B = "b" * 64


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0258():
    return _load_module(MIGRATION_0258, "_alembic_test_0258")


# ━━ Structural guards ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0258.read_text()

    def test_revision_id_is_0258(self, source: str) -> None:
        assert 'revision = "0258"' in source

    def test_down_revision_is_0257(self, source: str) -> None:
        assert 'down_revision = "0257"' in source

    def test_backwards_compat_marked_safe(self, source: str) -> None:
        assert "backwards-compat: safe" in source.lower() or (
            "Backwards-compat: safe" in source
        )

    def test_partial_unique_indexes_declared(self, source: str) -> None:
        assert "uq_learned_item_versions_global_hash" in source
        assert "uq_learned_item_versions_tenant_hash" in source
        assert "WHERE audience = 'global'" in source
        assert "WHERE audience = 'tenant'" in source

    def test_pg_hash_check_is_hex_regex(self, source: str) -> None:
        assert "~ '^[0-9a-f]{64}$'" in source

    def test_sqlite_hash_check_is_length(self, source: str) -> None:
        assert "length(canonical_content_hash) = 64" in source

    def test_no_mutable_status_column(self, source: str) -> None:
        # State is append-only events (freeze V3.3) — a `status` column
        # anywhere in the DDL would violate the contract. Column
        # declarations are indented `name TYPE ...` lines.
        import re

        assert not re.search(r"(?im)^\s+status\s", source)

    def test_dormant_ship_no_producers_or_consumers(self) -> None:
        # MUST-NOT: nothing outside the migration + tests may reference
        # the new tables (producers land in U4-I, publisher/loader U4-C).
        # OP-2567 U4-B: the sanctioned writer boundary is the ONE
        # allowlisted module — any OTHER file referencing the tables
        # still fails.
        allowed = {"learned_item_publisher.py"}
        offenders: list[str] = []
        for py in BACKEND_ROOT.rglob("*.py"):
            rel = py.relative_to(BACKEND_ROOT)
            parts = rel.parts
            if parts[0] in ("tests", "node_modules") or (
                parts[:2] == ("alembic", "versions")
            ):
                continue
            if str(rel) in allowed:
                continue
            text = py.read_text(errors="ignore")
            if any(table in text for table in ALL_TABLES):
                offenders.append(str(rel))
        assert offenders == [], f"dormant-ship violated by: {offenders}"


# ━━ SQLite harness (0249/0250 precedent — no alembic CLI) ━━━━━━━━━━━━


@pytest.fixture()
def conn():
    """Empty in-memory SQLite connection with an Operations proxy."""
    import sqlalchemy as sa
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    connection = engine.connect()
    ctx = MigrationContext.configure(connection=connection)
    with Operations.context(ctx):
        yield connection
    connection.close()


def _table_names(conn) -> set[str]:
    return {
        row[0]
        for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _trigger_names(conn) -> set[str]:
    return {
        row[0]
        for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }


def _index_names(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA index_list('{table}')").fetchall()
    return {row[1] for row in rows}


def _insert_version(
    conn,
    *,
    vid: str | None = None,
    content_hash: str = HASH_A,
    audience: str = "global",
    tenant_id: str | None = None,
) -> str:
    vid = vid or uuid.uuid4().hex
    conn.exec_driver_sql(
        "INSERT INTO learned_item_versions "
        "(id, canonical_content_hash, kind, audience, tenant_id, "
        " payload, created_by) "
        "VALUES (?, ?, 'lesson', ?, ?, '{}', 'test')",
        (vid, content_hash, audience, tenant_id),
    )
    return vid


# (a) upgrade/downgrade clean ----------------------------------------


class TestSqliteUpgradeDowngrade:
    def test_upgrade_runs_clean(self, conn, m0258) -> None:
        m0258.upgrade()
        assert set(ALL_TABLES) <= _table_names(conn)

    def test_upgrade_is_idempotent_on_rerun(self, conn, m0258) -> None:
        m0258.upgrade()
        m0258.upgrade()
        assert set(ALL_TABLES) <= _table_names(conn)

    def test_downgrade_drops_everything(self, conn, m0258) -> None:
        m0258.upgrade()
        m0258.downgrade()
        remaining = _table_names(conn)
        assert not (set(ALL_TABLES) & remaining)
        assert not any(t.startswith("trg_") for t in _trigger_names(conn))

    def test_upgrade_after_downgrade_cycles_clean(self, conn, m0258) -> None:
        m0258.upgrade()
        m0258.downgrade()
        m0258.upgrade()
        assert set(ALL_TABLES) <= _table_names(conn)


# (b) tables + indexes + triggers + CHECKs exist ----------------------


class TestSqliteSchemaShape:
    def test_all_eight_tables_exist(self, conn, m0258) -> None:
        m0258.upgrade()
        assert set(ALL_TABLES) <= _table_names(conn)

    def test_partial_unique_indexes_exist(self, conn, m0258) -> None:
        m0258.upgrade()
        idx = _index_names(conn, "learned_item_versions")
        assert "uq_learned_item_versions_global_hash" in idx
        assert "uq_learned_item_versions_tenant_hash" in idx

    def test_append_only_triggers_exist_on_all_ledger_tables(
        self, conn, m0258
    ) -> None:
        m0258.upgrade()
        triggers = _trigger_names(conn)
        for table in LEDGER_TABLES:
            assert f"trg_{table}_no_update" in triggers, table
            assert f"trg_{table}_no_delete" in triggers, table

    def test_snapshots_has_no_append_only_triggers(self, conn, m0258) -> None:
        m0258.upgrade()
        triggers = _trigger_names(conn)
        assert "trg_learned_item_snapshots_no_update" not in triggers
        assert "trg_learned_item_snapshots_no_delete" not in triggers

    def test_hash_length_check_enforced(self, conn, m0258) -> None:
        m0258.upgrade()
        with pytest.raises(Exception):
            _insert_version(conn, content_hash="deadbeef")  # not 64 chars

    def test_kind_check_enforced(self, conn, m0258) -> None:
        m0258.upgrade()
        with pytest.raises(Exception):
            conn.exec_driver_sql(
                "INSERT INTO learned_item_versions "
                "(id, canonical_content_hash, kind, audience, payload, "
                " created_by) "
                "VALUES ('v-bad', ?, 'not-a-kind', 'global', '{}', 'test')",
                (HASH_A,),
            )

    def test_delivery_mode_check_enforced(self, conn, m0258) -> None:
        m0258.upgrade()
        with pytest.raises(Exception):
            conn.exec_driver_sql(
                "INSERT INTO learned_item_versions "
                "(id, canonical_content_hash, kind, audience, "
                " delivery_mode, payload, created_by) "
                "VALUES ('v-bad', ?, 'lesson', 'global', 'pushed', '{}', "
                "'test')",
                (HASH_A,),
            )

    def test_publication_state_check_enforced(self, conn, m0258) -> None:
        m0258.upgrade()
        vid = _insert_version(conn)
        with pytest.raises(Exception):
            conn.exec_driver_sql(
                "INSERT INTO memory_publications (id, version_id, state) "
                "VALUES ('p-bad', ?, 'not-a-state')",
                (vid,),
            )


# (c) append-only enforcement -----------------------------------------


class TestSqliteAppendOnly:
    def test_update_on_versions_raises(self, conn, m0258) -> None:
        m0258.upgrade()
        vid = _insert_version(conn)
        with pytest.raises(Exception, match="append-only"):
            conn.exec_driver_sql(
                "UPDATE learned_item_versions SET created_by='mallory' "
                "WHERE id=?",
                (vid,),
            )

    def test_delete_on_versions_raises(self, conn, m0258) -> None:
        m0258.upgrade()
        vid = _insert_version(conn)
        with pytest.raises(Exception, match="append-only"):
            conn.exec_driver_sql(
                "DELETE FROM learned_item_versions WHERE id=?", (vid,)
            )

    def test_update_on_transition_events_raises(self, conn, m0258) -> None:
        m0258.upgrade()
        conn.exec_driver_sql(
            "INSERT INTO memory_transition_events "
            "(id, version_id, to_state, actor) "
            "VALUES ('e-1', 'v-1', 'approved', 'tester')"
        )
        with pytest.raises(Exception, match="append-only"):
            conn.exec_driver_sql(
                "UPDATE memory_transition_events SET actor='mallory' "
                "WHERE id='e-1'"
            )

    def test_delete_on_transition_events_raises(self, conn, m0258) -> None:
        m0258.upgrade()
        conn.exec_driver_sql(
            "INSERT INTO memory_transition_events "
            "(id, version_id, to_state, actor) "
            "VALUES ('e-2', 'v-1', 'approved', 'tester')"
        )
        with pytest.raises(Exception, match="append-only"):
            conn.exec_driver_sql(
                "DELETE FROM memory_transition_events WHERE id='e-2'"
            )


# (d) partial-unique dedupe per scope ---------------------------------


class TestSqlitePartialUnique:
    def test_duplicate_global_hash_rejected(self, conn, m0258) -> None:
        m0258.upgrade()
        _insert_version(conn, content_hash=HASH_A, audience="global")
        with pytest.raises(Exception):
            _insert_version(conn, content_hash=HASH_A, audience="global")

    def test_same_hash_different_tenants_allowed(self, conn, m0258) -> None:
        m0258.upgrade()
        _insert_version(
            conn, content_hash=HASH_A, audience="tenant", tenant_id="t-1"
        )
        _insert_version(
            conn, content_hash=HASH_A, audience="tenant", tenant_id="t-2"
        )
        count = conn.exec_driver_sql(
            "SELECT count(*) FROM learned_item_versions"
        ).scalar()
        assert count == 2

    def test_same_hash_same_tenant_rejected(self, conn, m0258) -> None:
        m0258.upgrade()
        _insert_version(
            conn, content_hash=HASH_B, audience="tenant", tenant_id="t-1"
        )
        with pytest.raises(Exception):
            _insert_version(
                conn, content_hash=HASH_B, audience="tenant", tenant_id="t-1"
            )

    def test_global_and_tenant_scopes_do_not_collide(self, conn, m0258) -> None:
        m0258.upgrade()
        _insert_version(conn, content_hash=HASH_A, audience="global")
        _insert_version(
            conn, content_hash=HASH_A, audience="tenant", tenant_id="t-1"
        )
        count = conn.exec_driver_sql(
            "SELECT count(*) FROM learned_item_versions"
        ).scalar()
        assert count == 2


# (e) audience consistency CHECKs -------------------------------------


class TestSqliteAudienceChecks:
    def test_global_with_tenant_id_rejected(self, conn, m0258) -> None:
        m0258.upgrade()
        with pytest.raises(Exception):
            _insert_version(conn, audience="global", tenant_id="t-1")

    def test_tenant_with_null_tenant_id_rejected(self, conn, m0258) -> None:
        m0258.upgrade()
        with pytest.raises(Exception):
            _insert_version(conn, audience="tenant", tenant_id=None)

    def test_bad_audience_rejected(self, conn, m0258) -> None:
        m0258.upgrade()
        with pytest.raises(Exception):
            _insert_version(conn, audience="everyone")


# (g) snapshots stay mutable ------------------------------------------


class TestSqliteSnapshotsMutable:
    def test_update_on_snapshots_succeeds(self, conn, m0258) -> None:
        m0258.upgrade()
        conn.exec_driver_sql(
            "INSERT INTO learned_item_snapshots "
            "(scope_key, live_set_head, membership) "
            "VALUES ('global', 1, '[]')"
        )
        conn.exec_driver_sql(
            "UPDATE learned_item_snapshots SET membership='[\"v-1\"]' "
            "WHERE scope_key='global' AND live_set_head=1"
        )
        row = conn.exec_driver_sql(
            "SELECT membership FROM learned_item_snapshots "
            "WHERE scope_key='global' AND live_set_head=1"
        ).fetchone()
        assert row[0] == '["v-1"]'

    def test_delete_on_snapshots_succeeds(self, conn, m0258) -> None:
        m0258.upgrade()
        conn.exec_driver_sql(
            "INSERT INTO learned_item_snapshots "
            "(scope_key, live_set_head, membership) "
            "VALUES ('global', 2, '[]')"
        )
        conn.exec_driver_sql(
            "DELETE FROM learned_item_snapshots "
            "WHERE scope_key='global' AND live_set_head=2"
        )
        count = conn.exec_driver_sql(
            "SELECT count(*) FROM learned_item_snapshots"
        ).scalar()
        assert count == 0


# (f) canonical content hash ------------------------------------------


class TestCanonicalContentHash:
    def test_deterministic_and_key_order_independent(self) -> None:
        from backend.learned_item_hash import canonical_content_hash

        p1 = {"title": "lesson", "body": "content", "keywords": ["a", "b"]}
        p2 = {"keywords": ["a", "b"], "body": "content", "title": "lesson"}
        h1 = canonical_content_hash(p1)
        assert h1 == canonical_content_hash(p1)
        assert h1 == canonical_content_hash(p2)
        assert len(h1) == 64
        assert all(c in "0123456789abcdef" for c in h1)

    def test_ignores_provenance_and_timestamp_fields(self) -> None:
        from backend.learned_item_hash import canonical_content_hash

        base = {"title": "lesson", "body": "content"}
        with_provenance = {
            "title": "lesson",
            "body": "content",
            "created_at": "2026-07-10T00:00:00Z",
            "created_by": "agent-7",
            "source_change_id": "I123",
            "verified_at": "2026-07-11T00:00:00Z",
            "provenance": {"gerrit": "change-2043"},
            "_meta": {"trace": "xyz"},
        }
        assert canonical_content_hash(base) == canonical_content_hash(
            with_provenance
        )

    def test_ignores_nested_provenance_keys(self) -> None:
        from backend.learned_item_hash import canonical_content_hash

        p1 = {"title": "x", "detail": {"body": "y"}}
        p2 = {
            "title": "x",
            "detail": {"body": "y", "created_at": "2026-01-01"},
        }
        assert canonical_content_hash(p1) == canonical_content_hash(p2)

    def test_changes_on_content_edit(self) -> None:
        from backend.learned_item_hash import canonical_content_hash

        p1 = {"title": "lesson", "body": "content"}
        p2 = {"title": "lesson", "body": "content EDITED"}
        assert canonical_content_hash(p1) != canonical_content_hash(p2)


# ━━ Postgres (skipped when OMNI_TEST_PG_URL is unset) ━━━━━━━━━━━━━━━━


def _fresh_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex  # 64 hex chars


async def _pg_insert_version(
    conn,
    *,
    content_hash: str,
    audience: str = "global",
    tenant_id: str | None = None,
) -> str:
    vid = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO learned_item_versions "
        "(id, canonical_content_hash, kind, audience, tenant_id, "
        " payload, created_by) "
        "VALUES ($1, $2, 'lesson', $3, $4, '{}', 'test')",
        vid,
        content_hash,
        audience,
        tenant_id,
    )
    return vid


async def _pg_ensure_tenant(conn, tid: str) -> None:
    await conn.execute(
        "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free') "
        "ON CONFLICT (id) DO NOTHING",
        tid,
        f"u4-ledger-test {tid}",
    )


class TestPostgres:
    async def test_all_eight_tables_exist(self, pg_test_pool) -> None:
        async with pg_test_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename = ANY($1::text[])",
                list(ALL_TABLES),
            )
        assert {r["tablename"] for r in rows} == set(ALL_TABLES)

    async def test_partial_unique_indexes_exist(self, pg_test_pool) -> None:
        async with pg_test_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename='learned_item_versions'"
            )
        names = {r["indexname"] for r in rows}
        assert "uq_learned_item_versions_global_hash" in names
        assert "uq_learned_item_versions_tenant_hash" in names

    async def test_append_only_triggers_exist(self, pg_test_pool) -> None:
        async with pg_test_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"
            )
        names = {r["tgname"] for r in rows}
        for table in LEDGER_TABLES:
            assert f"trg_{table}_no_update" in names, table
            assert f"trg_{table}_no_delete" in names, table

    async def test_update_and_delete_on_versions_raise(
        self, pg_test_pool
    ) -> None:
        import asyncpg

        async with pg_test_pool.acquire() as conn:
            vid = await _pg_insert_version(conn, content_hash=_fresh_hash())
            with pytest.raises(asyncpg.PostgresError, match="append-only"):
                await conn.execute(
                    "UPDATE learned_item_versions SET created_by='mallory' "
                    "WHERE id=$1",
                    vid,
                )
            with pytest.raises(asyncpg.PostgresError, match="append-only"):
                await conn.execute(
                    "DELETE FROM learned_item_versions WHERE id=$1", vid
                )

    async def test_update_on_transition_events_raises(
        self, pg_test_pool
    ) -> None:
        import asyncpg

        async with pg_test_pool.acquire() as conn:
            eid = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO memory_transition_events "
                "(id, version_id, to_state, actor) "
                "VALUES ($1, $2, 'approved', 'tester')",
                eid,
                str(uuid.uuid4()),
            )
            with pytest.raises(asyncpg.PostgresError, match="append-only"):
                await conn.execute(
                    "UPDATE memory_transition_events SET actor='m' "
                    "WHERE id=$1",
                    eid,
                )
            with pytest.raises(asyncpg.PostgresError, match="append-only"):
                await conn.execute(
                    "DELETE FROM memory_transition_events WHERE id=$1", eid
                )

    async def test_partial_unique_scoping(self, pg_test_pool) -> None:
        import asyncpg

        h = _fresh_hash()
        t1, t2 = f"u4t-{uuid.uuid4().hex[:8]}", f"u4t-{uuid.uuid4().hex[:8]}"
        async with pg_test_pool.acquire() as conn:
            await _pg_ensure_tenant(conn, t1)
            await _pg_ensure_tenant(conn, t2)
            # global dedupe (the case a plain UNIQUE would silently miss)
            await _pg_insert_version(conn, content_hash=h, audience="global")
            with pytest.raises(asyncpg.UniqueViolationError):
                await _pg_insert_version(
                    conn, content_hash=h, audience="global"
                )
            # same hash, different tenants → both allowed
            await _pg_insert_version(
                conn, content_hash=h, audience="tenant", tenant_id=t1
            )
            await _pg_insert_version(
                conn, content_hash=h, audience="tenant", tenant_id=t2
            )
            # same tenant + same hash → rejected
            with pytest.raises(asyncpg.UniqueViolationError):
                await _pg_insert_version(
                    conn, content_hash=h, audience="tenant", tenant_id=t1
                )

    async def test_audience_checks(self, pg_test_pool) -> None:
        import asyncpg

        async with pg_test_pool.acquire() as conn:
            tid = f"u4t-{uuid.uuid4().hex[:8]}"
            await _pg_ensure_tenant(conn, tid)
            with pytest.raises(asyncpg.CheckViolationError):
                await _pg_insert_version(
                    conn,
                    content_hash=_fresh_hash(),
                    audience="global",
                    tenant_id=tid,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await _pg_insert_version(
                    conn,
                    content_hash=_fresh_hash(),
                    audience="tenant",
                    tenant_id=None,
                )

    async def test_hash_hex_check(self, pg_test_pool) -> None:
        import asyncpg

        async with pg_test_pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await _pg_insert_version(
                    conn, content_hash="Z" * 64  # 64 chars but not hex
                )

    async def test_snapshots_mutable(self, pg_test_pool) -> None:
        key = f"u4-test-{uuid.uuid4().hex[:8]}"
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO learned_item_snapshots "
                "(scope_key, live_set_head, membership) VALUES ($1, 1, '[]')",
                key,
            )
            await conn.execute(
                "UPDATE learned_item_snapshots SET membership='[\"v\"]' "
                "WHERE scope_key=$1 AND live_set_head=1",
                key,
            )
            await conn.execute(
                "DELETE FROM learned_item_snapshots WHERE scope_key=$1", key
            )
