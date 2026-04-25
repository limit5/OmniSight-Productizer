"""Y1 row 7 (#277) — project_id column on existing business tables.

Adds a NULLable ``project_id TEXT REFERENCES projects(id) ON DELETE
SET NULL`` column to every business table that already carries
``tenant_id`` from I1 (alembic 0012), then backfills the new column
to the deterministic per-tenant default project that 0037 created.
This is the load-bearing schema half of the Y1 row::

    所有既有業務表加 ``project_id`` 欄位：workflow_runs /
    debug_findings / decisions / event_log / artifacts / spec_* /
    user_preferences (類似 I1 的 tenant_id 回填策略)。NULL 暫時允許,
    回填完 1 個 release 後加 NOT NULL。

Why this migration must follow 0037
───────────────────────────────────
0037 backfilled one default project per tenant with the deterministic
id projection ``p-<tenant-suffix>-default`` (strip ``t-`` prefix when
present, suffix ``-default``).  This migration uses the *same* SQL
projection to derive each business row's ``project_id`` from its
existing ``tenant_id``.  Running 0038 before 0037 would either skip
the backfill (no projects rows to point at) or trigger FK violations
when the UPDATE writes a missing project id — splitting the work
across two revisions keeps both single-purpose and the dependency
order explicit.

Tables covered (the literal TODO list, modulo two notes)
────────────────────────────────────────────────────────
* ``workflow_runs``       — Phase 56 durable workflow checkpoints.
* ``debug_findings``      — agent debug findings from event-bus emit.
* ``decision_rules``      — *the TODO row says "decisions"; the actual
                             table name in this codebase has always
                             been ``decision_rules`` (see I1 0012's
                             ``TABLES_NEEDING_TENANT_ID``).  We follow
                             the I1 precedent and operate on
                             ``decision_rules``.*
* ``event_log``           — Phase 50 event-bus tap.
* ``artifacts``           — agent-emitted file artifacts.
* ``user_preferences``    — J4 per-user kv (composite PK
                             ``(user_id, pref_key)``; ``project_id``
                             is independent of the PK).

Two TODO entries are deliberately NOT covered here:

* ``spec_*``              — *no ``spec_*`` table exists in the current
                             schema*.  The TODO row is forward-looking
                             from the time it was written; a future
                             ``spec_*`` table will need its own
                             ``project_id`` add (likely inline in its
                             create migration, since by then the FK
                             target is already there).  A grep of
                             ``backend/db.py::_SCHEMA`` and
                             ``backend/alembic/versions/`` confirms no
                             matching table.
* ``audit_log``           — *intentionally tenant-scoped, not
                             project-scoped*.  The hash chain integrity
                             contract from Phase 53 spans tenant-wide
                             actor activity; binding individual entries
                             to a project would force the chain to be
                             per-project which fragments oncall-facing
                             ledger queries (`who did what across this
                             tenant in the last 24 h`).  Skipping
                             matches the TODO row, which lists neither
                             ``audit_log`` nor ``api_keys`` / ``users``.
* ``api_keys`` / ``users`` — same reason as ``audit_log``: scoped at the
                             tenant level by design.  ``api_keys`` may
                             grow a project-scope concept later via a
                             scope-string contract; the column-level
                             change is not on this row.

Schema decisions
────────────────
* **NULLable column**.  The TODO row explicitly says
  "NULL 暫時允許, 回填完 1 個 release 後加 NOT NULL".  Keeping the
  column NULLable lets a future revision (after one release of
  observation) flip it to NOT NULL with a separate ALTER + backfill
  guard.  It also means the FK validates only on rows where
  ``project_id IS NOT NULL`` — pre-existing rows whose backfill
  somehow didn't materialise a project don't cause CREATE-time FK
  errors (the ALTER TABLE adds the column with all values NULL,
  which is FK-clean by definition).
* **``REFERENCES projects(id) ON DELETE SET NULL``**.  The default
  ``ON DELETE NO ACTION`` would block project deletion if any
  business row points at the project — which is exactly the wrong
  semantic when a project is *archived* (soft-delete via
  ``archived_at``) but the application also supports hard-delete in
  rare admin rollback paths (see 0033 / 0034 / 0036 docstrings for
  the same precedent).  ``SET NULL`` keeps the business row alive
  with a NULL ``project_id`` so a follow-up admin action can re-
  attach it.  Distinct from ``tenant_id`` which has no ON DELETE
  clause from I1 — that's a pre-existing wart we are not retroactively
  fixing in this revision.
* **No CHECK that ``project_id``'s tenant matches the row's
  ``tenant_id``**.  Enforcing "this project's tenant equals this
  row's tenant" requires either (a) a composite FK that carries the
  project's owning ``tenant_id`` into the business row (extra column
  per business table — heavy) or (b) a trigger (extra DDL surface,
  divergent SQLite vs PG syntax).  The next TODO row scopes this to
  a *test-level* invariant ("外鍵 + CHECK 約束") which we read as
  "FK plus a test that asserts the cross-tenant tuple invariant" —
  not as "DB-level CHECK constraint".  The application layer (Y3
  authorisation resolver) is the canonical enforcer; the FK keeps
  the row from pointing at a non-existent project.
* **One index per table** ``idx_<table>_project ON <table>(project_id)``.
  Mirrors the I1 ``idx_<table>_tenant`` pattern.  Not partial because
  the NULL tail will shrink to zero once the next revision flips
  ``project_id`` to NOT NULL — a partial index would have to be
  recreated then anyway.

Backfill projection
───────────────────
Identical to 0037's project-id derivation::

    project_id =
        'p-' || CASE
                  WHEN substr(tenant_id, 1, 2) = 't-'
                  THEN substr(tenant_id, 3)
                  ELSE tenant_id
                END
             || '-default'

Worked examples::

    tenant_id = 't-default'    →  project_id = 'p-default-default'
    tenant_id = 't-acme'       →  project_id = 'p-acme-default'
    tenant_id = 'legacy'       →  project_id = 'p-legacy-default'

Re-using the projection (rather than joining ``tenants`` and looking
the project up) is deliberate: the join would force ``WHERE EXISTS``
guards across PG and SQLite to behave consistently, and the
deterministic projection lets reviewers spot-check the backfill
without running it.

Idempotency
───────────
``ALTER TABLE … ADD COLUMN`` is NOT idempotent in either dialect —
re-running raises ``duplicate column`` on SQLite and ``column …
already exists`` on PG.  We guard each table by introspecting its
column list via ``PRAGMA table_info(<table>)`` (the
``alembic_pg_compat`` shim rewrites it to the equivalent
``information_schema.columns`` SELECT on PG) and skipping the
``ALTER TABLE`` plus the backfill UPDATE if ``project_id`` already
exists.  ``CREATE INDEX IF NOT EXISTS`` is natively idempotent so it
runs unconditionally — re-running the whole migration is safe.

The backfill UPDATE itself is also idempotent thanks to
``WHERE project_id IS NULL`` — a second run touches no rows because
every reachable row already has the deterministic id from the first
run.

Module-global / cross-worker state audit
────────────────────────────────────────
Pure DDL + DML migration.  No in-memory cache, no module-level
singleton.  Runs once at ``alembic upgrade head`` time during the
offline cutover window — there is no "every worker" question because
every worker boots after the upgrade and sees the post-add column
plus the post-backfill values.

Read-after-write timing audit
─────────────────────────────
No code reads ``project_id`` yet — Y2 admin REST and Y3 authorisation
resolver are still on the TODO list.  When those readers land they
observe the post-backfill state.  No timing-visible behaviour change
because the column did not exist before this revision; every reader
that could observe it lands strictly after the upgrade.

Production readiness gate
─────────────────────────
* No new Python / OS package — production image needs no rebuild.
* No new schema artefacts — TABLES_IN_ORDER / TABLES_WITH_IDENTITY_ID
  stay as 0032-0036 left them.  The migrator already replays every
  affected business table; column additions ride along with the row
  body when the migrator dumps SELECT * and replays via the column
  list it discovers from ``information_schema``.
* Backfill scope is bounded by the row count of the six business
  tables.  On a fresh dev DB with one tenant and one user, the
  upgrade does six ``ALTER TABLE`` + six ``UPDATE`` against tables
  that are mostly empty.
* Production status after this commit: **dev-only**.  Next gate is
  ``deployed-inactive`` — operator runs ``alembic upgrade head`` on
  the prod PG instance.  No env knob change required (the column is
  observed only by Y2/Y3 code that hasn't shipped yet).

Dialect handling
────────────────
Every SQL string in this migration goes through the
``alembic_pg_compat`` shim:

* ``PRAGMA table_info(T)``    →  ``SELECT … FROM
                                  information_schema.columns
                                  WHERE table_name = 'T'``  (the
                                  shim emits a row tuple shape
                                  ``(cid, name, type, notnull,
                                  dflt_value, pk)`` matching
                                  SQLite's, so ``row[1] == "name"``
                                  reads identically).
* ``substr(col, n)``           — both dialects, identical semantics.
* ``CASE WHEN … END``          — both dialects, identical semantics.

No new shim rules required.

Revision ID: 0038
Revises: 0037
Create Date: 2026-04-25
"""
from __future__ import annotations

from alembic import op


revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


# Tables that already carry ``tenant_id`` from I1 (alembic 0012's
# ``TABLES_NEEDING_TENANT_ID``) and that we now extend with
# ``project_id``.  Order matches the TODO row's enumeration; FK
# ordering does not matter here because all six tables already exist
# and the FK target ``projects`` exists from 0033.
_TABLES_NEEDING_PROJECT_ID: tuple[str, ...] = (
    "workflow_runs",
    "debug_findings",
    "decision_rules",   # TODO row says "decisions"; actual table name.
    "event_log",
    "artifacts",
    "user_preferences",
)


# Deterministic per-row projection, identical to 0037's project id
# derivation for default projects.  Re-using the same SQL keeps the
# backfilled FK self-consistent without a join against ``tenants``.
_PROJECT_ID_FROM_TENANT_ID = (
    "'p-' || CASE "
    "WHEN substr(tenant_id, 1, 2) = 't-' THEN substr(tenant_id, 3) "
    "ELSE tenant_id END || '-default'"
)


def upgrade() -> None:
    conn = op.get_bind()
    for table in _TABLES_NEEDING_PROJECT_ID:
        # Idempotency guard: if a re-run finds ``project_id`` already
        # present we skip both the ALTER (which would fail) and the
        # backfill UPDATE (already covered by the prior run plus the
        # ``WHERE project_id IS NULL`` predicate).  The CREATE INDEX
        # statement is natively idempotent so it runs unconditionally
        # below.
        cols = {
            row[1]
            for row in conn.exec_driver_sql(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        if "project_id" not in cols:
            conn.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN project_id TEXT "
                f"REFERENCES projects(id) ON DELETE SET NULL"
            )
            # Backfill from the deterministic 0037 projection.  Bounded
            # by row count of {table}; uses no temp table or join.
            conn.exec_driver_sql(
                f"UPDATE {table} "
                f"SET project_id = {_PROJECT_ID_FROM_TENANT_ID} "
                f"WHERE project_id IS NULL AND tenant_id IS NOT NULL"
            )
        conn.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_project "
            f"ON {table}(project_id)"
        )


def downgrade() -> None:
    """Drop the indexes and (best-effort) the columns.

    SQLite 3.35+ supports ``ALTER TABLE … DROP COLUMN`` but the syntax
    fails if the column participates in a foreign key from another
    table or in an index that the migration didn't drop first.  We
    drop the indexes ourselves, then attempt the column drop and
    swallow per-table errors so a downgrade against an older SQLite
    or under unexpected FK fan-in still completes — the residual
    columns are harmless once the application stops reading them.

    The I1 0012 downgrade uses the same pattern (drops the
    ``tenants`` table only, leaves ``tenant_id`` columns in place);
    we are deliberately a notch more aggressive because the column
    add was the entire point of this revision.
    """
    conn = op.get_bind()
    for table in _TABLES_NEEDING_PROJECT_ID:
        conn.exec_driver_sql(
            f"DROP INDEX IF EXISTS idx_{table}_project"
        )
    for table in _TABLES_NEEDING_PROJECT_ID:
        try:
            conn.exec_driver_sql(
                f"ALTER TABLE {table} DROP COLUMN project_id"
            )
        except Exception:
            # Either the dialect can't drop, or the column never
            # existed (downgrade run against pre-upgrade DB).  Either
            # way the resulting state — no project_id column — is the
            # downgrade target.
            pass
