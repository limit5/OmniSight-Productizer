"""FX2.D9.7.11 -- allow HubSpot OAuth token rows.

HubSpot is now a supported self-login OAuth provider.  The runtime login
path only stores ``users.auth_methods = ["oauth_hubspot"]`` today, but
the AS.0.4 drift guards require the token-vault provider whitelist and
the ``oauth_tokens.provider`` CHECK clause to stay byte-aligned.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton, no in-memory cache.
Every worker reads the same provider CHECK from PG / SQLite once the
migration commits.  Answer #1 of SOP Step 1.

Read-after-write timing audit
-----------------------------
The CHECK replacement happens inside the alembic transaction.  No
runtime writer or read-after-write timing assumption changes in this row.

Production readiness gate
-------------------------
No new Python / OS package.  No new table.  Production status of this
commit: dev-only; next gate is deployed-inactive once alembic 0198 is
applied to production.

Revision ID: 0198
Revises: 0197
Create Date: 2026-05-06
"""
from __future__ import annotations

from alembic import op


revision = "0198"
down_revision = "0197"
branch_labels = None
depends_on = None


_PROVIDERS_WITH_HUBSPOT_SQL = (
    "'apple','bitbucket','discord','github','gitlab','google','hubspot',"
    "'microsoft','notion','salesforce','slack'"
)
_PROVIDERS_WITHOUT_HUBSPOT_SQL = (
    "'apple','bitbucket','discord','github','gitlab','google',"
    "'microsoft','notion','salesforce','slack'"
)


def _pg_replace_provider_check(conn, providers_sql: str) -> None:
    conn.exec_driver_sql(
        "ALTER TABLE oauth_tokens "
        "DROP CONSTRAINT IF EXISTS oauth_tokens_provider_check"
    )
    conn.exec_driver_sql(
        "ALTER TABLE oauth_tokens "
        "ADD CONSTRAINT oauth_tokens_provider_check "
        f"CHECK (provider IN ({providers_sql}))"
    )


def _sqlite_rebuild(conn, providers_sql: str) -> None:
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_oauth_tokens_key_version")
    conn.exec_driver_sql("DROP INDEX IF EXISTS idx_oauth_tokens_provider_expires")
    conn.exec_driver_sql(
        "CREATE TABLE oauth_tokens_v2 (\n"
        "    user_id            TEXT NOT NULL\n"
        "                            REFERENCES users(id) ON DELETE CASCADE,\n"
        "    provider           TEXT NOT NULL\n"
        f"                            CHECK (provider IN ({providers_sql})),\n"
        "    access_token_enc   TEXT NOT NULL DEFAULT '',\n"
        "    refresh_token_enc  TEXT NOT NULL DEFAULT '',\n"
        "    expires_at         REAL,\n"
        "    scope              TEXT NOT NULL DEFAULT '',\n"
        "    key_version        INTEGER NOT NULL DEFAULT 1,\n"
        "    created_at         REAL NOT NULL,\n"
        "    updated_at         REAL NOT NULL,\n"
        "    version            INTEGER NOT NULL DEFAULT 0,\n"
        "    PRIMARY KEY (user_id, provider)\n"
        ")"
    )
    conn.exec_driver_sql(
        "INSERT INTO oauth_tokens_v2 ("
        "user_id, provider, access_token_enc, refresh_token_enc, expires_at, "
        "scope, key_version, created_at, updated_at, version"
        ") SELECT "
        "user_id, provider, access_token_enc, refresh_token_enc, expires_at, "
        "scope, key_version, created_at, updated_at, version "
        "FROM oauth_tokens"
    )
    conn.exec_driver_sql("DROP TABLE oauth_tokens")
    conn.exec_driver_sql("ALTER TABLE oauth_tokens_v2 RENAME TO oauth_tokens")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_oauth_tokens_provider_expires "
        "ON oauth_tokens(provider, expires_at)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_oauth_tokens_key_version "
        "ON oauth_tokens(key_version)"
    )


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        _pg_replace_provider_check(conn, _PROVIDERS_WITH_HUBSPOT_SQL)
    else:
        _sqlite_rebuild(conn, _PROVIDERS_WITH_HUBSPOT_SQL)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        _pg_replace_provider_check(conn, _PROVIDERS_WITHOUT_HUBSPOT_SQL)
    else:
        _sqlite_rebuild(conn, _PROVIDERS_WITHOUT_HUBSPOT_SQL)
