"""Phase 58 — Decision Profiles + auto_decision_log + decision_rules.negative

Three additions:

  decision_profiles    one row per available profile; current row
                       indicated by `enabled=1`; only one row should
                       be enabled at a time.
  auto_decision_log    every auto-resolved decision (chosen by
                       chooser, not the user) is recorded here so
                       the postmortem UI can list / bulk undo.
  decision_rules.{negative, undo_count}  for negative-rule learning.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-14
"""
from __future__ import annotations

import re

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS decision_profiles (
    id                      TEXT PRIMARY KEY,
    threshold_risky         REAL NOT NULL,
    threshold_destructive   REAL NOT NULL,
    auto_critical           INTEGER NOT NULL DEFAULT 0,
    enabled                 INTEGER NOT NULL DEFAULT 0,
    description             TEXT NOT NULL DEFAULT '',
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS auto_decision_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id         TEXT NOT NULL,
    kind                TEXT NOT NULL,
    severity            TEXT NOT NULL,
    chosen_option       TEXT NOT NULL,
    confidence          REAL NOT NULL DEFAULT 0.0,
    rationale           TEXT NOT NULL DEFAULT '',
    profile_id          TEXT NOT NULL DEFAULT '',
    auto_executed_at    REAL NOT NULL,
    undone_at           REAL,
    undone_by           TEXT
);
CREATE INDEX IF NOT EXISTS idx_auto_decision_log_kind ON auto_decision_log(kind);
CREATE INDEX IF NOT EXISTS idx_auto_decision_log_undone ON auto_decision_log(undone_at);
"""


_RULES_ADD_COLS = [
    "ALTER TABLE decision_rules ADD COLUMN negative INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE decision_rules ADD COLUMN undo_count INTEGER NOT NULL DEFAULT 0",
]


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in [s.strip() for s in _SQL.split(";") if s.strip()]:
        bind.exec_driver_sql(stmt)
    # Idempotent column adds — check existence first so a re-run on a
    # DB where the column already exists is a no-op. Using a savepoint
    # around a failing ALTER TABLE doesn't work on SQLite, and
    # transactional-DDL engines (Postgres) abort the whole migration
    # transaction on a DuplicateColumn error. Pre-check dodges both.
    dialect = bind.dialect.name.lower()
    for stmt in _RULES_ADD_COLS:
        col_match = re.match(
            r"ALTER TABLE (\w+) ADD COLUMN (\w+)\b", stmt
        )
        if not col_match:
            bind.exec_driver_sql(stmt)
            continue
        table, column = col_match.group(1), col_match.group(2)
        if dialect == "postgresql":
            exists = bind.exec_driver_sql(
                "SELECT 1 FROM information_schema.columns "
                f"WHERE table_schema='public' AND table_name='{table}' "
                f"AND column_name='{column}'"
            ).fetchone()
        else:
            exists = any(
                row[1] == column
                for row in bind.exec_driver_sql(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            )
        if exists:
            continue
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    bind = op.get_bind()
    for stmt in [
        "DROP INDEX IF EXISTS idx_auto_decision_log_undone",
        "DROP INDEX IF EXISTS idx_auto_decision_log_kind",
        "DROP TABLE IF EXISTS auto_decision_log",
        "DROP TABLE IF EXISTS decision_profiles",
        # SQLite doesn't support DROP COLUMN cleanly without a rebuild;
        # we leave the negative/undo_count columns in place on
        # downgrade. They are no-ops if Phase 58 code isn't loaded.
    ]:
        bind.exec_driver_sql(stmt)
