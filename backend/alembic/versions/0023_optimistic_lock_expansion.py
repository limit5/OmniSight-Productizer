"""Q.7 #301 — expand J2 optimistic-lock pattern to other mutation tables.

Adds ``version INTEGER NOT NULL DEFAULT 0`` to four more tables so the
same ``If-Match`` + 409 flow that ``workflow_runs`` uses can guard
cross-device concurrent edits on:

  * ``tasks``           — ``PATCH /tasks/{id}``
  * ``npi_state``       — ``PUT /runtime/npi`` (singleton runtime settings row)
  * ``tenant_secrets``  — ``PUT /secrets/{id}``
  * ``project_runs``    — ``PATCH /projects/runs/{run_id}``

See ``docs/design/multi-device-state-sync.md`` and TODO Q.7 for why
these four were chosen — they are the remaining "major mutation
endpoints" that two devices could race on before Q.7 (e.g. operator
drags a task to in_review on their laptop while marking it completed
on their phone — under last-write-wins the later PUT silently
overwrites the earlier one; under optimistic-lock the loser gets a
clean 409 with ``{current_version, your_version, hint}``).

Schema invariants mirrored from J2 (migration 0009_workflow_run_version):
  - ``NOT NULL DEFAULT 0`` so existing rows start at 0 and the first
    PATCH from any client sends ``If-Match: 0``.
  - No new index — version is only read as part of the PATCH's
    ``WHERE id = $n AND version = $m`` clause, which rides the
    existing primary-key index.
  - Column type is ``INTEGER``, not ``BIGINT`` — 2**31 PATCHes on a
    single row is not a reachable threshold and matches J2's precedent.

Revision ID: 0023
Revises: 0022
Create Date: 2026-04-24
"""
from __future__ import annotations

from alembic import op


revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


_TARGET_TABLES = ("tasks", "npi_state", "tenant_secrets", "project_runs")


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    for table in _TARGET_TABLES:
        if dialect == "postgresql":
            # PG's IF NOT EXISTS on ADD COLUMN is native so a re-run
            # (e.g. from a half-failed prior upgrade) is idempotent.
            bind.exec_driver_sql(
                f"ALTER TABLE {table} "
                "ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 0"
            )
        else:
            # SQLite ALTER TABLE ADD COLUMN has no IF NOT EXISTS; probe
            # PRAGMA table_info first. The test-migrator schema
            # coverage fixture (backend/tests/test_migrator_schema_
            # coverage.py) runs the SQLite bootstrap path, so this
            # branch is exercised by CI even on PG-first installs.
            cols = {
                row[1]
                for row in bind.exec_driver_sql(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            if "version" not in cols:
                bind.exec_driver_sql(
                    f"ALTER TABLE {table} "
                    "ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
                )


def downgrade() -> None:
    # alembic-allow-noop-downgrade: dropping the `version` column would
    # silently re-open the last-write-wins race on live rows and break
    # in-flight clients still sending `If-Match`. Hand-rolled migration
    # required to roll back this revision (see FX.7.6 contract).
    pass
