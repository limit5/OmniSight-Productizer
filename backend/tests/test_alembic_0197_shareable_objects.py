"""WP.9.1 -- alembic 0197 ``shareable_objects`` contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0197 = BACKEND_ROOT / "alembic" / "versions" / "0197_shareable_objects.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0197():
    return _load_module(MIGRATION_0197, "_alembic_test_0197")


def test_revision_id_and_parent_are_declared() -> None:
    source = MIGRATION_0197.read_text()
    assert 'revision = "0197"' in source
    assert 'down_revision = "0196"' in source


def test_required_columns_present_and_typed_for_pg(m0197) -> None:
    required = (
        "share_id",
        "object_kind",
        "object_id",
        "tenant_id",
        "owner_user_id",
        "visibility",
        "expires_at",
        "redaction_applied",
        "created_at",
    )

    for col in required:
        assert col in m0197._PG_CREATE_TABLE, f"PG branch missing {col}"

    assert "share_id          TEXT PRIMARY KEY" in m0197._PG_CREATE_TABLE
    assert "REFERENCES tenants(id) ON DELETE CASCADE" in m0197._PG_CREATE_TABLE
    assert "REFERENCES users(id) ON DELETE CASCADE" in m0197._PG_CREATE_TABLE
    assert "visibility IN ('private','team','tenant','public')" in (
        m0197._PG_CREATE_TABLE
    )
    assert "expires_at        TIMESTAMPTZ" in m0197._PG_CREATE_TABLE
    assert "redaction_applied JSONB NOT NULL DEFAULT '{}'::jsonb" in (
        m0197._PG_CREATE_TABLE
    )
    assert "created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()" in (
        m0197._PG_CREATE_TABLE
    )


def test_indexes_declared(m0197) -> None:
    assert "idx_shareable_objects_tenant_object" in m0197._INDEX_TENANT_OBJECT
    assert "ON shareable_objects(tenant_id, object_kind, object_id)" in (
        m0197._INDEX_TENANT_OBJECT
    )
    assert "idx_shareable_objects_owner_created" in m0197._INDEX_OWNER_CREATED
    assert "ON shareable_objects(owner_user_id, created_at DESC)" in (
        m0197._INDEX_OWNER_CREATED
    )
    assert "idx_shareable_objects_expires_at" in m0197._INDEX_EXPIRES_AT
    assert "WHERE expires_at IS NOT NULL" in m0197._INDEX_EXPIRES_AT


def _bootstrap_parent_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE tenants (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE users (
            id        TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id)
        );
        """
    )


def test_sqlite_upgrade_creates_dev_parity_table_and_indexes(m0197) -> None:
    conn = sqlite3.connect(":memory:")
    _bootstrap_parent_schema(conn)
    conn.executescript(m0197._SQLITE_CREATE_TABLE)
    conn.execute(m0197._INDEX_TENANT_OBJECT)
    conn.execute(m0197._INDEX_OWNER_CREATED)
    conn.execute(m0197._INDEX_EXPIRES_AT)

    columns = {
        row[1]: row[2]
        for row in conn.execute("PRAGMA table_info(shareable_objects)")
    }
    assert columns == {
        "share_id": "TEXT",
        "object_kind": "TEXT",
        "object_id": "TEXT",
        "tenant_id": "TEXT",
        "owner_user_id": "TEXT",
        "visibility": "TEXT",
        "expires_at": "TEXT",
        "redaction_applied": "TEXT",
        "created_at": "TEXT",
    }
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(shareable_objects)")
    }
    assert "idx_shareable_objects_tenant_object" in indexes
    assert "idx_shareable_objects_owner_created" in indexes
    assert "idx_shareable_objects_expires_at" in indexes


def test_sqlite_constraints_enforce_parents_and_visibility(m0197) -> None:
    conn = sqlite3.connect(":memory:")
    _bootstrap_parent_schema(conn)
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-a', 'Tenant A')")
    conn.execute("INSERT INTO users (id, tenant_id) VALUES ('u-a', 't-a')")
    conn.executescript(m0197._SQLITE_CREATE_TABLE)

    insert_sql = """
        INSERT INTO shareable_objects (
            share_id, object_kind, object_id, tenant_id, owner_user_id,
            visibility
        ) VALUES (
            :share_id, 'block', 'b-a', :tenant_id, :owner_user_id,
            :visibility
        )
    """
    conn.execute(
        insert_sql,
        {
            "share_id": "sh-ok",
            "tenant_id": "t-a",
            "owner_user_id": "u-a",
            "visibility": "private",
        },
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            insert_sql,
            {
                "share_id": "sh-bad-visibility",
                "tenant_id": "t-a",
                "owner_user_id": "u-a",
                "visibility": "external",
            },
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            insert_sql,
            {
                "share_id": "sh-missing-tenant",
                "tenant_id": "t-missing",
                "owner_user_id": "u-a",
                "visibility": "team",
            },
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            insert_sql,
            {
                "share_id": "sh-missing-user",
                "tenant_id": "t-a",
                "owner_user_id": "u-missing",
                "visibility": "tenant",
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

    def test_shareable_objects_in_tables_in_order(self) -> None:
        mig = self._load_migrator()
        assert "shareable_objects" in mig.TABLES_IN_ORDER

    def test_shareable_objects_not_in_identity_id_set(self) -> None:
        mig = self._load_migrator()
        assert "shareable_objects" not in mig.TABLES_WITH_IDENTITY_ID

    def test_shareable_objects_replays_after_tenants_and_users(self) -> None:
        mig = self._load_migrator()
        order = list(mig.TABLES_IN_ORDER)
        assert order.index("tenants") < order.index("shareable_objects")
        assert order.index("users") < order.index("shareable_objects")
