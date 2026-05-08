"""OP-779 D18 -- ``deploy_audit`` table for change-management compliance.

Append-only audit log for production deploy / rollback / SLO breach /
operator actions. Each row is hash-chained to its predecessor so a
later tamper attempt can be detected by re-walking the chain. Unlike
the per-tenant chain in :mod:`backend.audit`, this table is global
(deploys do not belong to a tenant) so the chain is single-linear.

Schema rationale
----------------
* ``kind`` is a small enum-as-text constrained by CHECK so a typo in a
  caller cannot fork the audit stream into a new event type silently.
  The four values cover the change-management compliance surface
  required by the ticket spec (deploy, rollback, slo_breach,
  operator_action).
* ``reason`` is nullable at the DDL layer because system events
  (e.g. ``slo_breach``) do not carry an operator reason; the
  application layer (``backend.deploy_audit.record``) enforces that
  *operator-initiated* prod deploys carry a non-empty reason. Keeping
  the DDL permissive avoids a write failure for system rows that
  don't have a human in the loop.
* ``prev_hash`` / ``curr_hash`` form a SHA-256 chain
  (``curr_hash = sha256(prev_hash || canonical(row))``) which lets
  ``deploy_audit.verify_chain()`` detect post-hoc edits to historical
  rows. The genesis row uses ``"0" * 64`` as ``prev_hash``.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Writers (the FastAPI release-approval router + SLO breach detector +
manual CLI rollbacks) all funnel through ``deploy_audit.record``,
which serialises chain-append via PG advisory lock on the table name.

Read-after-write timing audit
-----------------------------
``record`` commits the INSERT before returning, so the next ``query``
(report endpoint, CSV export) sees the row immediately.

Production readiness gate
-------------------------
No new Python / OS package. New table only -- existing rows untouched.
The CHECK constraints are enforced both on PG and on SQLite (for
tests). Indexes on ``(ts DESC)`` and ``(kind, ts DESC)`` cover the
two query shapes the report path uses (1y window scan + per-kind
filter).

Revision ID: 0204
Revises: 0203
Create Date: 2026-05-08
"""
from __future__ import annotations

from alembic import op


revision = "0204"
down_revision = "0203"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS deploy_audit (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind            TEXT NOT NULL,
    tag             TEXT,
    actor           TEXT,
    reason          TEXT,
    status          TEXT NOT NULL,
    elapsed_seconds DOUBLE PRECISION,
    context         TEXT,
    prev_hash       TEXT NOT NULL,
    curr_hash       TEXT NOT NULL,
    CONSTRAINT deploy_audit_kind_chk
        CHECK (kind IN ('deploy','rollback','slo_breach','operator_action')),
    CONSTRAINT deploy_audit_status_chk
        CHECK (status IN ('started','succeeded','failed'))
)
"""


_PG_INDEX_TS = """
CREATE INDEX IF NOT EXISTS idx_deploy_audit_ts
    ON deploy_audit (ts DESC)
"""


_PG_INDEX_KIND_TS = """
CREATE INDEX IF NOT EXISTS idx_deploy_audit_kind_ts
    ON deploy_audit (kind, ts DESC)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS deploy_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    kind            TEXT NOT NULL,
    tag             TEXT,
    actor           TEXT,
    reason          TEXT,
    status          TEXT NOT NULL,
    elapsed_seconds REAL,
    context         TEXT,
    prev_hash       TEXT NOT NULL,
    curr_hash       TEXT NOT NULL,
    CONSTRAINT deploy_audit_kind_chk
        CHECK (kind IN ('deploy','rollback','slo_breach','operator_action')),
    CONSTRAINT deploy_audit_status_chk
        CHECK (status IN ('started','succeeded','failed'))
)
"""


_SQLITE_INDEX_TS = """
CREATE INDEX IF NOT EXISTS idx_deploy_audit_ts
    ON deploy_audit (ts DESC)
"""


_SQLITE_INDEX_KIND_TS = """
CREATE INDEX IF NOT EXISTS idx_deploy_audit_kind_ts
    ON deploy_audit (kind, ts DESC)
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_DDL)
        bind.exec_driver_sql(_PG_INDEX_TS)
        bind.exec_driver_sql(_PG_INDEX_KIND_TS)
    else:
        bind.exec_driver_sql(_SQLITE_DDL)
        bind.exec_driver_sql(_SQLITE_INDEX_TS)
        bind.exec_driver_sql(_SQLITE_INDEX_KIND_TS)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_deploy_audit_kind_ts")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_deploy_audit_ts")
    bind.exec_driver_sql("DROP TABLE IF EXISTS deploy_audit")
