"""I4: Tenant-scoped secrets — tenant_secrets table + api_keys.tenant_id.

Creates the ``tenant_secrets`` table for storing encrypted credentials
per tenant (git_credentials, provider_keys, cloudflare_tokens, etc.).
Also adds ``tenant_id`` to the ``api_keys`` table and backfills existing
rows to ``t-default``.

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-16
"""
from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

DEFAULT_TENANT_ID = "t-default"


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Create tenant_secrets table
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS tenant_secrets (
            id              TEXT PRIMARY KEY,
            tenant_id       TEXT NOT NULL DEFAULT 't-default' REFERENCES tenants(id),
            secret_type     TEXT NOT NULL,
            key_name        TEXT NOT NULL,
            encrypted_value TEXT NOT NULL,
            metadata        TEXT NOT NULL DEFAULT '{}',
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (tenant_id, secret_type, key_name)
        )
    """)
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_tenant_secrets_tenant "
        "ON tenant_secrets(tenant_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_tenant_secrets_type "
        "ON tenant_secrets(tenant_id, secret_type)"
    )

    # 2. Add tenant_id to api_keys
    cols = {
        row[1]
        for row in conn.exec_driver_sql("PRAGMA table_info(api_keys)").fetchall()
    }
    if "tenant_id" not in cols:
        conn.exec_driver_sql(
            f"ALTER TABLE api_keys ADD COLUMN tenant_id TEXT DEFAULT '{DEFAULT_TENANT_ID}'"
        )
        conn.exec_driver_sql(
            "UPDATE api_keys SET tenant_id = ? WHERE tenant_id IS NULL",
            (DEFAULT_TENANT_ID,),
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant_id)"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_secrets")
