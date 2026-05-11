"""OP-877 D5 — ``release_audit`` table for develop -> main auto-promote.

Backwards-compat: safe

Append-only audit log for the daily ``scripts/auto_promote_develop_to_main.sh``
cron. Each cron tick writes exactly one row that captures:

* the outcome (``promoted`` / ``milestone_not_accepted`` / ``ff_not_possible``
  / ``push_rejected`` / ``noop``),
* the develop / main SHA range at the time of evaluation, and
* the fixVersion under evaluation, if any.

Why this is separate from :mod:`backend.deploy_audit`
-----------------------------------------------------
``deploy_audit`` (alembic 0204) is the change-management hash-chained
trail of *production deploys* (tag promotions to release.yaml, rollback
events, SLO breaches). ``release_audit`` is the upstream signal — the
daily auto-promote cron that decides whether ``main`` may even *be*
fast-forwarded to ``develop`` in the first place. They are temporally
adjacent (promote -> deploy) but live in different decision domains
and have different schemas (auto-promote doesn't have a tag, doesn't
need a hash chain, and records gate-failure reasons that deploy_audit
has no column for).

Why no hash chain
-----------------
The auto-promote cron is the only writer; rows are observational, not
compliance-anchored. Tamper resistance lives in the upstream
``deploy_audit`` chain (which records the production deploy that
follows a promote). Keeping ``release_audit`` schema-light makes the
operator query path ("did the cron fire today? what did it see?")
trivial — a single SELECT against (ts DESC).

Column rationale
----------------
* ``outcome`` is a closed enum enforced by CHECK so a typo in the cron
  cannot fork the audit stream. The five values cover the four error
  classes from the OP-877 catalog
  (``MilestoneNotAccepted`` / ``MainPushRejected`` / ``GitFFNotPossible``)
  plus the two healthy outcomes (``promoted`` and ``noop``).
* ``fix_version`` is nullable — for ``milestone_not_accepted`` rows we
  may not have a current fixVersion to attach.
* ``develop_sha`` / ``main_sha`` are the tip SHAs *at evaluation time*
  so the audit row is self-describing without needing to re-query git.
* ``detail`` is a free-form JSON blob for outcome-specific context
  (e.g. push stderr, list of main-only SHAs for the diverged case).
* ``ts`` is the cron-tick time (UTC) and the primary query axis.

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration — no module-level singleton. The only writer is
``scripts/auto_promote_develop_to_main.sh`` (one row per cron tick).
Readers are operator queries and the
``backend/tests/test_auto_promote.py`` suite.

Spec note on slot numbering
---------------------------
The OP-877 description gestures at ``0222`` as the migration slot
but the live ``versions/`` directory is at ``0206`` head (runner
incidents), so the next sequential slot is ``0207``. The slot number
does not affect behaviour — it is purely the alembic linkage ID and
follows the same convention as OP-854's ``0220 -> 0206`` adjustment.

Revision ID: 0207
Revises: 0206
Create Date: 2026-05-11
"""
from __future__ import annotations

from alembic import op


revision = "0207"
down_revision = "0206"
branch_labels = None
depends_on = None


_OUTCOME_LITERAL = (
    "'promoted','noop','milestone_not_accepted',"
    "'ff_not_possible','push_rejected'"
)


_PG_DDL = f"""
CREATE TABLE IF NOT EXISTS release_audit (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome         TEXT NOT NULL,
    fix_version     TEXT,
    develop_sha     TEXT NOT NULL DEFAULT '',
    main_sha        TEXT NOT NULL DEFAULT '',
    detail          TEXT NOT NULL DEFAULT '{{}}',
    CONSTRAINT release_audit_outcome_chk
        CHECK (outcome IN ({_OUTCOME_LITERAL}))
)
"""


_PG_INDEX_TS = """
CREATE INDEX IF NOT EXISTS idx_release_audit_ts
    ON release_audit (ts DESC)
"""


_PG_INDEX_OUTCOME_TS = """
CREATE INDEX IF NOT EXISTS idx_release_audit_outcome_ts
    ON release_audit (outcome, ts DESC)
"""


_SQLITE_DDL = f"""
CREATE TABLE IF NOT EXISTS release_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    outcome         TEXT NOT NULL,
    fix_version     TEXT,
    develop_sha     TEXT NOT NULL DEFAULT '',
    main_sha        TEXT NOT NULL DEFAULT '',
    detail          TEXT NOT NULL DEFAULT '{{}}',
    CONSTRAINT release_audit_outcome_chk
        CHECK (outcome IN ({_OUTCOME_LITERAL}))
)
"""


_SQLITE_INDEX_TS = """
CREATE INDEX IF NOT EXISTS idx_release_audit_ts
    ON release_audit (ts DESC)
"""


_SQLITE_INDEX_OUTCOME_TS = """
CREATE INDEX IF NOT EXISTS idx_release_audit_outcome_ts
    ON release_audit (outcome, ts DESC)
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_DDL)
        bind.exec_driver_sql(_PG_INDEX_TS)
        bind.exec_driver_sql(_PG_INDEX_OUTCOME_TS)
    else:
        bind.exec_driver_sql(_SQLITE_DDL)
        bind.exec_driver_sql(_SQLITE_INDEX_TS)
        bind.exec_driver_sql(_SQLITE_INDEX_OUTCOME_TS)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_release_audit_outcome_ts")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_release_audit_ts")
    bind.exec_driver_sql("DROP TABLE IF EXISTS release_audit")
