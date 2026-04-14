"""Phase 53 — audit & compliance layer.

Adds a single tamper-evident table that captures every state-changing
operation the Decision Engine performs (mode/strategy switches,
decision resolves, undos), plus a Merkle-style hash chain so any
post-hoc tampering with a row breaks the rest of the chain.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-14
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    actor           TEXT NOT NULL DEFAULT 'system',
    action          TEXT NOT NULL,
    entity_kind     TEXT NOT NULL,
    entity_id       TEXT,
    before_json     TEXT NOT NULL DEFAULT '{}',
    after_json      TEXT NOT NULL DEFAULT '{}',
    prev_hash       TEXT NOT NULL DEFAULT '',
    curr_hash       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_kind, entity_id);
"""


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in [s.strip() for s in _SQL.split(";") if s.strip()]:
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    bind = op.get_bind()
    for stmt in [
        "DROP INDEX IF EXISTS idx_audit_log_entity",
        "DROP INDEX IF EXISTS idx_audit_log_actor",
        "DROP INDEX IF EXISTS idx_audit_log_ts",
        "DROP TABLE IF EXISTS audit_log",
    ]:
        bind.exec_driver_sql(stmt)
