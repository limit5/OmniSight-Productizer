"""Phase-3-Runtime-v2 SP-2.1 — PG full-text search for episodic_memory.

Replaces SQLite FTS5 (virtual table + MATCH) with PostgreSQL
``tsvector`` + GIN index, gated by dialect so SQLite dev environments
keep working unchanged.

Why
───
SQLite exposes full-text search via FTS5 virtual tables with the
``MATCH`` operator. PostgreSQL has no FTS5 equivalent; its native
full-text engine uses the ``tsvector`` type + the ``@@`` operator
against a ``tsquery``. The existing compat wrapper
(``backend.db_pg_compat``) turns the FTS5 ``CREATE VIRTUAL TABLE`` /
``INSERT INTO *_fts`` / ``MATCH ?`` statements into no-ops on PG, so
today the L3 episodic memory search silently returns zero matches on
any PG-backed deploy. The LIKE fallback also no-ops because it's gated
behind an FTS5-MATCH exception that never fires.

In other words: PG deployments have had a silently broken episodic
memory search since Phase-3 cutover. Epic 3's ``search_episodic_memory``
port (SP-3.12) will wire the new tsvector column in; this migration
(SP-2.1) just lays the schema.

What this migration does
────────────────────────
PostgreSQL branch:
  1. Add a ``tsv tsvector GENERATED ALWAYS AS (to_tsvector('english',
     ...concat of indexed cols...)) STORED`` column to ``episodic_memory``.
     ``GENERATED ... STORED`` means PG auto-maintains the column on
     every INSERT / UPDATE — no app-layer trigger, no FTS5-style
     rebuild function. Drop the column and the index → search is
     still well-defined (empty result set, no crash) so a downgrade
     leaves the system in a consistent state.

  2. Add a GIN index ``episodic_memory_tsv_gin`` on the new column.
     GIN is the right index type for tsvector: the FTS engine walks
     posting lists in O(matches) rather than scanning every row.

SQLite branch: no-op. The SQLite FTS5 virtual table
``episodic_memory_fts`` stays in place (created by ``db.py::init()``)
and ``search_episodic_memory`` continues to use ``MATCH`` + LIKE
fallback. SQLite has no ``tsvector`` type; adding the column would
fail.

Indexed columns
───────────────
Mirrors what the SQLite FTS5 virtual table already indexes (see
``backend/db.py::init()`` line ~122):

    CREATE VIRTUAL TABLE episodic_memory_fts USING fts5(
        error_signature, solution, soc_vendor, tags,
        content='episodic_memory', content_rowid='rowid'
    )

The PG tsvector concatenates the same four columns with
``coalesce(..., '')`` so NULLs don't propagate into the to_tsvector
call. English dictionary is the pragmatic default — OmniSight's
audit/debug corpus is predominantly English (error strings,
provider SDK names, log tokens); multi-lingual search is a
follow-up if it ever becomes a real requirement.

Ranking behaviour difference vs. SQLite FTS5
────────────────────────────────────────────
Operator was warned in the design doc approval (2026-04-20):

  * SQLite FTS5 ranks via BM25 by default
  * PG ``ts_rank()`` scores by term frequency + position (not BM25)

Top-K row ordering may shift between dialects. The SP-3.12 port
adds ``test_episodic_memory_search_equivalence.py`` to pin *result
set* equivalence (same rows match-or-not) across dialects, accepting
rank-order drift within the matched set.

Idempotency
───────────
Column add uses ``ADD COLUMN IF NOT EXISTS`` (PG 9.6+). Index create
uses ``CREATE INDEX IF NOT EXISTS``. Re-running the migration is a
no-op.

Downgrade
─────────
``downgrade()`` drops the GIN index then the column. On a table with
any row count this is a full rewrite (STORED generated column drop)
— operators should not downgrade without a maintenance window. Given
the Phase-3-Runtime-v2 plan has an explicit escape hatch
(``phase-3-runtime-v2-start`` tag + 03-escape-hatch.md procedure),
downgrading past 0017 should be exceedingly rare.

Revision ID: 0017
Revises: 0016
Create Date: 2026-04-20
"""
from __future__ import annotations

from alembic import op


revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    if not is_pg:
        # SQLite keeps its existing FTS5 virtual table. Nothing to do.
        return

    # PostgreSQL path.
    #
    # GENERATED ALWAYS ... STORED: PG 12+. Column value is computed on
    # insert/update and persisted — never NULL, always in sync. The
    # ``coalesce()`` wrappers protect against NULL columns which would
    # cause to_tsvector to return NULL and propagate.
    conn.exec_driver_sql(
        """
        ALTER TABLE episodic_memory
            ADD COLUMN IF NOT EXISTS tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector(
                    'english',
                    coalesce(error_signature, '') || ' ' ||
                    coalesce(solution, '')         || ' ' ||
                    coalesce(soc_vendor, '')       || ' ' ||
                    coalesce(tags, '')
                )
            ) STORED
        """
    )

    conn.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS episodic_memory_tsv_gin
            ON episodic_memory USING GIN(tsv)
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    if not is_pg:
        return

    # Drop index before column — dropping the column would cascade the
    # index drop but being explicit keeps the order easy to read and
    # matches the reverse of upgrade().
    conn.exec_driver_sql(
        "DROP INDEX IF EXISTS episodic_memory_tsv_gin"
    )
    conn.exec_driver_sql(
        "ALTER TABLE episodic_memory DROP COLUMN IF EXISTS tsv"
    )
