"""BP.Q.4 -- alembic 0186 ``embedding_chunks`` contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0186 = BACKEND_ROOT / "alembic" / "versions" / "0186_embedding_chunks.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _bootstrap_parent_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE tenants (
            id TEXT PRIMARY KEY
        );
        """
    )


def test_revision_id_and_parent_are_declared() -> None:
    source = MIGRATION_0186.read_text()
    assert 'revision = "0186"' in source
    assert 'down_revision = "0192"' in source


def test_pg_branch_uses_pgvector_jsonb_and_hnsw_index() -> None:
    m0186 = _load_module(MIGRATION_0186, "_alembic_test_0186_pg")

    assert "embedding   vector NOT NULL" in m0186._PG_CREATE_TABLE
    assert "metadata    JSONB NOT NULL DEFAULT '{}'::jsonb" in (
        m0186._PG_CREATE_TABLE
    )
    assert "REFERENCES tenants(id) ON DELETE CASCADE" in m0186._PG_CREATE_TABLE
    assert "idx_embedding_chunks_tenant_source" in m0186._INDEX_TENANT_SOURCE
    assert "USING hnsw (embedding vector_cosine_ops)" in (
        m0186._PG_INDEX_EMBEDDING_HNSW
    )


def test_sqlite_upgrade_creates_dev_parity_table_and_index() -> None:
    m0186 = _load_module(MIGRATION_0186, "_alembic_test_0186_sqlite")
    conn = sqlite3.connect(":memory:")
    _bootstrap_parent_schema(conn)
    conn.executescript(m0186._SQLITE_CREATE_TABLE)
    conn.execute(m0186._INDEX_TENANT_SOURCE)

    columns = {
        row[1]: row[2]
        for row in conn.execute("PRAGMA table_info(embedding_chunks)")
    }
    assert columns == {
        "chunk_id": "TEXT",
        "tenant_id": "TEXT",
        "source_path": "TEXT",
        "chunk_text": "TEXT",
        "embedding": "TEXT",
        "metadata": "TEXT",
        "created_at": "TEXT",
    }
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(embedding_chunks)")
    }
    assert "idx_embedding_chunks_tenant_source" in indexes


def test_sqlite_tenant_delete_cascades_chunks() -> None:
    m0186 = _load_module(MIGRATION_0186, "_alembic_test_0186_cascade")
    conn = sqlite3.connect(":memory:")
    _bootstrap_parent_schema(conn)
    conn.executescript(m0186._SQLITE_CREATE_TABLE)
    conn.execute("INSERT INTO tenants(id) VALUES ('t-1')")
    conn.execute(
        """
        INSERT INTO embedding_chunks (
            chunk_id, tenant_id, source_path, chunk_text, embedding
        ) VALUES ('chunk-1', 't-1', 'docs/rag.md', 'install pgvector', '[0.1]')
        """
    )

    conn.execute("DELETE FROM tenants WHERE id='t-1'")

    assert conn.execute("SELECT COUNT(*) FROM embedding_chunks").fetchone() == (0,)


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

    def test_embedding_chunks_in_tables_in_order(self) -> None:
        mig = self._load_migrator()
        assert "embedding_chunks" in mig.TABLES_IN_ORDER

    def test_embedding_chunks_not_in_identity_id_set(self) -> None:
        mig = self._load_migrator()
        assert "embedding_chunks" not in mig.TABLES_WITH_IDENTITY_ID

    def test_embedding_chunks_replays_after_tenants(self) -> None:
        mig = self._load_migrator()
        order = list(mig.TABLES_IN_ORDER)
        assert order.index("tenants") < order.index("embedding_chunks")
