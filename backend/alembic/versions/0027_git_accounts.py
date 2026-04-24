"""Phase 5-1 (#multi-account-forge) — git_accounts table.

Lays the schema for the multi-account credential model that replaces
the legacy single-account ``Settings.{github,gitlab}_token{,_map}``
JSON-blob fields. One row per account, scoped to a tenant, with the
PAT / SSH key / webhook secret stored as Fernet ciphertext via
``backend.secret_store``.

Why a new table (vs. extending ``tenant_secrets``)
──────────────────────────────────────────────────
``tenant_secrets`` is a generic ``(tenant_id, secret_type, key_name,
encrypted_value)`` KV store. Forge accounts have richer structure
than KV: the "credential" is a tuple of (PAT, SSH key, webhook
secret, ssh_host, ssh_port, project, url_patterns, is_default flag,
last_used_at, ...). Forcing all of that through KV would mean either
six ``tenant_secrets`` rows per account (with ad-hoc naming
conventions to glue them) OR one row with a giant JSON-blob value
(fully opaque to SQL queries). Both options break the URL-pattern
resolver's ``WHERE platform=$1 AND tenant_id=$2 ORDER BY
last_used_at DESC`` fast path. A dedicated table is the right shape.

See ``docs/phase-5-multi-account/01-design.md`` for the full design
write-up — column-by-column rationale, partial-index reasoning,
why optimistic-lock ``version`` is added at table-create time, why
``url_patterns`` is JSONB list (not comma-split string), why FK
``ON DELETE CASCADE`` matches ``tenant_secrets``, etc.

This row ships ONLY the schema + drift guards. No CRUD, no
resolver swap, no UI, no call-site sweep — those are rows
5-2 through 5-11.

Revision ID: 0027
Revises: 0026
Create Date: 2026-04-24
"""
from __future__ import annotations

from alembic import op


revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        # PG path. JSONB for ``url_patterns`` + ``metadata`` (the
        # resolver in row 5-3 will use ``->>`` / ``@>`` operators);
        # BOOLEAN for the flag columns; DOUBLE PRECISION epoch
        # seconds for timestamps (matches sessions / chat_messages
        # convention so ``time.time()`` flows straight through).
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS git_accounts (
                id                       TEXT PRIMARY KEY,
                tenant_id                TEXT NOT NULL DEFAULT 't-default'
                                                REFERENCES tenants(id) ON DELETE CASCADE,
                platform                 TEXT NOT NULL
                                                CHECK (platform IN ('github','gitlab','gerrit','jira')),
                instance_url             TEXT NOT NULL DEFAULT '',
                label                    TEXT NOT NULL DEFAULT '',
                username                 TEXT NOT NULL DEFAULT '',
                encrypted_token          TEXT NOT NULL DEFAULT '',
                encrypted_ssh_key        TEXT NOT NULL DEFAULT '',
                ssh_host                 TEXT NOT NULL DEFAULT '',
                ssh_port                 INTEGER NOT NULL DEFAULT 0,
                project                  TEXT NOT NULL DEFAULT '',
                encrypted_webhook_secret TEXT NOT NULL DEFAULT '',
                url_patterns             JSONB NOT NULL DEFAULT '[]'::jsonb,
                auth_type                TEXT NOT NULL DEFAULT 'pat',
                is_default               BOOLEAN NOT NULL DEFAULT FALSE,
                enabled                  BOOLEAN NOT NULL DEFAULT TRUE,
                metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,
                last_used_at             DOUBLE PRECISION,
                created_at               DOUBLE PRECISION NOT NULL,
                updated_at               DOUBLE PRECISION NOT NULL,
                version                  INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_git_accounts_tenant "
            "ON git_accounts(tenant_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_git_accounts_tenant_platform "
            "ON git_accounts(tenant_id, platform)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_git_accounts_last_used "
            "ON git_accounts(tenant_id, last_used_at DESC NULLS LAST)"
        )
        # Partial unique index — at most one row per (tenant, platform)
        # may have ``is_default = TRUE``. Enforced at the database
        # layer so two concurrent UPDATEs that both try to flip the
        # default flag get a clean unique-violation on the loser
        # rather than racing past application-level guards
        # (lesson from SP-4.6 ``tenant_secrets.upsert_secret``).
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_git_accounts_default_per_platform "
            "ON git_accounts(tenant_id, platform) "
            "WHERE is_default = TRUE"
        )
    else:
        # SQLite dev parity. JSONB → TEXT-of-JSON; BOOLEAN → INTEGER
        # 0/1; partial indexes only since 3.8 — supported on the
        # dev SQLite versions in CI but the app layer also enforces
        # the "one default per (tenant, platform)" invariant on write
        # so the partial index is belt+braces, not load-bearing.
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS git_accounts (
                id                       TEXT PRIMARY KEY,
                tenant_id                TEXT NOT NULL DEFAULT 't-default'
                                                REFERENCES tenants(id) ON DELETE CASCADE,
                platform                 TEXT NOT NULL
                                                CHECK (platform IN ('github','gitlab','gerrit','jira')),
                instance_url             TEXT NOT NULL DEFAULT '',
                label                    TEXT NOT NULL DEFAULT '',
                username                 TEXT NOT NULL DEFAULT '',
                encrypted_token          TEXT NOT NULL DEFAULT '',
                encrypted_ssh_key        TEXT NOT NULL DEFAULT '',
                ssh_host                 TEXT NOT NULL DEFAULT '',
                ssh_port                 INTEGER NOT NULL DEFAULT 0,
                project                  TEXT NOT NULL DEFAULT '',
                encrypted_webhook_secret TEXT NOT NULL DEFAULT '',
                url_patterns             TEXT NOT NULL DEFAULT '[]',
                auth_type                TEXT NOT NULL DEFAULT 'pat',
                is_default               INTEGER NOT NULL DEFAULT 0,
                enabled                  INTEGER NOT NULL DEFAULT 1,
                metadata                 TEXT NOT NULL DEFAULT '{}',
                last_used_at             REAL,
                created_at               REAL NOT NULL,
                updated_at               REAL NOT NULL,
                version                  INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_git_accounts_tenant "
            "ON git_accounts(tenant_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_git_accounts_tenant_platform "
            "ON git_accounts(tenant_id, platform)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_git_accounts_last_used "
            "ON git_accounts(tenant_id, last_used_at DESC)"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_git_accounts_default_per_platform "
            "ON git_accounts(tenant_id, platform) "
            "WHERE is_default = 1"
        )


def downgrade() -> None:
    # Safe drop — until rows 5-2 / 5-5 land, this table is empty.
    # After legacy auto-migration ships (row 5-5), operators must
    # back up ``git_accounts`` before downgrading or the credential
    # rows are lost. This downgrade does not attempt to fold the
    # rows back into the legacy ``Settings`` JSON map fields
    # (asymmetric migration is a deliberate Phase-5 design choice).
    op.execute("DROP TABLE IF EXISTS git_accounts")
