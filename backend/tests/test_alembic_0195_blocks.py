"""WP.1.2 -- alembic 0195 ``blocks`` contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0195 = BACKEND_ROOT / "alembic" / "versions" / "0195_blocks.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_revision_id_and_parent_are_declared() -> None:
    source = MIGRATION_0195.read_text()
    assert 'revision = "0195"' in source
    assert 'down_revision = "0194"' in source


def test_required_columns_present_and_typed_for_pg() -> None:
    m0195 = _load_module(MIGRATION_0195, "_alembic_test_0195_pg")
    required = (
        "block_id",
        "parent_id",
        "tenant_id",
        "user_id",
        "project_id",
        "session_id",
        "kind",
        "status",
        "title",
        "payload",
        "metadata",
        "redaction_mask",
        "started_at",
        "completed_at",
        "created_at",
    )

    for col in required:
        assert col in m0195._PG_CREATE_TABLE, f"PG branch missing {col}"

    assert "block_id       TEXT PRIMARY KEY" in m0195._PG_CREATE_TABLE
    assert "parent_id      TEXT REFERENCES blocks(block_id)" in (
        m0195._PG_CREATE_TABLE
    )
    assert "tenant_id      TEXT NOT NULL" in m0195._PG_CREATE_TABLE
    assert "payload        JSONB NOT NULL DEFAULT '{}'::jsonb" in (
        m0195._PG_CREATE_TABLE
    )
    assert "metadata       JSONB NOT NULL DEFAULT '{}'::jsonb" in (
        m0195._PG_CREATE_TABLE
    )
    assert "redaction_mask JSONB NOT NULL DEFAULT '{}'::jsonb" in (
        m0195._PG_CREATE_TABLE
    )
    assert "created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()" in (
        m0195._PG_CREATE_TABLE
    )


def test_indexes_declared() -> None:
    m0195 = _load_module(MIGRATION_0195, "_alembic_test_0195_indexes")

    assert "idx_blocks_tenant_session" in m0195._INDEX_TENANT_SESSION
    assert "ON blocks(tenant_id, session_id, started_at DESC)" in (
        m0195._INDEX_TENANT_SESSION
    )
    assert "idx_blocks_parent" in m0195._INDEX_PARENT
    assert "ON blocks(parent_id)" in m0195._INDEX_PARENT


def test_sqlite_upgrade_creates_dev_parity_table_and_indexes() -> None:
    m0195 = _load_module(MIGRATION_0195, "_alembic_test_0195_sqlite")
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(m0195._SQLITE_CREATE_TABLE)
    conn.execute(m0195._INDEX_TENANT_SESSION)
    conn.execute(m0195._INDEX_PARENT)

    columns = {
        row[1]: row[2]
        for row in conn.execute("PRAGMA table_info(blocks)")
    }
    assert columns == {
        "block_id": "TEXT",
        "parent_id": "TEXT",
        "tenant_id": "TEXT",
        "user_id": "TEXT",
        "project_id": "TEXT",
        "session_id": "TEXT",
        "kind": "TEXT",
        "status": "TEXT",
        "title": "TEXT",
        "payload": "TEXT",
        "metadata": "TEXT",
        "redaction_mask": "TEXT",
        "started_at": "TEXT",
        "completed_at": "TEXT",
        "created_at": "TEXT",
    }
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(blocks)")
    }
    assert "idx_blocks_tenant_session" in indexes
    assert "idx_blocks_parent" in indexes


def test_sqlite_parent_fk_blocks_missing_parent() -> None:
    m0195 = _load_module(MIGRATION_0195, "_alembic_test_0195_fk")
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(m0195._SQLITE_CREATE_TABLE)

    try:
        conn.execute(
            """
            INSERT INTO blocks (
                block_id, parent_id, tenant_id, kind, status
            ) VALUES (
                'b-child', 'b-missing', 't-1', 'command', 'running'
            )
            """
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("blocks.parent_id must enforce the self-FK")


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

    def test_blocks_in_tables_in_order(self) -> None:
        mig = self._load_migrator()
        assert "blocks" in mig.TABLES_IN_ORDER

    def test_blocks_not_in_identity_id_set(self) -> None:
        mig = self._load_migrator()
        assert "blocks" not in mig.TABLES_WITH_IDENTITY_ID

    def test_blocks_replays_after_tenants(self) -> None:
        mig = self._load_migrator()
        order = list(mig.TABLES_IN_ORDER)
        assert order.index("tenants") < order.index("blocks")
