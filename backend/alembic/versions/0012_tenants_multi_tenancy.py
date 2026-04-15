"""I1: Multi-tenancy foundation — tenants table + tenant_id on business tables.

Creates the `tenants` table, inserts the default tenant `t-default`,
adds `tenant_id` columns to all business tables, and backfills
existing rows to belong to the default tenant.

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-16
"""
from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

DEFAULT_TENANT_ID = "t-default"

TABLES_NEEDING_TENANT_ID = [
    "users",
    "workflow_runs",
    "debug_findings",
    "decision_rules",
    "event_log",
    "audit_log",
    "artifacts",
    "user_preferences",
]


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Create tenants table
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS tenants (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            plan        TEXT NOT NULL DEFAULT 'free',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            enabled     INTEGER NOT NULL DEFAULT 1
        )
    """)

    # 2. Insert default tenant (idempotent)
    conn.exec_driver_sql(
        "INSERT OR IGNORE INTO tenants (id, name, plan) VALUES (?, ?, ?)",
        (DEFAULT_TENANT_ID, "Default Tenant", "free"),
    )

    # 3. Add tenant_id column to each business table and backfill
    for table in TABLES_NEEDING_TENANT_ID:
        # Check if column already exists
        cols = {
            row[1]
            for row in conn.exec_driver_sql(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        if "tenant_id" in cols:
            continue

        # user_preferences has a composite PK — SQLite can't add NOT NULL
        # columns with ALTER TABLE, so we add as nullable then backfill then
        # we rely on application-level enforcement.
        conn.exec_driver_sql(
            f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT DEFAULT '{DEFAULT_TENANT_ID}'"
        )
        conn.exec_driver_sql(
            f"UPDATE {table} SET tenant_id = ? WHERE tenant_id IS NULL",
            (DEFAULT_TENANT_ID,),
        )
        conn.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table}(tenant_id)"
        )


def downgrade() -> None:
    # SQLite doesn't support DROP COLUMN before 3.35.0; for safety we
    # only drop the tenants table. The tenant_id columns remain but are
    # harmless (unused after downgrade).
    op.execute("DROP TABLE IF EXISTS tenants")
