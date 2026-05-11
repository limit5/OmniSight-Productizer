"""OP-881 D9 -- ``prod_deploy_audit`` table for the prod deploy orchestrator.

Append-only record of every prod deploy attempt routed through
:mod:`backend.orchestrator.prod_deploy`. One row per orchestration
attempt, keyed by ``release_id`` so a duplicate POST to
``/api/v1/prod/deploy`` with the same id is a no-op (AC #4 idempotence).

Why a dedicated table — and not a reuse of ``deploy_audit`` (0204)
-----------------------------------------------------------------
``deploy_audit`` is a hash-chained change-management compliance log
(D18 / OP-779). Its primary key is auto-increment and one *deploy*
typically produces several rows (started + succeeded + rollback +
operator_action) because the chain captures *every* event. That is the
right schema for compliance review but the wrong schema for the
orchestrator's idempotence check, which has to answer "have I already
finished release_id X?" in a single indexed lookup.

This table is the orchestrator's bookkeeping ledger:

* ``release_id`` UNIQUE so the idempotence path is a 1-row SELECT.
* ``status`` rolls forward through ``running`` → terminal state
  (``completed`` / ``aborted_*``) so the SSE stream and the JIRA
  comment can be reconstructed from the row.
* ``last_step`` records which orchestrated step had the last update,
  so a partial failure is recoverable from the operator runbook.

The change-management compliance trail (``deploy_audit``) is still
written from inside the orchestrator at the start / end of each
attempt — the two tables are complementary, not redundant.

Revision ID: 0224
Revises: 0221
Create Date: 2026-05-11
"""
from __future__ import annotations

from alembic import op


revision = "0224"
down_revision = "0221"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS prod_deploy_audit (
    id              BIGSERIAL PRIMARY KEY,
    release_id      TEXT NOT NULL UNIQUE,
    image_tag       TEXT NOT NULL,
    actor           TEXT NOT NULL,
    status          TEXT NOT NULL,
    last_step       TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    elapsed_seconds DOUBLE PRECISION,
    error_class     TEXT,
    error_message   TEXT,
    context_json    JSONB,
    CONSTRAINT prod_deploy_audit_status_chk
        CHECK (status IN (
            'running','completed',
            'aborted_approval_refused',
            'aborted_smoke_failed',
            'aborted_timeout',
            'aborted_error'
        )),
    CONSTRAINT prod_deploy_audit_step_chk
        CHECK (last_step IS NULL OR last_step IN (
            'approval','image_pull','secrets_decrypt',
            'smoke_pre_check','blue_green_switch','completed'
        ))
)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS prod_deploy_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id      TEXT NOT NULL UNIQUE,
    image_tag       TEXT NOT NULL,
    actor           TEXT NOT NULL,
    status          TEXT NOT NULL,
    last_step       TEXT,
    started_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at     TEXT,
    elapsed_seconds REAL,
    error_class     TEXT,
    error_message   TEXT,
    context_json    TEXT,
    CONSTRAINT prod_deploy_audit_status_chk
        CHECK (status IN (
            'running','completed',
            'aborted_approval_refused',
            'aborted_smoke_failed',
            'aborted_timeout',
            'aborted_error'
        )),
    CONSTRAINT prod_deploy_audit_step_chk
        CHECK (last_step IS NULL OR last_step IN (
            'approval','image_pull','secrets_decrypt',
            'smoke_pre_check','blue_green_switch','completed'
        ))
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL)
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_prod_deploy_audit_status_started "
        "ON prod_deploy_audit (status, started_at DESC)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_prod_deploy_audit_status_started")
    bind.exec_driver_sql("DROP TABLE IF EXISTS prod_deploy_audit")
