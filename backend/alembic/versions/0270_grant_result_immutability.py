"""OP-2642 U6-0 T9/T10 G5a-3 — immutable grants/results (DORMANT).

Freeze every issued action-grant field except ``state`` so later state-machine
transitions cannot rewrite the copied operation identity, recovery policy, or
idempotency key. Grant rows are not deletable, and execution results are
write-once at the database boundary.

PostgreSQL is authoritative. SQLite gets its deliberate subset from
``backend/db.py::_SCHEMA`` because the dev SQLite path does not run alembic.

Additive and dormant: no producer, consumer, or result/grant pairing constraint
is wired by this revision. Rollback removes the four triggers and three trigger
functions.

Revision ID: 0270
Revises: 0269
Create Date: 2026-07-13
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op


revision = "0270"
down_revision = "0269"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION action_grants_freeze_identity()
        RETURNS trigger AS $$
        BEGIN
            IF ROW(
                NEW.grant_id, NEW.tenant_id, NEW.challenge_id,
                NEW.action_instance_id, NEW.principal_type, NEW.actor_id,
                NEW.request_id, NEW.model_call_id, NEW.adapter_namespace,
                NEW.tool_name, NEW.schema_version, NEW.family,
                NEW.canonical_target, NEW.args_hash, NEW.provenance_kind,
                NEW.model_snapshot_id, NEW.no_model_input_source,
                NEW.prepared_action_digest, NEW.grant_issuer_source,
                NEW.idempotency_key, NEW.recovery_mode, NEW.created_at,
                NEW.expires_at
            ) IS DISTINCT FROM ROW(
                OLD.grant_id, OLD.tenant_id, OLD.challenge_id,
                OLD.action_instance_id, OLD.principal_type, OLD.actor_id,
                OLD.request_id, OLD.model_call_id, OLD.adapter_namespace,
                OLD.tool_name, OLD.schema_version, OLD.family,
                OLD.canonical_target, OLD.args_hash, OLD.provenance_kind,
                OLD.model_snapshot_id, OLD.no_model_input_source,
                OLD.prepared_action_digest, OLD.grant_issuer_source,
                OLD.idempotency_key, OLD.recovery_mode, OLD.created_at,
                OLD.expires_at
            ) THEN
                RAISE EXCEPTION
                    'ActionGrantImmutable: only action_grants.state may change post-issue';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    conn.exec_driver_sql(
        """
        DROP TRIGGER IF EXISTS trg_action_grants_freeze_identity
            ON action_grants;
        CREATE TRIGGER trg_action_grants_freeze_identity
            BEFORE UPDATE ON action_grants
            FOR EACH ROW EXECUTE FUNCTION action_grants_freeze_identity()
        """
    )
    conn.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION action_grants_block_delete()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'ActionGrantImmutable: action_grants rows are not deletable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    conn.exec_driver_sql(
        """
        DROP TRIGGER IF EXISTS trg_action_grants_no_delete
            ON action_grants;
        CREATE TRIGGER trg_action_grants_no_delete
            BEFORE DELETE ON action_grants
            FOR EACH ROW EXECUTE FUNCTION action_grants_block_delete()
        """
    )
    conn.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION execution_results_block_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'ExecutionResultImmutable: execution_results is write-once';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    conn.exec_driver_sql(
        """
        DROP TRIGGER IF EXISTS trg_execution_results_no_update
            ON execution_results;
        CREATE TRIGGER trg_execution_results_no_update
            BEFORE UPDATE ON execution_results
            FOR EACH ROW EXECUTE FUNCTION execution_results_block_mutation()
        """
    )
    conn.exec_driver_sql(
        """
        DROP TRIGGER IF EXISTS trg_execution_results_no_delete
            ON execution_results;
        CREATE TRIGGER trg_execution_results_no_delete
            BEFORE DELETE ON execution_results
            FOR EACH ROW EXECUTE FUNCTION execution_results_block_mutation()
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_execution_results_no_delete "
        "ON execution_results"
    )
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_execution_results_no_update "
        "ON execution_results"
    )
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_action_grants_no_delete "
        "ON action_grants"
    )
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_action_grants_freeze_identity "
        "ON action_grants"
    )
    conn.exec_driver_sql(
        "DROP FUNCTION IF EXISTS execution_results_block_mutation()"
    )
    conn.exec_driver_sql(
        "DROP FUNCTION IF EXISTS action_grants_block_delete()"
    )
    conn.exec_driver_sql(
        "DROP FUNCTION IF EXISTS action_grants_freeze_identity()"
    )
