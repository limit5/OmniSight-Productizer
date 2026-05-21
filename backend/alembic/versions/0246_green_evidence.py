"""OP-1569 [RT-04b] -- ``green_evidence`` store keyed by full commit SHA.

backwards-compat: safe (additive, no FK out, no data backfill)

The release-train green-signal pipeline (design doc
``docs/design/2026-05-21-release-train-stories.md`` RT-04b) needs a
durable record of which *exact* develop commits have passed the fast
submit-gate so the candidate-build trigger (RT-09) can select a
last-certified SHA. The fast gate itself is RT-04a (CI, out of scope
here); this migration owns only the persistence surface that gate
writes to and the candidate selector reads from.

Why a dedicated table and not a column on ``release_train``
==========================================================
``release_train`` (RT-10a, not yet landed) tracks the *promotion*
lifecycle of a chosen candidate and carries a ``green_evidence``
reference — but green evidence is produced per-change at submit time,
long before (and far more often than) any promotion exists. Keying the
evidence on the full SHA in its own table lets the gate record a result
for every develop change while ``release_train`` rows are created only
for the handful of SHAs that are actually promoted. RT-10a's
``green_evidence`` field will reference this table by ``full_sha``.

Schema rationale
----------------
* ``full_sha`` -- the 40-char lowercase hex commit SHA, PRIMARY KEY.
  Querying is by *full* SHA only (AC): an abbreviated prefix can never
  collide with a stored full SHA because the lookup is an exact-match
  on the primary key. App-layer validation
  (``backend.agents.green_evidence``) rejects anything that is not
  40 hex chars before it ever reaches the DB; the ``length`` CHECK is
  the portable DB-side backstop (sqlite has no regex operator, so hex
  enforcement lives in the app layer, not a DB CHECK).
* ``status`` -- closed enum ``'pass'`` / ``'fail'``. Absence of a row
  is *not* a third state: the query helper is fail-closed, so an unknown
  SHA is treated as not-green without needing a sentinel row.
* ``pipeline_id`` -- the RT-04a CI pipeline that produced the evidence;
  nullable so a manual/backfilled record (operator break-glass) is
  still representable.
* ``evidence_url`` -- optional deep link to the gate run for operator
  triage.
* ``recorded_at`` -- when the evidence was first written.
* ``updated_at`` -- bumped on re-record (a SHA can flip pass<->fail if
  the gate is re-run, e.g. after a flaky-infra retry); the upsert path
  writes this so the audit trail shows the latest determination time.

Index on ``(status, recorded_at DESC)`` serves the candidate selector's
hot path: "give me the most recently certified-green SHA."

Module-global / cross-worker state audit
----------------------------------------
Pure DDL migration -- no module-level singleton. The sole writer is
``backend.agents.green_evidence.record_evidence`` (one upsert per gate
result); readers are ``green_status`` / ``get_evidence`` in the same
module. No background task or process-global cache is introduced.

Revision ID: 0246
Revises: 0245
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op


revision = "0246"
down_revision = "0245"
branch_labels = None
depends_on = None


# Closed enum -- must stay in lock-step with ``GreenStatus`` in
# ``backend.agents.green_evidence`` (asserted in
# ``test_green_evidence.py::test_status_enum_matches_migration``).
_STATUS_LITERAL = "'pass','fail'"


_PG_DDL = f"""
CREATE TABLE IF NOT EXISTS green_evidence (
    full_sha      TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    pipeline_id   TEXT,
    evidence_url  TEXT,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT green_evidence_status_chk
        CHECK (status IN ({_STATUS_LITERAL})),
    CONSTRAINT green_evidence_full_sha_len_chk
        CHECK (length(full_sha) = 40)
)
"""


_SQLITE_DDL = f"""
CREATE TABLE IF NOT EXISTS green_evidence (
    full_sha      TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    pipeline_id   TEXT,
    evidence_url  TEXT,
    recorded_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT green_evidence_status_chk
        CHECK (status IN ({_STATUS_LITERAL})),
    CONSTRAINT green_evidence_full_sha_len_chk
        CHECK (length(full_sha) = 40)
)
"""


_INDEX_STATUS_RECORDED = """
CREATE INDEX IF NOT EXISTS idx_green_evidence_status_recorded
    ON green_evidence (status, recorded_at DESC)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        _PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL
    )
    bind.exec_driver_sql(_INDEX_STATUS_RECORDED)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_green_evidence_status_recorded")
    bind.exec_driver_sql("DROP TABLE IF EXISTS green_evidence")
