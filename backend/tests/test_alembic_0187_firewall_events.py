"""KS.4.13 -- alembic 0187 ``firewall_events`` contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0187 = BACKEND_ROOT / "alembic" / "versions" / "0187_firewall_events.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0187():
    return _load_module(MIGRATION_0187, "_alembic_test_0187")


# -- Group 1: structural guards --------------------------------------------


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0187.read_text()

    def test_revision_id_is_0187(self, source: str) -> None:
        assert 'revision = "0187"' in source

    def test_down_revision_is_0185(self, source: str) -> None:
        assert 'down_revision = "0185"' in source

    def test_required_columns_present(self, m0187) -> None:
        required = (
            "event_id",
            "tenant_id",
            "classification",
            "input_hash",
            "blocked_reason",
            "created_at",
        )
        for col in required:
            assert col in m0187._PG_CREATE_TABLE, f"PG branch missing {col}"
            assert col in m0187._SQLITE_CREATE_TABLE, f"SQLite branch missing {col}"

    def test_plaintext_input_column_is_absent(self, m0187) -> None:
        joined = "\n".join((m0187._PG_CREATE_TABLE, m0187._SQLITE_CREATE_TABLE))
        assert "input_text" not in joined
        assert "plain" not in joined.lower()
        assert "input_hash" in joined

    def test_classification_check_only_persists_review_cases(self, m0187) -> None:
        assert m0187._CLASSIFICATIONS_SQL == "'blocked','suspicious'"
        assert "CHECK (classification IN ('blocked','suspicious'))" in (
            m0187._PG_CREATE_TABLE
        )
        assert "safe" not in m0187._CLASSIFICATIONS_SQL

    def test_pg_branch_uses_timestamptz_default(self, m0187) -> None:
        assert "TIMESTAMPTZ NOT NULL DEFAULT NOW()" in m0187._PG_CREATE_TABLE
        assert " REAL" not in m0187._PG_CREATE_TABLE

    def test_sqlite_branch_uses_text_timestamp(self, m0187) -> None:
        assert "created_at     TEXT NOT NULL" in m0187._SQLITE_CREATE_TABLE
        assert "TIMESTAMPTZ" not in m0187._SQLITE_CREATE_TABLE

    def test_tenant_fk_and_indexes_declared(self, m0187) -> None:
        assert "REFERENCES tenants(id) ON DELETE CASCADE" in m0187._PG_CREATE_TABLE
        assert "idx_firewall_events_tenant_class_time" in (
            m0187._INDEX_TENANT_CLASS_TIME
        )
        assert "idx_firewall_events_input_hash" in m0187._INDEX_INPUT_HASH


# -- Group 2: functional SQLite upgrade ------------------------------------


def _bootstrap_pre_0187_parent_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE tenants (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT ''
        );
        """
    )


class _StubBind:
    """Mimics enough of an alembic context bind for ``conn.exec_driver_sql``."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

        class _Dialect:
            name = "sqlite"

        self.dialect = _Dialect()

    def exec_driver_sql(self, sql: str, *args, **kwargs):
        return self._raw.execute(sql)


def _bind(monkeypatch, conn: sqlite3.Connection) -> None:
    from alembic import op as alembic_op

    bind = _StubBind(conn)
    monkeypatch.setattr(alembic_op, "get_bind", lambda: bind)


@pytest.fixture()
def upgraded_db(monkeypatch, m0187) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bootstrap_pre_0187_parent_schema(conn)
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-a', 'Tenant A')")
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-b', 'Tenant B')")
    _bind(monkeypatch, conn)
    m0187.upgrade()
    return conn


def _insert_event(
    conn: sqlite3.Connection,
    event_id: str,
    tenant_id: str = "t-a",
    *,
    classification: str = "blocked",
    input_hash: str = "sha256:abc",
    blocked_reason: str = "prompt_injection",
) -> None:
    conn.execute(
        "INSERT INTO firewall_events "
        "(event_id, tenant_id, classification, input_hash, blocked_reason, "
        "created_at) VALUES (:event_id, :tenant_id, :classification, "
        ":input_hash, :blocked_reason, :created_at)",
        {
            "event_id": event_id,
            "tenant_id": tenant_id,
            "classification": classification,
            "input_hash": input_hash,
            "blocked_reason": blocked_reason,
            "created_at": "2026-05-03T00:00:00Z",
        },
    )


class TestSqliteUpgradeCreatesTable:
    def test_firewall_events_table_exists(self, upgraded_db) -> None:
        row = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='firewall_events'"
        ).fetchone()
        assert row is not None

    def test_all_required_columns_present(self, upgraded_db) -> None:
        cols = {
            row[1]
            for row in upgraded_db.execute(
                "PRAGMA table_info(firewall_events)"
            ).fetchall()
        }
        required = {
            "event_id",
            "tenant_id",
            "classification",
            "input_hash",
            "blocked_reason",
            "created_at",
        }
        assert not (required - cols)

    def test_accepts_blocked_and_suspicious_only(self, upgraded_db) -> None:
        _insert_event(upgraded_db, "fw-blocked", classification="blocked")
        _insert_event(upgraded_db, "fw-suspicious", classification="suspicious")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_event(upgraded_db, "fw-safe", classification="safe")

    def test_default_blocked_reason_is_empty(self, upgraded_db) -> None:
        upgraded_db.execute(
            "INSERT INTO firewall_events "
            "(event_id, tenant_id, classification, input_hash, created_at) "
            "VALUES ('fw-default', 't-a', 'suspicious', 'sha256:def', "
            "'2026-05-03T00:00:00Z')"
        )
        row = upgraded_db.execute(
            "SELECT blocked_reason FROM firewall_events WHERE event_id='fw-default'"
        ).fetchone()
        assert row == ("",)

    def test_tenant_delete_cascades_events(self, upgraded_db) -> None:
        _insert_event(upgraded_db, "fw-a", tenant_id="t-a")
        _insert_event(upgraded_db, "fw-b", tenant_id="t-b")
        upgraded_db.execute("DELETE FROM tenants WHERE id='t-a'")
        rows = upgraded_db.execute(
            "SELECT event_id FROM firewall_events ORDER BY event_id"
        ).fetchall()
        assert rows == [("fw-b",)]

    def test_indexes_created(self, upgraded_db) -> None:
        rows = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name LIKE 'idx_firewall_events_%' "
            "ORDER BY name"
        ).fetchall()
        assert rows == [
            ("idx_firewall_events_input_hash",),
            ("idx_firewall_events_tenant_class_time",),
        ]


# -- Group 3: idempotency ---------------------------------------------------


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_no_dup_no_change(self, monkeypatch, m0187) -> None:
        conn = sqlite3.connect(":memory:")
        _bootstrap_pre_0187_parent_schema(conn)
        _bind(monkeypatch, conn)
        m0187.upgrade()
        m0187.upgrade()
        row = conn.execute("SELECT count(*) FROM firewall_events").fetchone()
        assert row == (0,)


# -- Group 4: PG dialect branch executes -----------------------------------


class TestPgBranchExecutes:
    def test_pg_branch_emits_create_table_and_indexes(self, monkeypatch, m0187) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0187.upgrade()

        assert len(captured) == 3
        joined = "\n".join(captured)
        assert "CREATE TABLE IF NOT EXISTS firewall_events" in joined
        assert "input_hash" in joined
        assert "input_text" not in joined
        assert "TIMESTAMPTZ NOT NULL DEFAULT NOW()" in joined
        assert "idx_firewall_events_tenant_class_time" in joined
        assert "idx_firewall_events_input_hash" in joined

    def test_pg_downgrade_drops_indexes_and_table(self, monkeypatch, m0187) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        def _exec(sql):
            captured.append(str(sql))

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        monkeypatch.setattr(alembic_op, "execute", _exec)
        m0187.downgrade()
        joined = "\n".join(captured)
        assert "DROP INDEX IF EXISTS idx_firewall_events_input_hash" in joined
        assert "DROP INDEX IF EXISTS idx_firewall_events_tenant_class_time" in joined
        assert "DROP TABLE IF EXISTS firewall_events" in joined


# -- Group 5: migrator drift guard -----------------------------------------


class TestMigratorListsTable:
    def _load_migrator(self):
        import importlib.util as _u

        repo_root = Path(__file__).resolve().parents[2]
        spec = _u.spec_from_file_location(
            "migrate_sqlite_to_pg", repo_root / "scripts" / "migrate_sqlite_to_pg.py"
        )
        mig = _u.module_from_spec(spec)
        sys.modules["migrate_sqlite_to_pg"] = mig
        spec.loader.exec_module(mig)
        return mig

    def test_firewall_events_in_tables_in_order(self) -> None:
        mig = self._load_migrator()
        assert "firewall_events" in mig.TABLES_IN_ORDER

    def test_firewall_events_not_in_identity_id_set(self) -> None:
        mig = self._load_migrator()
        assert "firewall_events" not in mig.TABLES_WITH_IDENTITY_ID

    def test_firewall_events_replays_after_tenants(self) -> None:
        mig = self._load_migrator()
        order = list(mig.TABLES_IN_ORDER)
        assert order.index("tenants") < order.index("firewall_events")
