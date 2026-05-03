"""SC.10.1 -- ``dsar_requests`` table.

Persistent queue for data-subject access, erasure, and portability
requests.  This row deliberately lands only the schema; SC.10.2 through
SC.10.5 own the routes, export payloads, erasure orchestration, email
templates, and SLA worker.

* ``id`` -- app-generated request id (``dsar-*`` convention expected by
  later SC.10 routes).  TEXT PK follows tenant_invites / git_accounts /
  llm_credentials and avoids sequence-reset work during SQLite -> PG
  cutover.
* ``tenant_id`` -- tenant scope for the request.  FK cascades on tenant
  delete so tenant teardown does not leave workflow rows that can never
  be acted on.
* ``user_id`` -- data subject.  FK cascades on user delete, mirroring
  the AS.2.2 ``oauth_tokens`` DSAR cleanup posture: user-owned
  regulatory work rows must not leave dangling user references.
* ``request_type`` -- one of ``access`` / ``erasure`` / ``portability``.
  These are exactly the three endpoint rows under SC.10.2-SC.10.4.
* ``status`` -- coarse workflow lifecycle for the later worker:
  ``pending`` / ``processing`` / ``completed`` / ``failed`` /
  ``cancelled``.  State transitions remain application-owned.
* ``requested_at`` / ``due_at`` -- epoch seconds.  ``due_at`` is caller-
  supplied so SC.10.5 can implement the 30-day SLA without dialect-
  specific date arithmetic in the schema.
* ``completed_at`` -- nullable epoch seconds for terminal outcomes.
* ``payload_json`` -- request metadata such as export filters or an
  operator note; never stores secrets.
* ``result_json`` -- structured completion metadata such as an export
  object key or erasure summary; default empty object until terminal.
* ``error`` -- short failure detail for operator triage; empty string
  on non-failed rows.
* ``version`` -- optimistic-lock counter, matching the J2/Q.7 convention
  used by other mutable workflow tables.

Indexes
-------
* ``idx_dsar_requests_user_status`` supports "show my DSAR requests"
  and user-owned workflow polling.
* ``idx_dsar_requests_tenant_due`` supports SC.10.5's SLA scan for
  pending / processing rows by tenant and due time.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every worker reads the same DDL state from PG / SQLite so the table is
visible atomically post-commit.  Answer #1 of SOP Step 1 -- every
worker reads the same DDL state from the same DB.

Read-after-write timing audit
-----------------------------
The CREATE TABLE happens inside the alembic upgrade transaction.
``scripts/deploy.sh`` closes the asyncpg pool before alembic upgrade
and reopens it after, so runtime workers never see a half-shaped
schema.  No concurrent writer exists during the migration window.

Production readiness gate
-------------------------
* No new Python / OS package -- production image needs no rebuild.
* New table added -- ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  is updated in this same row to include ``dsar_requests`` (replays
  after ``tenants`` and ``users`` because of the FKs).  TEXT PK, so the
  table is NOT in ``TABLES_WITH_IDENTITY_ID``.  The drift guard
  ``test_migrator_schema_coverage`` and the SC.10.1 contract test
  enforce this.
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (0063 -> 0064) is run
  against prod PG.  ``deployed-active`` requires SC.10.2-SC.10.5 routes
  and the SLA worker to start writing rows.

Revision ID: 0064
Revises: 0063
Create Date: 2026-05-03
"""
from __future__ import annotations

from alembic import op


revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


# -- Enum whitelists --------------------------------------------------------


_REQUEST_TYPES_SQL = "'access','erasure','portability'"
_STATUSES_SQL = "'cancelled','completed','failed','pending','processing'"


# -- PG branch --------------------------------------------------------------


_PG_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS dsar_requests (\n"
    "    id            TEXT PRIMARY KEY,\n"
    "    tenant_id     TEXT NOT NULL\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    user_id       TEXT NOT NULL\n"
    "                         REFERENCES users(id) ON DELETE CASCADE,\n"
    f"    request_type  TEXT NOT NULL\n"
    f"                         CHECK (request_type IN ({_REQUEST_TYPES_SQL})),\n"
    f"    status        TEXT NOT NULL DEFAULT 'pending'\n"
    f"                         CHECK (status IN ({_STATUSES_SQL})),\n"
    "    requested_at  DOUBLE PRECISION NOT NULL,\n"
    "    due_at        DOUBLE PRECISION NOT NULL,\n"
    "    completed_at  DOUBLE PRECISION,\n"
    "    payload_json  JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
    "    result_json   JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
    "    error         TEXT NOT NULL DEFAULT '',\n"
    "    version       INTEGER NOT NULL DEFAULT 0\n"
    ")"
)

_PG_INDEX_USER_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_dsar_requests_user_status "
    "ON dsar_requests(user_id, status)"
)

_PG_INDEX_TENANT_DUE = (
    "CREATE INDEX IF NOT EXISTS idx_dsar_requests_tenant_due "
    "ON dsar_requests(tenant_id, due_at) "
    "WHERE status IN ('pending', 'processing')"
)


# -- SQLite branch ----------------------------------------------------------


_SQLITE_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS dsar_requests (\n"
    "    id            TEXT PRIMARY KEY,\n"
    "    tenant_id     TEXT NOT NULL\n"
    "                         REFERENCES tenants(id) ON DELETE CASCADE,\n"
    "    user_id       TEXT NOT NULL\n"
    "                         REFERENCES users(id) ON DELETE CASCADE,\n"
    f"    request_type  TEXT NOT NULL\n"
    f"                         CHECK (request_type IN ({_REQUEST_TYPES_SQL})),\n"
    f"    status        TEXT NOT NULL DEFAULT 'pending'\n"
    f"                         CHECK (status IN ({_STATUSES_SQL})),\n"
    "    requested_at  REAL NOT NULL,\n"
    "    due_at        REAL NOT NULL,\n"
    "    completed_at  REAL,\n"
    "    payload_json  TEXT NOT NULL DEFAULT '{}',\n"
    "    result_json   TEXT NOT NULL DEFAULT '{}',\n"
    "    error         TEXT NOT NULL DEFAULT '',\n"
    "    version       INTEGER NOT NULL DEFAULT 0\n"
    ")"
)

_SQLITE_INDEX_USER_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_dsar_requests_user_status "
    "ON dsar_requests(user_id, status)"
)

_SQLITE_INDEX_TENANT_DUE = (
    "CREATE INDEX IF NOT EXISTS idx_dsar_requests_tenant_due "
    "ON dsar_requests(tenant_id, due_at) "
    "WHERE status IN ('pending', 'processing')"
)


# -- upgrade / downgrade ----------------------------------------------------


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(_PG_CREATE_TABLE)
        conn.exec_driver_sql(_PG_INDEX_USER_STATUS)
        conn.exec_driver_sql(_PG_INDEX_TENANT_DUE)
        return

    conn.exec_driver_sql(_SQLITE_CREATE_TABLE)
    conn.exec_driver_sql(_SQLITE_INDEX_USER_STATUS)
    conn.exec_driver_sql(_SQLITE_INDEX_TENANT_DUE)


def downgrade() -> None:
    conn = op.get_bind()
    for idx in (
        "idx_dsar_requests_tenant_due",
        "idx_dsar_requests_user_status",
    ):
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS {idx}")
    op.execute("DROP TABLE IF EXISTS dsar_requests")
