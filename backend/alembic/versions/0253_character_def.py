"""OP-2508 RECRUIT C1 — ``character_def`` table + built-in character seed.

Safe floor of the character-recruit EPIC
(``docs/architecture/2026-07-02-character-recruit-epic-design.md``): the
persistent counterpart of ``backend/agents/character_registry.CHARACTERS``.
INERT at this revision — nothing reads the table yet (no loader, no API);
C2+ adds the readers, so applying/rolling back this migration cannot change
runtime behaviour.

Schema notes
------------
* ``slug`` is the primary key; on PG a regex CHECK pins the label-safe
  shape (``^[a-z][a-z0-9-]*$``). SQLite has no ``~`` operator, so the
  sqlite branch omits the regex check (app-level validation is the real
  gate; the DB check is defence-in-depth on prod PG only).
* ``brain`` / ``guild`` are deliberately NOT SQL-enum-constrained — both
  vocabularies grow (new agent classes, new guilds) and the application
  validates against the live registries. ``max_tier`` IS check-constrained
  to the fixed S/M/L/X ladder (``character_registry.TIER_ORDER``).
* Dialect branch mirrors 0239 (``agent_character_card``): TIMESTAMPTZ /
  NOW() on PG, TEXT / CURRENT_TIMESTAMP on SQLite.

Seed
----
``SEED_CHARACTERS`` is a frozen EXACT copy of the current
``character_registry.CHARACTERS`` roster (nova / pixel / sage / rex).
Deliberately duplicated, not imported: a migration must not change shape
when the registry evolves. ``backend/tests/test_alembic_0253_character_def.py``
drift-guards the copy against the live registry.

Idempotency: ``CREATE TABLE IF NOT EXISTS`` + ``INSERT … ON CONFLICT DO
NOTHING`` (PG) / ``INSERT OR IGNORE`` (SQLite), so a re-run is a no-op.

Module-global / cross-worker audit: pure DDL+DML, no singleton, no cache.

Revision ID: 0253
Revises: 0252
Create Date: 2026-07-02
backwards-compat: safe (additive, inert)
"""
from __future__ import annotations

from alembic import op


revision = "0253"
down_revision = "0252"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS character_def (
    slug          TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    brain         TEXT NOT NULL,
    guild         TEXT NOT NULL,
    max_tier      TEXT NOT NULL,
    blurb         TEXT NOT NULL DEFAULT '',
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT character_def_slug_chk CHECK (slug ~ '^[a-z][a-z0-9-]*$'),
    CONSTRAINT character_def_max_tier_chk CHECK (max_tier IN ('S','M','L','X'))
)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS character_def (
    slug          TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    brain         TEXT NOT NULL,
    guild         TEXT NOT NULL,
    max_tier      TEXT NOT NULL,
    blurb         TEXT NOT NULL DEFAULT '',
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT character_def_max_tier_chk CHECK (max_tier IN ('S','M','L','X'))
)
"""


# Frozen copy of backend/agents/character_registry.CHARACTERS at 0253 time.
# The companion test asserts set + field equality against the live registry.
SEED_CHARACTERS: tuple[dict[str, str], ...] = (
    {
        "slug": "nova",
        "display_name": "Nova",
        "brain": "subscription-claude",
        "guild": "backend",
        "max_tier": "L",
        "blurb": "Elite backend architect — deep, high-tier work.",
    },
    {
        "slug": "pixel",
        "display_name": "Pixel",
        "brain": "subscription-codex",
        "guild": "frontend",
        "max_tier": "M",
        "blurb": "Frontend/UI specialist.",
    },
    {
        "slug": "sage",
        "display_name": "Sage",
        "brain": "subscription-gemini",
        "guild": "backend",
        "max_tier": "M",
        "blurb": "Versatile backend generalist.",
    },
    {
        "slug": "rex",
        "display_name": "Rex",
        "brain": "subscription-grok",
        "guild": "sre",
        "max_tier": "S",
        "blurb": "Fast, cheap tooling/ops hand — small self-contained tasks.",
    },
)


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def _build_insert(row: dict[str, str], dialect: str) -> str:
    cols = "slug, display_name, brain, guild, max_tier, blurb"
    vals = ", ".join(
        f"'{_sql_escape(row[c])}'"
        for c in ("slug", "display_name", "brain", "guild", "max_tier", "blurb")
    )
    if dialect == "postgresql":
        return (
            f"INSERT INTO character_def ({cols}) VALUES ({vals}) "
            f"ON CONFLICT (slug) DO NOTHING"
        )
    return f"INSERT OR IGNORE INTO character_def ({cols}) VALUES ({vals})"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    bind.exec_driver_sql(_PG_DDL if dialect == "postgresql" else _SQLITE_DDL)
    for row in SEED_CHARACTERS:
        bind.exec_driver_sql(_build_insert(row, dialect))


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TABLE IF EXISTS character_def")
