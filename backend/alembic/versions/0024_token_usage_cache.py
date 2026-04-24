"""ZZ.A1 #303-1 — add prompt-cache columns to token_usage.

Extends the ``token_usage`` table with the three cache-tracking fields
that ``SharedTokenUsage.track()`` now records:

  * ``cache_read_tokens``     — INTEGER, nullable. Total prompt-cache
                                 hit tokens observed across every call
                                 for this model.
  * ``cache_create_tokens``   — INTEGER, nullable. Total cache-creation
                                 (write) tokens (Anthropic-only; OpenAI
                                 has no equivalent side).
  * ``cache_hit_ratio``       — DOUBLE PRECISION (REAL on SQLite),
                                 nullable. ``cache_read / (input +
                                 cache_read)`` from the most recent
                                 ``track()`` call. Persisted rather
                                 than recomputed so a cold reload via
                                 ``load_token_usage_from_db`` resurfaces
                                 the last-seen value without needing
                                 per-turn history.

All three columns are intentionally NULLABLE with no DEFAULT:

  * Existing rows predate ZZ.A1 and have no cache data to back-fill.
    Per the TODO spec ``cache_read = NULL`` is the canonical marker
    for "pre-ZZ data"; the UI renders this as an em-dash rather than
    ``0 / 0`` which would imply a genuine zero-hit call.
  * New rows produced by the ZZ.A1 codepath always populate the three
    fields (``SharedTokenUsage.track`` forces ints/float) so the only
    way a NULL survives is genuine legacy data.

No index is added — the dashboard reads ``SELECT *`` for a small
(<100 model) table so the existing PK index is sufficient.

Revision ID: 0024
Revises: 0023
Create Date: 2026-04-24
"""
from __future__ import annotations

from alembic import op


revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


_PG_COLUMNS = (
    ("cache_read_tokens", "INTEGER"),
    ("cache_create_tokens", "INTEGER"),
    ("cache_hit_ratio", "DOUBLE PRECISION"),
)

_SQLITE_COLUMNS = (
    ("cache_read_tokens", "INTEGER"),
    ("cache_create_tokens", "INTEGER"),
    ("cache_hit_ratio", "REAL"),
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
    # Defensive no-op: dropping the columns would lose accumulated
    # cache observability data and break hot workers still writing
    # via the ported track(). Hand-rolled migration required for
    # rollback.
    pass
