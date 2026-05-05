"""BP.M.1 -- ``auto_distilled_skills`` table.

Persistent review queue for L1 Skill Auto-Distillation outputs.  This
row lands only the schema; BP.M.2 owns trajectory summarisation, BP.M.3
owns REST review/promote endpoints, and BP.M.5 owns audit_log events.

* ``id`` -- app-generated skill draft id.  TEXT PK follows the existing
  workflow/review queue convention and avoids sequence-reset work during
  SQLite -> PG cutover.
* ``tenant_id`` -- tenant scope for review isolation.  Tenant deletion
  cascades drafts so teardown cannot leave orphaned generated skills.
* ``skill_name`` -- proposed production skill name, reviewed by humans
  before promotion.
* ``source_task_id`` -- originating task trajectory.  Nullable FK with
  ``ON DELETE SET NULL`` keeps skill drafts readable even if task rows
  are later pruned.
* ``markdown_content`` -- distilled skill body awaiting review.
* ``version`` -- optimistic-lock counter for review/promote updates.
* ``status`` -- human gate lifecycle: ``draft`` / ``reviewed`` /
  ``promoted``.  State transitions remain application-owned.
* ``created_at`` -- creation timestamp for recent-first review queues.

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
* New table added -- ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  is updated in this same row to include ``auto_distilled_skills`` after
  ``tasks``.  TEXT PK, so the table is NOT in
  ``TABLES_WITH_IDENTITY_ID``.  The drift guard
  ``test_migrator_schema_coverage`` and the BP.M.1 contract test
  enforce this.
* SQLite dev parity -- :mod:`backend.db`'s ``_SCHEMA`` constant receives
  the same CREATE TABLE / index shape so fresh dev SQLite DBs and the
  migrator drift guard see the table before runtime code starts writing.
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (0191 -> 0192) is run
  against prod PG.  ``deployed-active`` requires BP.M.2-BP.M.5 runtime
  entries to write and audit distillation / promotion events.

Revision ID: 0192
Revises: 0191
Create Date: 2026-05-05
"""
from __future__ import annotations

from alembic import op


revision = "0192"
down_revision = "0191"
branch_labels = None
depends_on = None


_STATUSES_SQL = "'draft','promoted','reviewed'"


_PG_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS auto_distilled_skills (\n"
    "    id               TEXT PRIMARY KEY,\n"
    "    tenant_id        TEXT NOT NULL\n"
    "                          REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    skill_name       TEXT NOT NULL,\n"
    "    source_task_id   TEXT\n"
    "                          REFERENCES tasks(id) ON DELETE SET NULL,\n"
    "    markdown_content TEXT NOT NULL,\n"
    "    version          INTEGER NOT NULL DEFAULT 1,\n"
    "    status           TEXT NOT NULL DEFAULT 'draft'\n"
    f"                          CHECK (status IN ({_STATUSES_SQL})),\n"
    "    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
    ")"
)

_SQLITE_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS auto_distilled_skills (\n"
    "    id               TEXT PRIMARY KEY,\n"
    "    tenant_id        TEXT NOT NULL\n"
    "                          REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    skill_name       TEXT NOT NULL,\n"
    "    source_task_id   TEXT\n"
    "                          REFERENCES tasks(id) ON DELETE SET NULL,\n"
    "    markdown_content TEXT NOT NULL,\n"
    "    version          INTEGER NOT NULL DEFAULT 1,\n"
    "    status           TEXT NOT NULL DEFAULT 'draft'\n"
    f"                          CHECK (status IN ({_STATUSES_SQL})),\n"
    "    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n"
    ")"
)

_INDEX_TENANT_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_auto_distilled_skills_tenant_status "
    "ON auto_distilled_skills(tenant_id, status)"
)

_INDEX_SOURCE_TASK = (
    "CREATE INDEX IF NOT EXISTS idx_auto_distilled_skills_source_task "
    "ON auto_distilled_skills(source_task_id)"
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(_PG_CREATE_TABLE)
    else:
        conn.exec_driver_sql(_SQLITE_CREATE_TABLE)
    conn.exec_driver_sql(_INDEX_TENANT_STATUS)
    conn.exec_driver_sql(_INDEX_SOURCE_TASK)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_auto_distilled_skills_source_task")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_auto_distilled_skills_tenant_status")
    op.execute("DROP TABLE IF EXISTS auto_distilled_skills")
