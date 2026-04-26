"""BS.2.4 — relax NOT NULL on catalog_entries override columns.

Why
───
0051 declared ``vendor``, ``family``, ``display_name``, ``version``,
and ``install_method`` as ``NOT NULL`` on every row source. But ADR
§3.2 specifies override rows as **partial diffs** layered on top of a
shipped base — a tenant who just wants to rename one column should be
able to insert an override row carrying only ``display_name`` plus
NULLs in the rest, and the resolver fills in the gaps from the shipped
row. The NOT NULL declaration contradicts that design.

BS.2.4's PG-live integration tests are the first writers that exercise
the partial-override path against a real PG (the BS.2.1/2.2/2.3 smoke
+ matrix tests are dep-shape only and never round-trip through the
DB). Two paths fail today:

* ``PATCH /catalog/entries/{id}`` creating a fresh override over a
  shipped base — ``_create_override_row`` writes only the fields the
  caller supplied, so unsupplied columns hit the NOT NULL.
* ``DELETE /catalog/entries/{id}`` against a shipped row creates a
  ``hidden=TRUE`` override tombstone with NO other columns set —
  same NOT NULL violation.

Fix
───
Drop ``NOT NULL`` from the five columns and add a row-level CHECK that
keeps the original invariant for shipped / operator / subscription
rows (every column populated) while allowing override rows to carry
arbitrary NULL subsets. The CHECK expresses the design literally:

* ``source = 'override'``: any of the five columns may be NULL —
  the resolver inherits from the base.
* ``source IN ('shipped', 'operator', 'subscription')``: every column
  must be populated — these rows are standalone.

SQLite parity follows the same pattern. PG branch uses
``ALTER COLUMN ... DROP NOT NULL`` + ``ADD CONSTRAINT``; SQLite has no
ALTER COLUMN support, so we rebuild the table preserving every row
under ``ALTER TABLE RENAME`` + ``CREATE TABLE`` + ``INSERT … SELECT``.
The rebuild path is mechanical and there is no SQLite production data
this migration can disturb (catalog tables are dev-only at the SQLite
layer per BS.1.4 follow-up).

Module-global / cross-worker state audit
────────────────────────────────────────
Pure DDL migration. No in-memory cache, no module-level singleton.
Every worker reads from the same PG, so the schema relaxation is
visible atomically post-commit. Answer #1 for the cross-worker
question — every worker reads the same DDL state from the same DB.

Read-after-write timing audit
─────────────────────────────
The new CHECK is evaluated per-row inside the same transaction as
the INSERT — there is no read-after-write window between the
constraint evaluation and the row landing. The two writers affected
(``_create_override_row`` + the DELETE path) hit the constraint
in the same connection that they are inserting on, so MVCC is moot.

Production readiness gate
─────────────────────────
* No new Python / OS package — production image needs no rebuild.
* No new schema artefact (the existing ``catalog_entries`` table is
  modified in place; ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  doesn't change).
* Production status of THIS commit: **dev-only**. Next gate:
  ``deployed-inactive`` once BS.2.4 lands and the alembic chain
  (0051 + 0052 + this) is run against prod PG.

Revision ID: 0053
Revises: 0052
Create Date: 2026-04-27
"""
from __future__ import annotations

from alembic import op


revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


# ─── PG branch ────────────────────────────────────────────────────────────


_PG_DROP_NOT_NULLS = (
    "ALTER TABLE catalog_entries ALTER COLUMN vendor         DROP NOT NULL",
    "ALTER TABLE catalog_entries ALTER COLUMN family         DROP NOT NULL",
    "ALTER TABLE catalog_entries ALTER COLUMN display_name   DROP NOT NULL",
    "ALTER TABLE catalog_entries ALTER COLUMN version        DROP NOT NULL",
    "ALTER TABLE catalog_entries ALTER COLUMN install_method DROP NOT NULL",
)


_PG_ADD_OVERRIDE_NULLABILITY_CHECK = (
    "ALTER TABLE catalog_entries ADD CONSTRAINT "
    "ck_catalog_entries_override_nullable_columns "
    "CHECK ("
    "  source = 'override' "
    "  OR ("
    "    vendor IS NOT NULL AND family IS NOT NULL "
    "    AND display_name IS NOT NULL AND version IS NOT NULL "
    "    AND install_method IS NOT NULL"
    "  )"
    ")"
)


_PG_DROP_OVERRIDE_NULLABILITY_CHECK = (
    "ALTER TABLE catalog_entries DROP CONSTRAINT IF EXISTS "
    "ck_catalog_entries_override_nullable_columns"
)


_PG_RESTORE_NOT_NULLS = (
    "ALTER TABLE catalog_entries ALTER COLUMN vendor         SET NOT NULL",
    "ALTER TABLE catalog_entries ALTER COLUMN family         SET NOT NULL",
    "ALTER TABLE catalog_entries ALTER COLUMN display_name   SET NOT NULL",
    "ALTER TABLE catalog_entries ALTER COLUMN version        SET NOT NULL",
    "ALTER TABLE catalog_entries ALTER COLUMN install_method SET NOT NULL",
)


# ─── SQLite branch ────────────────────────────────────────────────────────
#
# SQLite cannot ALTER COLUMN. Rebuild the table: rename old to backup,
# create the relaxed-schema table, copy every row, drop the backup.
# Indexes are recreated by referencing the same statements 0051 used.


_SQLITE_REBUILD_RELAX = """
CREATE TABLE catalog_entries_v2 (
    id              TEXT NOT NULL,
    source          TEXT NOT NULL,
    schema_version  INTEGER NOT NULL DEFAULT 1,
    tenant_id       TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    vendor          TEXT,
    family          TEXT,
    display_name    TEXT,
    version         TEXT,
    install_method  TEXT,
    install_url     TEXT,
    sha256          TEXT,
    size_bytes      INTEGER,
    depends_on      TEXT NOT NULL DEFAULT '[]',
    metadata        TEXT NOT NULL DEFAULT '{}',
    hidden          INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL DEFAULT (strftime('%s','now')),
    updated_at      REAL NOT NULL DEFAULT (strftime('%s','now')),
    CHECK (source IN ('shipped','operator','override','subscription')),
    CHECK (family IS NULL OR family IN ('mobile','embedded','web','software',
                                        'rtos','cross-toolchain','custom')),
    CHECK (install_method IS NULL OR install_method IN
           ('noop','docker_pull','shell_script','vendor_installer')),
    CHECK (hidden IN (0, 1)),
    CHECK (
        (source = 'shipped'  AND tenant_id IS NULL)
        OR
        (source IN ('operator','override','subscription')
            AND tenant_id IS NOT NULL)
    ),
    CHECK (
        source = 'override'
        OR (vendor IS NOT NULL AND family IS NOT NULL
            AND display_name IS NOT NULL AND version IS NOT NULL
            AND install_method IS NOT NULL)
    )
)
"""


_SQLITE_REBUILD_RESTORE = """
CREATE TABLE catalog_entries_v2 (
    id              TEXT NOT NULL,
    source          TEXT NOT NULL,
    schema_version  INTEGER NOT NULL DEFAULT 1,
    tenant_id       TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    vendor          TEXT NOT NULL,
    family          TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    version         TEXT NOT NULL,
    install_method  TEXT NOT NULL,
    install_url     TEXT,
    sha256          TEXT,
    size_bytes      INTEGER,
    depends_on      TEXT NOT NULL DEFAULT '[]',
    metadata        TEXT NOT NULL DEFAULT '{}',
    hidden          INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL DEFAULT (strftime('%s','now')),
    updated_at      REAL NOT NULL DEFAULT (strftime('%s','now')),
    CHECK (source IN ('shipped','operator','override','subscription')),
    CHECK (family IN ('mobile','embedded','web','software',
                      'rtos','cross-toolchain','custom')),
    CHECK (install_method IN ('noop','docker_pull',
                              'shell_script','vendor_installer')),
    CHECK (hidden IN (0, 1)),
    CHECK (
        (source = 'shipped'  AND tenant_id IS NULL)
        OR
        (source IN ('operator','override','subscription')
            AND tenant_id IS NOT NULL)
    )
)
"""


_SQLITE_INDEXES_RECREATE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_catalog_entries_visible "
    "ON catalog_entries(id, source, COALESCE(tenant_id, '')) "
    "WHERE hidden = 0",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entries_family "
    "ON catalog_entries(family)",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entries_tenant "
    "ON catalog_entries(tenant_id) "
    "WHERE tenant_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_catalog_entries_source "
    "ON catalog_entries(source)",
)


def _sqlite_rebuild(conn, target_ddl: str) -> None:
    """Common rebuild helper: backup → recreate (using *target_ddl*) →
    copy → drop backup → recreate indexes."""
    conn.exec_driver_sql(target_ddl)
    conn.exec_driver_sql(
        "INSERT INTO catalog_entries_v2 ("
        "  id, source, schema_version, tenant_id, vendor, family, "
        "  display_name, version, install_method, install_url, sha256, "
        "  size_bytes, depends_on, metadata, hidden, created_at, updated_at"
        ") SELECT "
        "  id, source, schema_version, tenant_id, vendor, family, "
        "  display_name, version, install_method, install_url, sha256, "
        "  size_bytes, depends_on, metadata, hidden, created_at, updated_at "
        "FROM catalog_entries"
    )
    conn.exec_driver_sql("DROP TABLE catalog_entries")
    conn.exec_driver_sql("ALTER TABLE catalog_entries_v2 RENAME TO catalog_entries")
    for stmt in _SQLITE_INDEXES_RECREATE:
        conn.exec_driver_sql(stmt)


# ─── upgrade / downgrade ──────────────────────────────────────────────────


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name
    if dialect == "postgresql":
        for stmt in _PG_DROP_NOT_NULLS:
            conn.exec_driver_sql(stmt)
        conn.exec_driver_sql(_PG_ADD_OVERRIDE_NULLABILITY_CHECK)
    else:
        _sqlite_rebuild(conn, _SQLITE_REBUILD_RELAX)


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name
    if dialect == "postgresql":
        # Defensive: any lingering override rows with NULL columns must
        # be cleaned up before SET NOT NULL fires; the upgrade path
        # only enables them after this migration runs, so a clean
        # downgrade has none. Production downgrades that keep override
        # rows past the relaxation will fail loudly here — that's the
        # intended signal that the data needs review.
        conn.exec_driver_sql(_PG_DROP_OVERRIDE_NULLABILITY_CHECK)
        for stmt in _PG_RESTORE_NOT_NULLS:
            conn.exec_driver_sql(stmt)
    else:
        _sqlite_rebuild(conn, _SQLITE_REBUILD_RESTORE)
