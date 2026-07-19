"""OP-2699 U6-4 — L3 per-user erasable semantic-fact store (DORMANT).

The greenfield L3 store (design §2.D / §3 L3 — NOT the U4 append-only global
ledger). Each row is one governed per-user fact: the semantic key
``(fact_type, subject, predicate)`` is CLEAR (queryable for supersession /
one-current-value / contradiction), the VALUE is SEALED (``sealed_ciphertext`` +
``dek_ref`` — U6-0b crypto-shred, so "delete my memory" = destroy the dek_ref ⇒
the ciphertext is unrecoverable even from a backup). Only content-free metadata
survives an erase (the ``l3_erasure_audit`` tombstone: counts + timestamps).

Isolation = explicit ``tenant_id = $ AND user_id = $`` predicates on EVERY query
(PRIMARY + always-live) + FORCED row-level security keyed on the per-transaction
session settings ``app.tenant_id`` / ``app.user_id`` (DEFENSE-IN-DEPTH). NOTE(ops):
PostgreSQL never applies RLS to a SUPERUSER — so the RLS layer only bites once the
app connects as a NON-SUPERUSER role. The current prod app connects as a superuser
(boreas-A1), so the APP PREDICATES are the sole LIVE isolation until the app role
is de-superuser-ed; RLS is verified correct (tested under a non-superuser role) and
waits for that deploy change. Do NOT drop an app predicate trusting the RLS
backstop. The design flags migration 0021 as having tenant columns but NO RLS.

PostgreSQL is authoritative + owns RLS. SQLite gets a deliberate no-RLS subset from
``backend/db.py::_SCHEMA`` (SQLite has no RLS; dev isolation is explicit app
predicates only). Additive + dormant: no producer/consumer is wired (U6-5 writes,
U6-6 confirms, U6-7 reads). Rollback drops policies, indexes, tables.

Revision ID: 0273
Revises: 0272
Create Date: 2026-07-19
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op

revision = "0273"
down_revision = "0272"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS l3_facts (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id),
            user_id TEXT NOT NULL,
            fact_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            sealed_ciphertext TEXT NOT NULL,
            dek_ref JSONB NOT NULL,
            sensitivity TEXT NOT NULL DEFAULT 'normal'
                CHECK (sensitivity IN ('normal', 'sensitive')),
            valid_from TEXT,
            valid_until TEXT,
            state TEXT NOT NULL DEFAULT 'quarantined'
                CHECK (state IN ('quarantined', 'promoted', 'superseded', 'rejected')),
            revision INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # one live value per (user, semantic key) — enforced only for promoted rows.
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_l3_facts_one_current "
        "ON l3_facts (tenant_id, user_id, fact_type, subject, predicate) "
        "WHERE state = 'promoted'"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_l3_facts_user "
        "ON l3_facts (tenant_id, user_id, state)"
    )

    # content-free erasure tombstone: counts + timestamps + a one-way PSEUDONYM of
    # the scope (``user_ref``) — NEVER the raw user_id and NEVER content.
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS l3_erasure_audit (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_ref TEXT NOT NULL,
            fact_count INTEGER NOT NULL,
            erased_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # REAL, FORCED row-level security keyed on per-txn session settings.
    conn.exec_driver_sql("ALTER TABLE l3_facts ENABLE ROW LEVEL SECURITY")
    conn.exec_driver_sql("ALTER TABLE l3_facts FORCE ROW LEVEL SECURITY")
    conn.exec_driver_sql("DROP POLICY IF EXISTS l3_facts_scope ON l3_facts")
    conn.exec_driver_sql(
        """
        CREATE POLICY l3_facts_scope ON l3_facts
            USING (tenant_id = current_setting('app.tenant_id', true)
                   AND user_id = current_setting('app.user_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)
                       AND user_id = current_setting('app.user_id', true))
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql("DROP POLICY IF EXISTS l3_facts_scope ON l3_facts")
    conn.exec_driver_sql("DROP INDEX IF EXISTS uq_l3_facts_one_current")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_l3_facts_user")
    conn.exec_driver_sql("DROP TABLE IF EXISTS l3_facts")
    conn.exec_driver_sql("DROP TABLE IF EXISTS l3_erasure_audit")
