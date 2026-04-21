"""Phase-3 Step B.4 / task #116 — MFA ephemeral challenges table.

Adds ``mfa_challenges`` for cross-worker storage of short-lived
authentication challenges. Replaces the per-worker module-global
dicts ``mfa._webauthn_challenges`` and ``mfa._pending_mfa`` that
broke under ``uvicorn --workers N`` (begin on worker A, complete
on worker B → 400 "challenge not found").

Two call paths share this table, distinguished by the ``kind`` col:

  * ``kind='webauthn'`` — WebAuthn registration / authentication
    challenge bytes. Keyed by ``user_id`` (one pending WebAuthn
    dance per user; begin_register overwrites any prior challenge).
    ``payload`` is base64-encoded raw bytes.
  * ``kind='mfa_pending'`` — MFA login-challenge token stash
    between password-OK and MFA-code-OK. Keyed by the challenge
    token (token_urlsafe). ``payload`` is JSON:
    ``{"user_id", "ip", "user_agent"}``.

TTL: ``created_at`` is recorded on insert. Callers use ``WHERE
created_at > NOW() - INTERVAL '5 minutes'`` to filter stale
entries, matching the original dict's 300-second cleanup. A
nightly sweep or on-access purge keeps the table small.

Revision ID: 0018
Revises: 0017
Create Date: 2026-04-21
"""
from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS mfa_challenges (
                id          TEXT PRIMARY KEY,
                kind        TEXT NOT NULL,
                payload     TEXT NOT NULL,
                created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_mfa_challenges_created "
            "ON mfa_challenges(created_at)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_mfa_challenges_kind "
            "ON mfa_challenges(kind)"
        )
    else:
        # SQLite dev parity. Same shape; the compat wrapper handles
        # TIMESTAMP vs TEXT type translation at runtime.
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS mfa_challenges (
                id          TEXT PRIMARY KEY,
                kind        TEXT NOT NULL,
                payload     TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_mfa_challenges_created "
            "ON mfa_challenges(created_at)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_mfa_challenges_kind "
            "ON mfa_challenges(kind)"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mfa_challenges")
