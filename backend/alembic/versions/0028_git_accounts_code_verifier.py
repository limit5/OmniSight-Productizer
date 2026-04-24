"""Phase 5-12 (#multi-account-forge) — git_accounts.code_verifier (OAuth prep).

Lays the ``code_verifier`` JSONB column on ``git_accounts`` for the
future OAuth 2.0 flow (GitHub App install / GitLab OAuth2 /
Atlassian 3LO / generic PKCE). Row 5-12's scope is explicit: **do
not implement OAuth yet — only reserve the schema shape so the
eventual OAuth row does not need another data-model migration**.

Why both columns (``auth_type`` + ``code_verifier``)
────────────────────────────────────────────────────
``auth_type`` already shipped day-1 on 0027 (``TEXT DEFAULT 'pat'``)
because it is a cheap one-token discriminator that every row carries
anyway. ``code_verifier`` was held back to this row because it is
OAuth-specific container state:

* During an authorization_code + PKCE exchange the client must
  persist the high-entropy ``code_verifier`` across the redirect
  hop (RFC 7636). Storing it on the in-progress ``git_accounts``
  row is the natural home — there's no separate session-scoped
  table and one credential row is the unit of "account being
  connected".
* OAuth refresh flows typically rotate refresh tokens; the JSONB
  shape lets us stash ``refresh_token_fingerprint`` / ``scopes`` /
  ``expires_at`` / ``auth_server_metadata`` alongside the verifier
  without another ALTER TABLE per provider.
* Single JSONB container (not one TEXT column per OAuth field)
  keeps the schema stable across future OAuth variants
  (PKCE-only / client-credentials / device-code) — only the
  JSON shape evolves.

Why additive (not a table recreate)
───────────────────────────────────
0027's ``git_accounts`` is already populated by row 5-5's legacy
auto-migration hook. An ALTER TABLE with ``NOT NULL DEFAULT '{}'``
is a cheap metadata-only operation on PG 11+ (stored in
``pg_attrdef``; existing rows get the default on read) — no
table rewrite, no lock escalation. Safe to run against live prod.

Scope discipline (row 5-12)
───────────────────────────
This migration is **schema-only**. No CRUD surface update
(``GitAccountCreate`` / ``GitAccountUpdate`` in
``backend/routers/git_accounts.py`` stay exactly as row 5-4 shipped
them), no resolver change, no UI, no validator extension. The
column is writable only by a future OAuth handler that row 5-12's
TODO header explicitly defers. Keeping the blast radius to "one
new nullable-ish column with a DEFAULT" means an accidental
rollback reverts cleanly and no operator action is needed to land
this row.

Production-readiness gate
─────────────────────────
This row stays **dev-only** — column lands empty + unread. The
``[V]`` flip only happens once the eventual OAuth implementation
ships + exercises the column end-to-end (PAT-only MVP does not
read or write this column).

Revision ID: 0028
Revises: 0027
Create Date: 2026-04-24
"""
from __future__ import annotations

from alembic import op


revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        # JSONB so future handlers can query inside the blob
        # (``code_verifier->>'verifier'``) without string-parsing.
        # NOT NULL + DEFAULT '{}'::jsonb means every existing row
        # gets an empty object on read; no back-fill UPDATE needed.
        conn.exec_driver_sql(
            "ALTER TABLE git_accounts "
            "ADD COLUMN IF NOT EXISTS code_verifier "
            "JSONB NOT NULL DEFAULT '{}'::jsonb"
        )
    else:
        # SQLite dev parity — TEXT-of-JSON (no native JSONB).
        # ``ADD COLUMN IF NOT EXISTS`` is SQLite 3.35+; the dev
        # image bundles 3.40 so the ``IF NOT EXISTS`` is safe.
        # For the belt-and-braces case where a fresh DB already
        # has the column (via db.py::_SCHEMA), we wrap in a
        # try/except so a re-run on an already-migrated dev DB
        # does not explode.
        try:
            conn.exec_driver_sql(
                "ALTER TABLE git_accounts "
                "ADD COLUMN code_verifier TEXT NOT NULL DEFAULT '{}'"
            )
        except Exception as exc:  # pragma: no cover — dev ergonomics
            msg = str(exc).lower()
            # SQLite raises OperationalError for duplicate-column; we
            # only swallow *that* specific case so genuine failures
            # still surface.
            if "duplicate column name" not in msg:
                raise


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name
    if dialect == "postgresql":
        conn.exec_driver_sql(
            "ALTER TABLE git_accounts DROP COLUMN IF EXISTS code_verifier"
        )
    else:
        # SQLite supports DROP COLUMN only from 3.35 onward. The dev
        # image is 3.40; fall back to a try/except so older SQLite
        # environments (if any) degrade cleanly instead of hard-fail.
        try:
            conn.exec_driver_sql(
                "ALTER TABLE git_accounts DROP COLUMN code_verifier"
            )
        except Exception:  # pragma: no cover — dev ergonomics
            # If DROP COLUMN isn't supported, leave the column in
            # place. It's harmless — default '{}' matches the
            # pre-migration absence when consumers ignore it.
            pass
