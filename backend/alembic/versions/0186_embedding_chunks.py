"""BP.Q.4 -- ``embedding_chunks`` table.

Persistent pgvector-backed chunk store for the internal Knowledge
Retrieval layer landed in BP.Q.1-BP.Q.3.  This row lands only the
schema; indexing, embedding, and agent tool routing remain owned by the
existing RAG runtime modules and follow-up BP.Q rows.

* ``chunk_id`` -- app-generated stable chunk id used by incremental
  indexers for upsert/delete.
* ``tenant_id`` -- tenant scope for retrieval isolation.  Tenant
  deletion cascades indexed chunks so teardown cannot leave orphaned
  embeddings.
* ``source_path`` -- workspace-relative path used for citations and
  path-scoped deletes.
* ``chunk_text`` -- raw text payload returned to the agentic tool.
* ``embedding`` -- pgvector dense embedding.  SQLite stores TEXT-of-
  vector for dev parity only; production semantic search is PG-only.
* ``metadata`` -- JSONB caller/indexer metadata such as line ranges.
* ``created_at`` -- ingestion timestamp for stale-index debugging.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every worker reads the same DDL state from PG / SQLite so the table is
visible atomically post-commit.  Answer #1 of SOP Step 1 -- every
worker reads the same DDL state from the same DB.

Read-after-write timing audit
-----------------------------
The CREATE TABLE happens inside the alembic upgrade transaction.
Runtime writers are not introduced in this row, so no downstream
read-after-write timing expectation changes.

Production readiness gate
-------------------------
* No new Python / OS package -- production image needs no rebuild.
  PostgreSQL deployments must enable the existing ``vector`` extension;
  this migration runs ``CREATE EXTENSION IF NOT EXISTS vector``.
* New table added -- ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  is updated in this same row to include ``embedding_chunks`` after
  ``tenants``.  TEXT PK, so the table is NOT in
  ``TABLES_WITH_IDENTITY_ID``.  The drift guard
  ``test_migrator_schema_coverage`` and the BP.Q.4 contract test
  enforce this.
* SQLite dev parity -- :mod:`backend.db`'s ``_SCHEMA`` constant receives
  the same table shape with pgvector/JSONB downgraded to TEXT so fresh
  dev SQLite DBs and the migrator drift guard see the table before
  runtime code starts writing.
* BP.Q.6 tenant isolation: PG enables and forces RLS on
  ``embedding_chunks``.  Runtime code must set
  ``omnisight.tenant_id`` for each tenant-scoped operation; missing
  settings deny reads/writes and the app-level query path still keeps
  an explicit ``tenant_id = $N`` filter.
* KS.1 review note: BP.Q.6 does not envelope-encrypt raw embeddings.
  Chunk text remains the sensitive retrieval payload and is isolated by
  tenant FK + RLS + query-time filters; if KS later encrypts chunk text
  at rest, embeddings should be regenerated from decrypted plaintext
  inside the tenant scope rather than stored as reversible secrets.
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (0192 -> 0186) is run
  against prod PG with pgvector available.

Revision ID: 0186
Revises: 0192
Create Date: 2026-05-05
"""
from __future__ import annotations

from alembic import op


revision = "0186"
down_revision = "0192"
branch_labels = None
depends_on = None


_PG_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS embedding_chunks (\n"
    "    chunk_id    TEXT PRIMARY KEY,\n"
    "    tenant_id   TEXT NOT NULL\n"
    "                     REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    source_path TEXT NOT NULL,\n"
    "    chunk_text  TEXT NOT NULL,\n"
    "    embedding   vector NOT NULL,\n"
    "    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
    "    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
    ")"
)

_SQLITE_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS embedding_chunks (\n"
    "    chunk_id    TEXT PRIMARY KEY,\n"
    "    tenant_id   TEXT NOT NULL\n"
    "                     REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    source_path TEXT NOT NULL,\n"
    "    chunk_text  TEXT NOT NULL,\n"
    "    embedding   TEXT NOT NULL,\n"
    "    metadata    TEXT NOT NULL DEFAULT '{}',\n"
    "    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n"
    ")"
)

_INDEX_TENANT_SOURCE = (
    "CREATE INDEX IF NOT EXISTS idx_embedding_chunks_tenant_source "
    "ON embedding_chunks(tenant_id, source_path)"
)

_PG_INDEX_EMBEDDING_HNSW = (
    "CREATE INDEX IF NOT EXISTS idx_embedding_chunks_embedding_hnsw "
    "ON embedding_chunks USING hnsw (embedding vector_cosine_ops)"
)

_PG_ENABLE_RLS = (
    "ALTER TABLE embedding_chunks ENABLE ROW LEVEL SECURITY"
)

_PG_FORCE_RLS = (
    "ALTER TABLE embedding_chunks FORCE ROW LEVEL SECURITY"
)

_PG_CREATE_TENANT_POLICY = (
    "CREATE POLICY embedding_chunks_tenant_isolation "
    "ON embedding_chunks "
    "USING (tenant_id = current_setting('omnisight.tenant_id', true)) "
    "WITH CHECK (tenant_id = current_setting('omnisight.tenant_id', true))"
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        conn.exec_driver_sql(_PG_CREATE_TABLE)
        conn.exec_driver_sql(_INDEX_TENANT_SOURCE)
        conn.exec_driver_sql(_PG_INDEX_EMBEDDING_HNSW)
        conn.exec_driver_sql(_PG_ENABLE_RLS)
        conn.exec_driver_sql(_PG_FORCE_RLS)
        conn.exec_driver_sql(
            "DROP POLICY IF EXISTS embedding_chunks_tenant_isolation "
            "ON embedding_chunks"
        )
        conn.exec_driver_sql(_PG_CREATE_TENANT_POLICY)
    else:
        conn.exec_driver_sql(_SQLITE_CREATE_TABLE)
        conn.exec_driver_sql(_INDEX_TENANT_SOURCE)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(
            "DROP POLICY IF EXISTS embedding_chunks_tenant_isolation "
            "ON embedding_chunks"
        )
        conn.exec_driver_sql(
            "ALTER TABLE embedding_chunks DISABLE ROW LEVEL SECURITY"
        )
        conn.exec_driver_sql("DROP INDEX IF EXISTS idx_embedding_chunks_embedding_hnsw")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_embedding_chunks_tenant_source")
    op.execute("DROP TABLE IF EXISTS embedding_chunks")
