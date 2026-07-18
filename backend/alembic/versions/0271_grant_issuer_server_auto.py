"""U6-0 B-autoauth AA-2 — allow grant_issuer_source='server_auto_grant' (DORMANT).

Widen the ``action_grants.grant_issuer_source`` CHECK to admit the server-side
auto-authorization issuer, so ``db.auto_grant_from_prepared`` can record grants
issued by the (default-OFF) auto-auth policy. Additive + dormant: nothing
produces a ``server_auto_grant`` row until AA-3 wires the policy behind
``OMNISIGHT_U6_AUTO_AUTH`` (default OFF).

PostgreSQL is authoritative; the dev SQLite path gets the same widened CHECK
from ``backend/db.py::_SCHEMA`` (SQLite does not run alembic). Rollback narrows
the CHECK back to the original two values (fails only if a server_auto_grant row
already exists — expected for an irreversible widening).

Revision ID: 0271
Revises: 0270
Create Date: 2026-07-19
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op

revision = "0271"
down_revision = "0270"
branch_labels = None
depends_on = None

_CONSTRAINT = "action_grants_grant_issuer_source_check"
_OLD = "('ui_confirm', 'slash_command')"
_NEW = "('ui_confirm', 'slash_command', 'server_auto_grant')"


def _reset_check(values: str) -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql(
        f"ALTER TABLE action_grants DROP CONSTRAINT IF EXISTS {_CONSTRAINT}"
    )
    conn.exec_driver_sql(
        f"ALTER TABLE action_grants ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (grant_issuer_source IN {values})"
    )


def upgrade() -> None:
    _reset_check(_NEW)


def downgrade() -> None:
    _reset_check(_OLD)
