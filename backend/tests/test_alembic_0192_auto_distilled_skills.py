"""BP.M.1 -- alembic 0192 ``auto_distilled_skills`` contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0192 = (
    BACKEND_ROOT
    / "alembic"
    / "versions"
    / "0192_auto_distilled_skills.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0192():
    return _load_module(MIGRATION_0192, "_alembic_test_0192")


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0192.read_text()

    def test_revision_id_is_0192(self, source: str) -> None:
        assert 'revision = "0192"' in source

    def test_down_revision_is_0191(self, source: str) -> None:
        assert 'down_revision = "0191"' in source

    def test_required_columns_present(self, m0192) -> None:
        required = (
            "id",
            "tenant_id",
            "skill_name",
            "source_task_id",
            "markdown_content",
            "version",
            "status",
            "created_at",
        )
        for col in required:
            assert col in m0192._PG_CREATE_TABLE, f"PG branch missing {col}"
            assert col in m0192._SQLITE_CREATE_TABLE, f"SQLite branch missing {col}"

    def test_status_check_clause_declared(self, m0192) -> None:
        assert m0192._STATUSES_SQL == "'draft','promoted','reviewed'"
        assert "DEFAULT 'draft'" in m0192._PG_CREATE_TABLE
        assert "DEFAULT 'draft'" in m0192._SQLITE_CREATE_TABLE

    def test_text_pk_and_fks_declared(self, m0192) -> None:
        assert "id               TEXT PRIMARY KEY" in m0192._PG_CREATE_TABLE
        assert "REFERENCES tenants(id) ON DELETE CASCADE" in m0192._PG_CREATE_TABLE
        assert "REFERENCES tasks(id) ON DELETE SET NULL" in m0192._PG_CREATE_TABLE

    def test_created_at_dialect_shape(self, m0192) -> None:
        assert "TIMESTAMPTZ NOT NULL DEFAULT NOW()" in m0192._PG_CREATE_TABLE
        assert "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP" in (
            m0192._SQLITE_CREATE_TABLE
        )

    def test_indexes_declared(self, m0192) -> None:
        assert "idx_auto_distilled_skills_tenant_status" in (
            m0192._INDEX_TENANT_STATUS
        )
        assert "idx_auto_distilled_skills_source_task" in (
            m0192._INDEX_SOURCE_TASK
        )


def _bootstrap_parent_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE tenants (
            id TEXT PRIMARY KEY
        );
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY
        );
        """
    )


def test_sqlite_upgrade_creates_table_and_indexes(m0192) -> None:
    conn = sqlite3.connect(":memory:")
    _bootstrap_parent_schema(conn)
    conn.executescript(m0192._SQLITE_CREATE_TABLE)
    conn.execute(m0192._INDEX_TENANT_STATUS)
    conn.execute(m0192._INDEX_SOURCE_TASK)

    columns = {
        row[1]: row[2]
        for row in conn.execute("PRAGMA table_info(auto_distilled_skills)")
    }
    assert columns == {
        "id": "TEXT",
        "tenant_id": "TEXT",
        "skill_name": "TEXT",
        "source_task_id": "TEXT",
        "markdown_content": "TEXT",
        "version": "INTEGER",
        "status": "TEXT",
        "created_at": "TEXT",
    }
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(auto_distilled_skills)")
    }
    assert "idx_auto_distilled_skills_tenant_status" in indexes
    assert "idx_auto_distilled_skills_source_task" in indexes


def test_sqlite_status_check_rejects_invalid_status(m0192) -> None:
    conn = sqlite3.connect(":memory:")
    _bootstrap_parent_schema(conn)
    conn.executescript(m0192._SQLITE_CREATE_TABLE)
    conn.execute("INSERT INTO tenants(id) VALUES ('t-1')")
    conn.execute("INSERT INTO tasks(id) VALUES ('task-1')")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO auto_distilled_skills (
                id, tenant_id, skill_name, source_task_id,
                markdown_content, status
            ) VALUES (:id, :tenant_id, :skill_name, :source_task_id,
                      :markdown_content, :status)
            """,
            {
                "id": "ads-1",
                "tenant_id": "t-1",
                "skill_name": "skill-fastapi",
                "source_task_id": "task-1",
                "markdown_content": "# body",
                "status": "skipped",
            },
        )


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

    def test_auto_distilled_skills_in_tables_in_order(self) -> None:
        mig = self._load_migrator()
        assert "auto_distilled_skills" in mig.TABLES_IN_ORDER

    def test_auto_distilled_skills_not_in_identity_id_set(self) -> None:
        mig = self._load_migrator()
        assert "auto_distilled_skills" not in mig.TABLES_WITH_IDENTITY_ID

    def test_auto_distilled_skills_replays_after_tenants_and_tasks(self) -> None:
        mig = self._load_migrator()
        order = list(mig.TABLES_IN_ORDER)
        assert order.index("tenants") < order.index("auto_distilled_skills")
        assert order.index("tasks") < order.index("auto_distilled_skills")
