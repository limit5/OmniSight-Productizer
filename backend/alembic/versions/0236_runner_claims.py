"""OP-1106 v2-Ⅹ-1bc — runner_claims table for runner coordination substrate.

Revision ID: 0236
Revises: 0235
Create Date: 2026-05-15
backwards-compat: safe

Per spec docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md
§3 Family ⑩ §4. Foundation table for the runner coordination substrate
that replaces JIRA-label-based mutex (OP-977) over the next Atlas tickets:

* OP-1107 (X-2-Shadow) wires the shadow dual-write integration
* OP-1108 (X-2bc) replaces ``find_mutex_holders()`` JQL read path with
  ``find_active_holders()`` (this table)
* OP-1109 (X-2d) cuts claim acquisition over in the 3 runner entry points
* OP-1110 (X-2-Cutover) drops the label-based writes

This migration is **shadow-only at land time**: writes happen but no
read consumer exists. That keeps it deploy-safe — rolling forward and
back never breaks an active runner because nothing reads the table yet.

Schema rationale
----------------
* ``lease_id`` is a client-generated UUID4. It's the primary key so the
  caller can hold + reference a lease without round-trip surrogate-key
  reads, and so the application's release/heartbeat paths don't need
  to look up the row by composite key.
* ``resource_key`` is the mutex resource (typically
  ``mutex:<file-path>`` or ``mutex:<jira-key>``). A **partial unique
  index** on ``(resource_key) WHERE state = 'active'`` enforces
  at-most-one active claim per resource. SQLite 3.8+ and Postgres both
  support partial unique indexes — INSERT conflicts surface as
  IntegrityError → wrapped to ``ClaimBlocked`` in the module layer.
* ``state`` is ``'active'`` or ``'released'`` (closed set, enforced by
  CHECK). Released rows stay in the table as an audit trail; the
  partial index ignores them.
* ``fencing_token`` extends the OP-977 format
  (``claim:{instance}:{epoch_us}-{uuid}``) so callers can reuse
  existing parsing.
* ``external_refs`` is JSON for forward-compat (e.g., Gerrit Change ID,
  worktree path). Stored as JSONB on Postgres, TEXT on SQLite — the
  module layer round-trips it via ``json.dumps`` / ``json.loads``.
"""
from __future__ import annotations

from alembic import op

# revision identifiers
revision = "0236"
down_revision = "0235"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS runner_claims (
    lease_id            TEXT PRIMARY KEY,
    ticket_key          TEXT NOT NULL,
    resource_key        TEXT NOT NULL,
    owner_agent_class   TEXT NOT NULL,
    owner_instance_id   TEXT NOT NULL,
    fencing_token       TEXT NOT NULL UNIQUE,
    state               TEXT NOT NULL DEFAULT 'active',
    phase               TEXT NOT NULL DEFAULT 'pickup',
    heartbeat_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acquired_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    released_at         TIMESTAMPTZ,
    release_reason      TEXT,
    external_refs       JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT runner_claims_state_check
        CHECK (state IN ('active', 'released'))
)
"""

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS runner_claims (
    lease_id            TEXT PRIMARY KEY,
    ticket_key          TEXT NOT NULL,
    resource_key        TEXT NOT NULL,
    owner_agent_class   TEXT NOT NULL,
    owner_instance_id   TEXT NOT NULL,
    fencing_token       TEXT NOT NULL UNIQUE,
    state               TEXT NOT NULL DEFAULT 'active',
    phase               TEXT NOT NULL DEFAULT 'pickup',
    heartbeat_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    acquired_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    released_at         TEXT,
    release_reason      TEXT,
    external_refs       TEXT NOT NULL DEFAULT '{}',
    CONSTRAINT runner_claims_state_check
        CHECK (state IN ('active', 'released'))
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL)
    bind.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_runner_claims_resource_active "
        "ON runner_claims (resource_key) WHERE state = 'active'"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_runner_claims_ticket "
        "ON runner_claims (ticket_key, state)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_runner_claims_heartbeat_active "
        "ON runner_claims (heartbeat_at) WHERE state = 'active'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_runner_claims_heartbeat_active")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_runner_claims_ticket")
    bind.exec_driver_sql("DROP INDEX IF EXISTS uq_runner_claims_resource_active")
    bind.exec_driver_sql("DROP TABLE IF EXISTS runner_claims")
