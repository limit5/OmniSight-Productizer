"""OP-1118 v2-Ⅹ-RescueCLI — runner_audit_events table for operator-rescue audit trail.

Revision ID: 0237
Revises: 0236
Create Date: 2026-05-15
backwards-compat: safe

Per spec §3 Family ⑩ §4 ticket row 15 (Code AC #2). Append-only audit
trail for any operator-driven mutation of the OP-1106 ``runner_claims``
table — every ``omnisight-runner-rescue {dump,release,reset}`` invocation
writes one row here so post-incident review can answer "who released
this lease and why".

Schema rationale
----------------
* Append-only design: no UPDATE or DELETE paths exist in the rescue CLI.
  An incident-review reading the table can trust each row is a fact at
  ``ts``, not a mutable record.
* ``action`` is a free-form string (no CHECK) so future audit producers
  beyond the rescue CLI can extend the namespace without a schema
  migration. The rescue CLI uses ``rescue.{dump|release|reset}``.
* ``operator_fingerprint`` is the L2 operator ID per ADR-0033; the
  rescue CLI requires it on every write subcommand (``release`` and
  ``reset``). Dump operations are read-only and can be unattributed
  (still logged for audit completeness, with fingerprint=NULL).
* ``target_lease_id`` + ``target_ticket_key`` carry the scope of the
  action — useful for joining against ``runner_claims`` during review.
* ``details`` is JSON for forward-compat (release reason, lease state
  snapshot pre-release, error context). Stored as JSONB on Postgres,
  TEXT on SQLite — the rescue CLI round-trips via ``json.dumps``.
"""
from __future__ import annotations

from alembic import op

# revision identifiers
revision = "0237"
down_revision = "0236"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS runner_audit_events (
    id                      BIGSERIAL PRIMARY KEY,
    ts                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action                  TEXT NOT NULL,
    operator_fingerprint    TEXT,
    target_lease_id         TEXT,
    target_ticket_key       TEXT,
    details                 JSONB NOT NULL DEFAULT '{}'::jsonb
)
"""

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS runner_audit_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    action                  TEXT NOT NULL,
    operator_fingerprint    TEXT,
    target_lease_id         TEXT,
    target_ticket_key       TEXT,
    details                 TEXT NOT NULL DEFAULT '{}'
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL)
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_runner_audit_events_ts "
        "ON runner_audit_events (ts)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_runner_audit_events_action_ts "
        "ON runner_audit_events (action, ts)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_runner_audit_events_lease "
        "ON runner_audit_events (target_lease_id) "
        "WHERE target_lease_id IS NOT NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_runner_audit_events_lease")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_runner_audit_events_action_ts")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_runner_audit_events_ts")
    bind.exec_driver_sql("DROP TABLE IF EXISTS runner_audit_events")
