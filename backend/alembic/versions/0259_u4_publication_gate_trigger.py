"""OP-2567 U4-B — publication-gate trigger (defense-in-depth).

Backwards-compat: safe

Implements the U4-A0 contract freeze §G3/F3 backstop
(``docs/design/2026-07-10-phase-u4-a0-contract-freeze.md``): a
conditional ``BEFORE INSERT`` trigger on ``memory_publications`` that
makes an unapproved ``publishing``/``published`` event impossible at
the DB level. Per G3's honest security model this is defense-in-depth,
not a hard boundary — the REAL gate is human-only approval (U4-D) +
eval evidence (U4-F/H).

In-trigger check = approval + eval EXISTENCE join ONLY. Freeze F3's
live-set revalidation is NOT recomputed here (that needs a full
live-set reduction + sha256 in SQL); G3 assigns it to the
advisory-locked procedure in U4-C. All states other than
``publishing``/``published`` pass through untouched — revocation and
failure paths must never jam on this trigger.

Ships DORMANT — the guarded table is empty and nothing writes it yet
(the sanctioned writer boundary lands alongside this migration, the
composing procedure in U4-C). Zero runtime behaviour change.

Revision ID: 0259
Revises: 0258
Create Date: 2026-07-10
"""
from __future__ import annotations

from alembic import op


revision = "0259"
down_revision = "0258"
branch_labels = None
depends_on = None


# ━━ Postgres DDL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# The ENTIRE condition lives inside the plpgsql body — PostgreSQL
# forbids subqueries in a trigger WHEN clause, so the sqlite WHEN shape
# below cannot be mirrored here. This is a NEW function, deliberately
# not 0258's unconditional-RAISE mutation blocker (that one has no
# success path and 0258's own downgrade drops it). A BEFORE INSERT
# function MUST end with RETURN NEW — falling off the end is a runtime
# error on every insert, and returning NULL silently drops rows.

_PG_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION learned_item_publication_gate()
RETURNS trigger AS $$
BEGIN
    IF NEW.state IN ('publishing', 'published') THEN
        IF NEW.approval_id IS NULL OR NOT EXISTS (
            SELECT 1
            FROM memory_approvals a
            JOIN memory_eval_runs e ON e.id = a.eval_run_id
            WHERE a.id = NEW.approval_id
              AND a.version_id = NEW.version_id
              AND e.decision = 'promote'
        ) THEN
            RAISE EXCEPTION
                'PublicationGate: % requires a promote-decision approval',
                NEW.state;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_PG_DROP_TRIGGER = """
DROP TRIGGER IF EXISTS trg_memory_publications_insert_gate
    ON memory_publications
"""

_PG_CREATE_TRIGGER = """
CREATE TRIGGER trg_memory_publications_insert_gate
    BEFORE INSERT ON memory_publications
    FOR EACH ROW EXECUTE FUNCTION learned_item_publication_gate()
"""


# ━━ SQLite DDL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# WHEN + subquery is valid sqlite trigger syntax (0234/0258 family).

_SQLITE_CREATE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS trg_memory_publications_insert_gate
BEFORE INSERT ON memory_publications
WHEN NEW.state IN ('publishing', 'published') AND (
    NEW.approval_id IS NULL OR NOT EXISTS (
        SELECT 1
        FROM memory_approvals a
        JOIN memory_eval_runs e ON e.id = a.eval_run_id
        WHERE a.id = NEW.approval_id
          AND a.version_id = NEW.version_id
          AND e.decision = 'promote'
    )
)
BEGIN
    SELECT RAISE(ABORT,
        'PublicationGate: state requires a promote-decision approval');
END
"""


# ━━ upgrade / downgrade ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_TRIGGER_FN)
        bind.exec_driver_sql(_PG_DROP_TRIGGER)
        bind.exec_driver_sql(_PG_CREATE_TRIGGER)
    else:
        bind.exec_driver_sql(_SQLITE_CREATE_TRIGGER)


def downgrade() -> None:
    # Drops ONLY the gate trigger (+ its function on PG). The 0258
    # tables and A1's append-only triggers are 0258's to manage.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_DROP_TRIGGER)
        bind.exec_driver_sql(
            "DROP FUNCTION IF EXISTS learned_item_publication_gate()"
        )
    else:
        bind.exec_driver_sql(
            "DROP TRIGGER IF EXISTS trg_memory_publications_insert_gate"
        )
