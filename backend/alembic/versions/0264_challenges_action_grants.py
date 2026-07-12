"""OP-2634 U6-0 T9/T10 G4a-1 — challenges and action grants (DORMANT).

Add the write-once ``challenges`` and ``action_grants`` substrate that later
confirmation and ExecutionService leaves will transition and claim.  Both
rows copy the complete operation identity from their prepared action; grants
also bind model provenance to a durable snapshot through a composite foreign
key.

PostgreSQL is authoritative. SQLite gets its deliberate subset from
``backend/db.py::_SCHEMA`` because the dev SQLite path does not run alembic.

Additive and dormant: no producer or consumer is wired by this revision.
Rollback drops grants before challenges to respect their foreign key.

Revision ID: 0264
Revises: 0263
Create Date: 2026-07-13
"""
from __future__ import annotations

from alembic import op


revision = "0264"
down_revision = "0263"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS challenges (
            challenge_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            action_instance_id TEXT NOT NULL,
            principal_type TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            request_id TEXT NOT NULL DEFAULT '',
            model_call_id TEXT NOT NULL DEFAULT '',
            adapter_namespace TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            family TEXT NOT NULL,
            canonical_target TEXT NOT NULL DEFAULT '',
            args_hash TEXT NOT NULL,
            provenance_kind TEXT NOT NULL
                CHECK (provenance_kind IN ('model', 'no_model_input')),
            model_snapshot_id TEXT,
            no_model_input_source TEXT,
            prepared_action_digest TEXT NOT NULL,
            confirmer_actor TEXT,
            confirmer_principal_type TEXT,
            confirmed_at TIMESTAMPTZ,
            confirm_reason TEXT,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'confirmed', 'rejected', 'expired')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_challenges_tenant_instance
                UNIQUE (tenant_id, action_instance_id),
            CONSTRAINT uq_challenges_tenant_challenge
                UNIQUE (tenant_id, challenge_id),
            CONSTRAINT fk_challenges_prepared_action
                FOREIGN KEY (tenant_id, action_instance_id)
                REFERENCES prepared_actions (tenant_id, action_instance_id),
            CONSTRAINT ck_challenges_provenance_xor CHECK (
                (provenance_kind = 'model'
                 AND model_snapshot_id IS NOT NULL
                 AND model_call_id <> ''
                 AND no_model_input_source IS NULL)
             OR (provenance_kind = 'no_model_input'
                 AND no_model_input_source IS NOT NULL
                 AND model_call_id = ''
                 AND model_snapshot_id IS NULL)
            ),
            CONSTRAINT ck_challenges_expiry CHECK (expires_at > created_at)
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_challenges_tenant_state "
        "ON challenges (tenant_id, state)"
    )
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS action_grants (
            grant_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            challenge_id TEXT NOT NULL,
            action_instance_id TEXT NOT NULL,
            principal_type TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            request_id TEXT NOT NULL DEFAULT '',
            model_call_id TEXT NOT NULL DEFAULT '',
            adapter_namespace TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            family TEXT NOT NULL,
            canonical_target TEXT NOT NULL DEFAULT '',
            args_hash TEXT NOT NULL,
            provenance_kind TEXT NOT NULL
                CHECK (provenance_kind IN ('model', 'no_model_input')),
            model_snapshot_id TEXT,
            no_model_input_source TEXT,
            prepared_action_digest TEXT NOT NULL,
            grant_issuer_source TEXT NOT NULL
                CHECK (grant_issuer_source IN ('ui_confirm', 'slash_command')),
            idempotency_key TEXT,
            recovery_mode TEXT NOT NULL DEFAULT 'non_replayable'
                CHECK (recovery_mode IN (
                    'non_replayable', 'sink_idempotency_key', 'read_after_write'
                )),
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'executing', 'consumed', 'expired')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_grants_tenant_challenge
                UNIQUE (tenant_id, challenge_id),
            CONSTRAINT uq_grants_tenant_instance
                UNIQUE (tenant_id, action_instance_id),
            CONSTRAINT uq_grants_tenant_grant
                UNIQUE (tenant_id, grant_id),
            CONSTRAINT fk_grants_prepared_action
                FOREIGN KEY (tenant_id, action_instance_id)
                REFERENCES prepared_actions (tenant_id, action_instance_id),
            CONSTRAINT fk_grants_challenge
                FOREIGN KEY (tenant_id, challenge_id)
                REFERENCES challenges (tenant_id, challenge_id),
            CONSTRAINT fk_grants_snapshot
                FOREIGN KEY (tenant_id, model_snapshot_id)
                REFERENCES provenance_snapshots (tenant_id, snapshot_id),
            CONSTRAINT ck_grants_provenance_xor CHECK (
                (provenance_kind = 'model'
                 AND model_snapshot_id IS NOT NULL
                 AND model_call_id <> ''
                 AND no_model_input_source IS NULL)
             OR (provenance_kind = 'no_model_input'
                 AND no_model_input_source IS NOT NULL
                 AND model_call_id = ''
                 AND model_snapshot_id IS NULL)
            ),
            CONSTRAINT ck_grants_expiry CHECK (expires_at > created_at)
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_grants_tenant_state "
        "ON action_grants (tenant_id, state)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_grants_tenant_state")
    conn.exec_driver_sql("DROP TABLE IF EXISTS action_grants")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_challenges_tenant_state")
    conn.exec_driver_sql("DROP TABLE IF EXISTS challenges")
