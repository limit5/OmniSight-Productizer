"""Phase 5b-1 (#llm-credentials) — llm_credentials table.

Lays the schema for the multi-account LLM credential model that
replaces the legacy in-memory-only ``Settings.{anthropic,google,
openai,xai,groq,deepseek,together,openrouter}_api_key`` scalar fields.
Today those keys are read from ``.env`` into ``Settings`` at boot and
only ever live in one process's memory — `PUT /runtime/settings` can
override them but the override evaporates on every rolling restart /
``docker compose restart backend-*`` cycle.

The right model — established by Phase 5 for forge credentials — is
one row per (tenant, provider) account with the PAT / API key stored
as Fernet ciphertext via ``backend.secret_store``. A dedicated table
rather than extending ``tenant_secrets`` for the same reasons Phase
5-1 cited: richer structure (``base_url`` / ``org_id`` / ``scopes``
per account, ``is_default`` flag, ``last_used_at`` LRU column), and
a first-class ``ORDER BY last_used_at DESC`` fast path on the tenant
+ provider index.

Schema parallels ``git_accounts`` (alembic 0027) column-for-column —
intentionally copy-paste-with-domain-rename per the Phase 5b-1 spec:
"可沿用 Phase 5-1 的 ``git_accounts`` 結構複製貼上改 domain 名,
省重設計時間". Differences from ``git_accounts``:

- ``provider`` replaces ``platform``; CHECK constraint lists the 9
  providers from ``backend.agents.llm.list_providers()`` (the 8
  keyed ones + ``ollama`` so its ``base_url`` can live in this table
  via ``metadata`` even though ``encrypted_value`` is empty).
- Single ``encrypted_value`` column replaces the three ``encrypted_
  {token,ssh_key,webhook_secret}`` columns (LLM credential is a
  single API key — no SSH, no webhook secret to gate HMAC).
- No ``ssh_host`` / ``ssh_port`` / ``project`` columns (not
  meaningful for LLM providers).
- No ``url_patterns`` column — provider routing is per-(tenant,
  provider) keyed, not per-URL like forge credentials which route
  by repo URL glob.
- ``metadata JSONB`` carries ``base_url`` (self-hosted OpenAI-
  compatible gateways, ``http://ai_engine:11434`` for ollama),
  ``org_id`` (OpenAI org scoping), ``scopes`` (future OAuth),
  ``model_overrides`` (per-account model allowlist), ``notes``.
- ``auth_type`` kept at the column level for symmetry with the
  Phase 5-12 OAuth-prep hook (``pat`` | ``oauth``); OAuth for
  LLM providers is out of scope for MVP but the column reservation
  costs nothing and keeps 5b symmetric with 5.
- ``version INTEGER NOT NULL DEFAULT 0`` day-1 optimistic-lock guard
  — matches the J2 / Q.7 lineage and lets row 5b-3's ``PATCH``
  rotate-key flow use ``If-Match`` without a follow-up migration.

See ``docs/phase-5b-llm-credentials/01-design.md`` for the full
column-by-column rationale, partial-index reasoning, why this is
symmetric with but not shared with ``git_accounts`` / ``tenant_
secrets``, etc.

This row ships ONLY the schema + drift guards. No CRUD, no
resolver swap, no UI, no call-site sweep — those are rows
5b-2 through 5b-6.

Why migration number 0029 not 0020
──────────────────────────────────
Row header title says "alembic 0020" but the live alembic head is
already well past 0020 (current live head = 0028, see row 5-12).
TODO row titles were drafted weeks ago when 0019 / 0020 were the
next free numbers; by the time the row is implemented the number
has advanced. ``down_revision`` is the load-bearing field and it
correctly chains after 0028_git_accounts_code_verifier.

Revision ID: 0029
Revises: 0028
Create Date: 2026-04-24
"""
from __future__ import annotations

from alembic import op


revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        # PG path. JSONB for ``metadata`` (the resolver in row 5b-2
        # will use ``->>`` operators against ``base_url`` / ``org_id``
        # etc); BOOLEAN for the flag columns; DOUBLE PRECISION epoch
        # seconds for timestamps (matches sessions / chat_messages /
        # git_accounts convention so ``time.time()`` flows straight
        # through).
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS llm_credentials (
                id                TEXT PRIMARY KEY,
                tenant_id         TEXT NOT NULL DEFAULT 't-default'
                                        REFERENCES tenants(id) ON DELETE CASCADE,
                provider          TEXT NOT NULL
                                        CHECK (provider IN (
                                            'anthropic','google','openai','xai',
                                            'groq','deepseek','together',
                                            'openrouter','ollama'
                                        )),
                label             TEXT NOT NULL DEFAULT '',
                encrypted_value   TEXT NOT NULL DEFAULT '',
                metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
                auth_type         TEXT NOT NULL DEFAULT 'pat',
                is_default        BOOLEAN NOT NULL DEFAULT FALSE,
                enabled           BOOLEAN NOT NULL DEFAULT TRUE,
                last_used_at      DOUBLE PRECISION,
                created_at        DOUBLE PRECISION NOT NULL,
                updated_at        DOUBLE PRECISION NOT NULL,
                version           INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_llm_credentials_tenant "
            "ON llm_credentials(tenant_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_llm_credentials_tenant_provider "
            "ON llm_credentials(tenant_id, provider)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_llm_credentials_last_used "
            "ON llm_credentials(tenant_id, last_used_at DESC NULLS LAST)"
        )
        # Partial unique index — at most one row per (tenant, provider)
        # may have ``is_default = TRUE``. Enforced at the database
        # layer so two concurrent UPDATEs that both try to flip the
        # default flag get a clean unique-violation on the loser
        # rather than racing past application-level guards
        # (lesson from SP-4.6 ``tenant_secrets.upsert_secret`` and
        # row 5-1 ``git_accounts``).
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_credentials_default_per_provider "
            "ON llm_credentials(tenant_id, provider) "
            "WHERE is_default = TRUE"
        )
    else:
        # SQLite dev parity. JSONB → TEXT-of-JSON; BOOLEAN → INTEGER
        # 0/1; partial indexes only since 3.8 — supported on the
        # dev SQLite versions in CI but the app layer also enforces
        # the "one default per (tenant, provider)" invariant on write
        # so the partial index is belt+braces, not load-bearing.
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS llm_credentials (
                id                TEXT PRIMARY KEY,
                tenant_id         TEXT NOT NULL DEFAULT 't-default'
                                        REFERENCES tenants(id) ON DELETE CASCADE,
                provider          TEXT NOT NULL
                                        CHECK (provider IN (
                                            'anthropic','google','openai','xai',
                                            'groq','deepseek','together',
                                            'openrouter','ollama'
                                        )),
                label             TEXT NOT NULL DEFAULT '',
                encrypted_value   TEXT NOT NULL DEFAULT '',
                metadata          TEXT NOT NULL DEFAULT '{}',
                auth_type         TEXT NOT NULL DEFAULT 'pat',
                is_default        INTEGER NOT NULL DEFAULT 0,
                enabled           INTEGER NOT NULL DEFAULT 1,
                last_used_at      REAL,
                created_at        REAL NOT NULL,
                updated_at        REAL NOT NULL,
                version           INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_llm_credentials_tenant "
            "ON llm_credentials(tenant_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_llm_credentials_tenant_provider "
            "ON llm_credentials(tenant_id, provider)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_llm_credentials_last_used "
            "ON llm_credentials(tenant_id, last_used_at DESC)"
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_credentials_default_per_provider "
            "ON llm_credentials(tenant_id, provider) "
            "WHERE is_default = 1"
        )


def downgrade() -> None:
    # Safe drop — until rows 5b-2 / 5b-5 land, this table is empty.
    # After legacy auto-migration ships (row 5b-5), operators must
    # back up ``llm_credentials`` before downgrading or the
    # credential rows are lost. This downgrade does not attempt to
    # fold the rows back into the legacy ``Settings`` scalar fields
    # (asymmetric migration is a deliberate Phase-5b design choice,
    # same as Phase 5-1 / 5-12).
    op.execute("DROP TABLE IF EXISTS llm_credentials")
