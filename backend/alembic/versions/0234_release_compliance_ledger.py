"""OP-952 H7 — immutable release compliance ledger.

Backwards-compat: safe

Append-only compliance ledger for Sprint H release conductor actions.
Every H3 state transition, H4 operator approval decision, and H5
hotfix trigger writes one row. Rows are hash-chained so tampering is
detectable by re-walking ``prev_row_hash`` / ``row_hash``.

The table is immutable at the database level: UPDATE and DELETE are
blocked by trigger on both Postgres and SQLite. Corrections must be
recorded as new rows.

Revision ID: 0234
Revises: 0232
Create Date: 2026-05-12
"""
from __future__ import annotations

from alembic import op


revision = "0234"
down_revision = "0232"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS release_compliance_ledger (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    release_id      TEXT NOT NULL,
    before_state    TEXT,
    after_state     TEXT,
    reason          TEXT NOT NULL DEFAULT '',
    evidence_hash   TEXT NOT NULL,
    prev_row_hash   TEXT NOT NULL,
    row_hash        TEXT NOT NULL,
    CONSTRAINT release_compliance_ledger_evidence_hash_chk
        CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT release_compliance_ledger_prev_hash_chk
        CHECK (prev_row_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT release_compliance_ledger_row_hash_chk
        CHECK (row_hash ~ '^[0-9a-f]{64}$')
)
"""


_PG_INDEX_TS = """
CREATE INDEX IF NOT EXISTS idx_release_compliance_ledger_ts
    ON release_compliance_ledger (ts DESC)
"""


_PG_INDEX_RELEASE_TS = """
CREATE INDEX IF NOT EXISTS idx_release_compliance_ledger_release_ts
    ON release_compliance_ledger (release_id, ts DESC)
"""


_PG_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION release_compliance_ledger_block_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'LedgerTriggerBypassed: release_compliance_ledger is append-only';
END;
$$ LANGUAGE plpgsql
"""


_PG_TRIGGER_UPDATE = """
DROP TRIGGER IF EXISTS trg_release_compliance_ledger_no_update
    ON release_compliance_ledger;
CREATE TRIGGER trg_release_compliance_ledger_no_update
    BEFORE UPDATE ON release_compliance_ledger
    FOR EACH ROW EXECUTE FUNCTION release_compliance_ledger_block_mutation()
"""


_PG_TRIGGER_DELETE = """
DROP TRIGGER IF EXISTS trg_release_compliance_ledger_no_delete
    ON release_compliance_ledger;
CREATE TRIGGER trg_release_compliance_ledger_no_delete
    BEFORE DELETE ON release_compliance_ledger
    FOR EACH ROW EXECUTE FUNCTION release_compliance_ledger_block_mutation()
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS release_compliance_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    release_id      TEXT NOT NULL,
    before_state    TEXT,
    after_state     TEXT,
    reason          TEXT NOT NULL DEFAULT '',
    evidence_hash   TEXT NOT NULL,
    prev_row_hash   TEXT NOT NULL,
    row_hash        TEXT NOT NULL,
    CONSTRAINT release_compliance_ledger_evidence_hash_chk
        CHECK (length(evidence_hash) = 64),
    CONSTRAINT release_compliance_ledger_prev_hash_chk
        CHECK (length(prev_row_hash) = 64),
    CONSTRAINT release_compliance_ledger_row_hash_chk
        CHECK (length(row_hash) = 64)
)
"""


_SQLITE_INDEX_TS = """
CREATE INDEX IF NOT EXISTS idx_release_compliance_ledger_ts
    ON release_compliance_ledger (ts DESC)
"""


_SQLITE_INDEX_RELEASE_TS = """
CREATE INDEX IF NOT EXISTS idx_release_compliance_ledger_release_ts
    ON release_compliance_ledger (release_id, ts DESC)
"""


_SQLITE_TRIGGER_UPDATE = """
CREATE TRIGGER IF NOT EXISTS trg_release_compliance_ledger_no_update
BEFORE UPDATE ON release_compliance_ledger
BEGIN
    SELECT RAISE(ABORT, 'LedgerTriggerBypassed: release_compliance_ledger is append-only');
END
"""


_SQLITE_TRIGGER_DELETE = """
CREATE TRIGGER IF NOT EXISTS trg_release_compliance_ledger_no_delete
BEFORE DELETE ON release_compliance_ledger
BEGIN
    SELECT RAISE(ABORT, 'LedgerTriggerBypassed: release_compliance_ledger is append-only');
END
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_DDL)
        bind.exec_driver_sql(_PG_INDEX_TS)
        bind.exec_driver_sql(_PG_INDEX_RELEASE_TS)
        bind.exec_driver_sql(_PG_TRIGGER_FN)
        bind.exec_driver_sql(_PG_TRIGGER_UPDATE)
        bind.exec_driver_sql(_PG_TRIGGER_DELETE)
    else:
        bind.exec_driver_sql(_SQLITE_DDL)
        bind.exec_driver_sql(_SQLITE_INDEX_TS)
        bind.exec_driver_sql(_SQLITE_INDEX_RELEASE_TS)
        bind.exec_driver_sql(_SQLITE_TRIGGER_UPDATE)
        bind.exec_driver_sql(_SQLITE_TRIGGER_DELETE)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TRIGGER IF EXISTS trg_release_compliance_ledger_no_delete")
    bind.exec_driver_sql("DROP TRIGGER IF EXISTS trg_release_compliance_ledger_no_update")
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            "DROP FUNCTION IF EXISTS release_compliance_ledger_block_mutation()"
        )
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_release_compliance_ledger_release_ts")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_release_compliance_ledger_ts")
    bind.exec_driver_sql("DROP TABLE IF EXISTS release_compliance_ledger")
