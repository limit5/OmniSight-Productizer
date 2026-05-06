"""AS.2.2 — ``oauth_tokens`` table for the OAuth credential vault.

Lays the persistent home for the OAuth ``access_token`` /
``refresh_token`` ciphertext that the AS.2.1 token vault
(:mod:`backend.security.token_vault`) produces.  The vault is the
*only* approved entry-point for this table's ``access_token_enc`` /
``refresh_token_enc`` round-trip — every router that touches a stored
OAuth credential MUST go through
:func:`backend.security.token_vault.encrypt_for_user` /
:func:`~backend.security.token_vault.decrypt_for_user`.

Column rationale
────────────────

* ``user_id`` (TEXT, FK → ``users(id)`` ON DELETE CASCADE):
  the OAuth credential is per-user; the takeover-prevention guard
  (AS.0.3 / 0058) makes the binding (user → method) auditable, the
  cascade keeps GDPR / DSAR delete-user paths from leaving stranded
  ciphertext rows.

* ``provider`` (TEXT, CHECK ∈ ``{google, github, apple, microsoft, discord, gitlab, bitbucket, slack, notion}``):
  must byte-equal :data:`backend.security.token_vault.SUPPORTED_PROVIDERS`
  and :data:`backend.account_linking._AS1_OAUTH_PROVIDERS` — the
  cross-module drift guard tests in
  ``backend/tests/test_alembic_0057_oauth_tokens.py`` and
  ``backend/tests/test_token_vault.py`` fail red when the three
  diverge.  Adding a new provider is a 3-PR rule (vault catalog +
  helper whitelist + this CHECK constraint).

* ``access_token_enc`` (TEXT, default ``''``):  the Fernet ciphertext
  emitted by :func:`token_vault.encrypt_for_user`.  Stored as the
  urlsafe-base64 ASCII string; the ``''`` default is for the brief
  window between row INSERT and the first token-exchange response
  (AS.6.1 OAuth router will never read this default — it sets the
  column in the same UPSERT statement that creates the row).  Why
  default-empty instead of NOT NULL with no default: the empty string
  is unambiguously "no ciphertext yet" (the vault never emits an empty
  ciphertext — Fernet output is at least 73 chars), and it lets test
  fixtures pre-create rows without having to manufacture a stub
  ciphertext.

* ``refresh_token_enc`` (TEXT, default ``''``):  same shape; some IdPs
  (notably Apple sign-in for non-first-time logins) do not return a
  refresh token at all, so the empty-string default doubles as
  "this provider didn't issue one".  AS.2.4 refresh-hook treats
  ``refresh_token_enc = ''`` as "cannot auto-refresh; fall back to
  re-prompting the user".

* ``expires_at`` (DOUBLE PRECISION / REAL, NULLable):  POSIX epoch
  seconds when the access token stops being accepted by the IdP.
  NULLable because some IdPs (legacy GitHub PATs masquerading as
  OAuth) issue tokens with no documented expiry.  AS.2.4 refresh
  hook scans ``WHERE expires_at IS NOT NULL AND expires_at - 60 <
  extract(epoch from now())`` so the index ``(provider, expires_at)``
  below is shaped to that query.

* ``scope`` (TEXT, default ``''``):  space-separated OAuth scope string
  the IdP granted.  Persisted verbatim from the token-exchange
  response (the AS.1 OAuth client normalises whitespace).  Empty
  default for the same "no row content yet" rationale as the cipher
  columns.

* ``key_version`` (INTEGER, default 1):  reserved for the future KMS
  rotation roadmap (AS.0.4 §3.1 #4).  Today the vault writes /
  reads only ``KEY_VERSION_CURRENT = 1``; any other value on read
  raises :class:`backend.security.token_vault.UnknownKeyVersionError`.
  The first KMS migration will introduce ``2`` and a dual-read
  fallback; the column existing day-1 means that landing won't
  require a backfill.

* ``created_at`` / ``updated_at`` (DOUBLE PRECISION / REAL, NOT NULL):
  POSIX epoch seconds; matches ``git_accounts`` / ``llm_credentials``
  convention so :func:`time.time` flows straight through.  Caller
  must set both at INSERT and bump ``updated_at`` on every UPDATE
  (the AS.6.1 router will own this via a small helper).

* ``version`` (INTEGER, default 0):  optimistic-lock counter — same
  J2 / Q.7 lineage every other table in this codebase ships day-1.
  AS.2.4 refresh hook uses ``If-Match`` against this column to keep
  two concurrent refresh attempts from clobbering each other.

Why a composite ``(user_id, provider)`` PK and not a TEXT id
─────────────────────────────────────────────────────────────
``git_accounts`` and ``llm_credentials`` use ``id TEXT PRIMARY KEY``
because they explicitly support multiple labelled accounts per
``(tenant, provider)`` pair (different forge personas, different
LLM API keys).  OAuth login is the opposite: at most one binding
per ``(user, provider)`` — re-linking the same provider just rotates
the ciphertext on the existing row.  A composite PK enforces that
"one row per pair" invariant at the database layer instead of via
a separate UNIQUE index, and lets ``(user_id, ?)`` lookups (every
endpoint that owns a single user's tokens) hit the PK index for
free.

Index choices
─────────────

* Composite PK ``(user_id, provider)`` doubles as an index for
  per-user lookups (left-prefix scan).

* Secondary ``idx_oauth_tokens_provider_expires`` on
  ``(provider, expires_at)`` shapes the AS.2.4 refresh-hook scan
  ("for provider X, find tokens that expire in the next 60 s").
  ``NULLS LAST`` on PG so NULL-expiry rows sort to the end of the
  range scan and the hook can stop early.

* Secondary ``idx_oauth_tokens_key_version`` on ``(key_version)``
  shapes the KS.1.4 lazy re-encrypt scanner. After the quarterly
  master-KEK schedule advances, background workers can scan
  ``WHERE key_version < current_version`` without a full table walk.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure DDL migration — no module-level state, no in-memory cache.
Every worker reads the same DDL state from PG / SQLite so the table
is visible atomically post-commit.  **Answer #1** of SOP §1 — every
worker reads the same DDL state from the same DB.

Read-after-write timing audit
─────────────────────────────
The CREATE TABLE happens inside the alembic upgrade transaction.
``scripts/deploy.sh`` closes the asyncpg pool before alembic upgrade
and reopens it after, so runtime workers never see a half-shaped
schema.  No concurrent writer exists during the migration window.

Production readiness gate
─────────────────────────
* No new Python / OS package — production image needs no rebuild.
  The migration uses only SQL primitives already supported by the
  asyncpg + psycopg2 baseline (Phase-3 G4) and aiosqlite.
* New table added — ``scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER``
  is updated in this same row to include ``oauth_tokens`` (replays
  AFTER ``users`` because of the FK).  PK is composite TEXT so the
  table is NOT in ``TABLES_WITH_IDENTITY_ID``.  The drift guard
  ``test_migrator_schema_coverage`` enforces this.
* Production status of THIS commit: **dev-only**.  Next gate:
  ``deployed-inactive`` once the alembic chain (… → 0058 → 0057)
  is run against prod PG.  ``deployed-active`` requires the AS.6.1
  OAuth router (separate row) to start writing rows.

Chain ordering
──────────────
``down_revision = "0058"`` — this row chains AFTER the AS.0.3
``users.auth_methods`` migration even though the numerical IDs
suggest the opposite ordering.  Rationale: 0058 (auth_methods
column) was authored knowing 0057 (oauth_tokens table) would land
later and explicitly skipped the 0057 slot in its ``down_revision``
chain (see 0058's docstring + the
``test_alembic_0058_users_auth_methods.test_down_revision_is_0056``
contract test).  Inserting 0057 between 0056 and 0058 would force
both that test and any dev DB already at 0058 to break; appending
after 0058 keeps the existing chain stable and matches the same
"jump-over" pattern 0056 used to skip the unused 0055 slot.

Revision ID: 0057
Revises: 0058
Create Date: 2026-04-28
"""
from __future__ import annotations

from alembic import op


revision = "0057"
down_revision = "0058"
branch_labels = None
depends_on = None


# ─── Provider whitelist ──────────────────────────────────────────────────
# MUST byte-equal ``backend.security.token_vault.SUPPORTED_PROVIDERS`` and
# ``backend.account_linking._AS1_OAUTH_PROVIDERS``.  Sorted alphabetically
# so the CHECK clause is reproducible across both dialects and the
# drift-guard test can string-match.
_PROVIDERS_SQL = "'apple','bitbucket','discord','github','gitlab','google','microsoft','notion','slack'"


# ─── PG branch ───────────────────────────────────────────────────────────


# JSONB / BOOLEAN / DOUBLE PRECISION conventions match the
# ``llm_credentials`` (alembic 0029) parent template.  ``IF NOT EXISTS``
# is belt+braces against operator hand-creation out of band.
_PG_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS oauth_tokens (\n"
    "    user_id            TEXT NOT NULL\n"
    "                            REFERENCES users(id) ON DELETE CASCADE,\n"
    f"    provider           TEXT NOT NULL\n"
    f"                            CHECK (provider IN ({_PROVIDERS_SQL})),\n"
    "    access_token_enc   TEXT NOT NULL DEFAULT '',\n"
    "    refresh_token_enc  TEXT NOT NULL DEFAULT '',\n"
    "    expires_at         DOUBLE PRECISION,\n"
    "    scope              TEXT NOT NULL DEFAULT '',\n"
    "    key_version        INTEGER NOT NULL DEFAULT 1,\n"
    "    created_at         DOUBLE PRECISION NOT NULL,\n"
    "    updated_at         DOUBLE PRECISION NOT NULL,\n"
    "    version            INTEGER NOT NULL DEFAULT 0,\n"
    "    PRIMARY KEY (user_id, provider)\n"
    ")"
)

# Provider-scoped expiry scan for AS.2.4 refresh hook
# ("for provider X, find tokens expiring in next 60 s").  NULLS LAST so
# rows with no documented expiry sort past the range scan and the hook
# can stop early.
_PG_INDEX_EXPIRES = (
    "CREATE INDEX IF NOT EXISTS idx_oauth_tokens_provider_expires "
    "ON oauth_tokens(provider, expires_at NULLS LAST)"
)

_PG_INDEX_KEY_VERSION = (
    "CREATE INDEX IF NOT EXISTS idx_oauth_tokens_key_version "
    "ON oauth_tokens(key_version)"
)


# ─── SQLite branch ───────────────────────────────────────────────────────


# Dialect-shifted dev parity: DOUBLE PRECISION → REAL; otherwise
# byte-identical column shape.  Same PRIMARY KEY clause syntax (PG
# accepts the inline form too but we keep them separate for clarity).
_SQLITE_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS oauth_tokens (\n"
    "    user_id            TEXT NOT NULL\n"
    "                            REFERENCES users(id) ON DELETE CASCADE,\n"
    f"    provider           TEXT NOT NULL\n"
    f"                            CHECK (provider IN ({_PROVIDERS_SQL})),\n"
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

# SQLite ignores ``NULLS LAST`` (the syntax is rejected pre-3.30 and a
# no-op after); use plain (provider, expires_at) — the AS.2.4 hook
# tolerates the difference because the dev path's row count is tiny.
_SQLITE_INDEX_EXPIRES = (
    "CREATE INDEX IF NOT EXISTS idx_oauth_tokens_provider_expires "
    "ON oauth_tokens(provider, expires_at)"
)

_SQLITE_INDEX_KEY_VERSION = (
    "CREATE INDEX IF NOT EXISTS idx_oauth_tokens_key_version "
    "ON oauth_tokens(key_version)"
)


# ─── upgrade / downgrade ─────────────────────────────────────────────────


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(_PG_CREATE_TABLE)
        conn.exec_driver_sql(_PG_INDEX_EXPIRES)
        conn.exec_driver_sql(_PG_INDEX_KEY_VERSION)
        return

    # SQLite path.  ``CREATE TABLE IF NOT EXISTS`` keeps the migration
    # idempotent on dev DBs that already saw it; the test suite re-binds
    # the migration against a hand-built schema and we want the second
    # call to be a no-op.
    conn.exec_driver_sql(_SQLITE_CREATE_TABLE)
    conn.exec_driver_sql(_SQLITE_INDEX_EXPIRES)
    conn.exec_driver_sql(_SQLITE_INDEX_KEY_VERSION)


def downgrade() -> None:
    # Safe drop — until AS.6.1 lands the OAuth router, this table is
    # empty.  After that, operators must back up ``oauth_tokens``
    # before downgrading or the OAuth bindings are lost.  We do NOT
    # attempt to fold rows back into a prior shape: the table is
    # the only persistent home for the vault ciphertext, so a
    # downgrade is by definition a "drop the OAuth login surface
    # entirely" operation.
    op.execute("DROP INDEX IF EXISTS idx_oauth_tokens_key_version")
    op.execute("DROP INDEX IF EXISTS idx_oauth_tokens_provider_expires")
    op.execute("DROP TABLE IF EXISTS oauth_tokens")
