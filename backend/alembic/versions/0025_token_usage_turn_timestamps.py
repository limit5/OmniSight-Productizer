"""ZZ.A3 #303-3 — add per-turn LLM boundary stamps to token_usage.

Extends the ``token_usage`` table with two ISO-8601 UTC timestamp
columns captured by ``TokenTrackingCallback`` at the LLM-turn
boundaries:

  * ``turn_started_at`` — TEXT, nullable. Wall-clock at
                           ``on_llm_start`` of the most recent turn.
  * ``turn_ended_at``   — TEXT, nullable. Wall-clock at
                           ``on_llm_end`` of the most recent turn.

Semantics are "last-turn snapshot" — each ``SharedTokenUsage.track``
call overwrites the pair with the current turn's boundaries, not
accumulation. Downstream (ZZ.A3 checkbox 3 UI) computes two
quantities from the pair across consecutive turns:

  1. LLM compute time = ``turn_ended_at - turn_started_at`` of the
     same row.
  2. Inter-turn gap   = ``this_turn.turn_started_at -
     last_turn.turn_ended_at`` — tool execution + event-bus
     scheduling + context-gather wait, everything that falls outside
     the LLM compute window. The "avg gap 320ms" mini-stat in the
     TokenUsageStats card is driven off this quantity.

Both columns are intentionally NULLABLE with no DEFAULT:

  * Existing rows predate ZZ.A3 and have no boundary data — per the
    NULL-vs-genuine-zero contract ZZ.A1 established for cache_*
    fields, the dashboard renders an em-dash rather than a fabricated
    0ms gap.
  * New rows produced by the ZZ.A3 codepath populate the fields from
    ``TokenTrackingCallback.on_llm_start / on_llm_end`` (ISO-8601
    UTC, microsecond precision) on the first track() call.

No index is added — the dashboard reads ``SELECT *`` for a small
(<100 model) table so the existing PK index is sufficient.

Revision ID: 0025
Revises: 0024
Create Date: 2026-04-24
"""
from __future__ import annotations

from alembic import op


revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


_PG_COLUMNS = (
    ("turn_started_at", "TEXT"),
    ("turn_ended_at", "TEXT"),
)

_SQLITE_COLUMNS = (
    ("turn_started_at", "TEXT"),
    ("turn_ended_at", "TEXT"),
)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        for name, coltype in _PG_COLUMNS:
            bind.exec_driver_sql(
                f"ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS {name} {coltype}"
            )
    else:
        existing = {
            row[1]
            for row in bind.exec_driver_sql(
                "PRAGMA table_info(token_usage)"
            ).fetchall()
        }
        for name, coltype in _SQLITE_COLUMNS:
            if name not in existing:
                bind.exec_driver_sql(
                    f"ALTER TABLE token_usage ADD COLUMN {name} {coltype}"
                )


def downgrade() -> None:
    # alembic-allow-noop-downgrade: dropping the per-turn boundary
    # columns would lose accumulated turn-timing data and break hot
    # workers still writing via the ported track(). Hand-rolled
    # migration required for rollback (see FX.7.6 contract).
    pass
