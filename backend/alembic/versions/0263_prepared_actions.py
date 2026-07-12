"""OP-2629 U6-0 T9/T10 G2b — durable prepared actions (DORMANT).

Add the write-once ``prepared_actions`` store that the later T9/T10 guard and
ExecutionService leaves will write and read. Each row contains the canonical
operation identity, executable arguments, human rendering, and binding digest.
The explicit tenant-scoped UNIQUE key is the composite-FK anchor for later
challenge and grant tables.

PostgreSQL is authoritative. SQLite gets its deliberate subset from
``backend/db.py::_SCHEMA`` because the dev SQLite path does not run alembic.

Additive and dormant: no producer or consumer is wired by this revision.
Rollback drops the table and its dependent index.

Revision ID: 0263
Revises: 0262
Create Date: 2026-07-13
"""
from __future__ import annotations

from alembic import op


revision = "0263"
down_revision = "0262"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS prepared_actions (
            action_instance_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            principal_type TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            request_id TEXT NOT NULL DEFAULT '',
            model_call_id TEXT NOT NULL DEFAULT '',
            adapter_namespace TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            family TEXT NOT NULL,
            effect TEXT NOT NULL
                CHECK (effect IN ('read_only', 'mutating')),
            canonical_target TEXT NOT NULL DEFAULT '',
            args_hash TEXT NOT NULL,
            provenance_kind TEXT NOT NULL
                CHECK (provenance_kind IN ('model', 'no_model_input')),
            model_snapshot_id TEXT,
            no_model_input_source TEXT,
            executable_args JSONB NOT NULL,
            human_rendering JSONB NOT NULL DEFAULT '{}'::jsonb,
            prepared_action_digest TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_prepared_actions_tenant_instance
                UNIQUE (tenant_id, action_instance_id),
            CONSTRAINT ck_prepared_actions_provenance_xor CHECK (
                (provenance_kind = 'model'
                 AND model_snapshot_id IS NOT NULL
                 AND model_call_id <> ''
                 AND no_model_input_source IS NULL)
             OR (provenance_kind = 'no_model_input'
                 AND no_model_input_source IS NOT NULL
                 AND model_call_id = ''
                 AND model_snapshot_id IS NULL)
            )
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_prepared_actions_tenant_digest "
        "ON prepared_actions (tenant_id, prepared_action_digest)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_prepared_actions_tenant_digest")
    conn.exec_driver_sql("DROP TABLE IF EXISTS prepared_actions")
