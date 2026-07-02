"""OP-2514 — persist recruited characters + grandfather baseline seed.

C1's 0253 seeded only the 4 built-ins (nova/pixel/sage/rex); the 4 RECRUITED
characters (iris/argus/kai/vega) live only in prod's DB, so a rebuild / new
environment loses them. This migration version-controls:

* ``character_def`` rows for the 4 recruits (roster DEFINITIONS = config,
  exactly like the 0253 built-in seed).
* Their grandfather BASELINE per
  ``docs/operations/rpg-grandfather-xp-2026-07-02.md``: the documented
  one-time ``agent_character_card`` level/xp grants + ``agent_skill_state``
  rows. These are the deterministic veteran baseline, NOT live earned state.

Deliberately NOT seeded (organic post-baseline state, protected by backups):

* XP earned after the baseline through real deliveries.
* ``branch_choice`` — the recruits' branches were left PENDING in the
  grandfather ledger; nova/vega ``enterprise_web`` locks are post-recruit.
* vega's card/skills — vega was a fresh recruit with NO grandfather grant,
  so only its ``character_def`` row is seeded.

Idempotency / prod safety: every row is ``INSERT … ON CONFLICT DO NOTHING``
(PG) / ``INSERT OR IGNORE`` (SQLite) — an existing DB with live progression
is untouched (prod = no-op); only a fresh DB gets the roster + baseline. No
DDL: ``character_def`` (0253), ``agent_character_card`` (0239) and
``agent_skill_state`` (0226) already exist at this revision.

Card identity mirrors the runner's character path
(``auto-runner-jira.py::_resolve_card_identity``): ``agent_id = slug``,
``"class" = brain``, ``instance_suffix = slug`` — the slug-suffix keeps the
unique ``("class", instance_suffix)`` identity index collision-free between
same-brain characters and bot cards.

Skill ``level`` is computed from xp via a frozen copy of the
``skill_leveling.compute_level`` thresholds (25/100/250/600); card levels are
the documented grants (consistent with ``xp_engine.level_for_xp``). The
companion test drift-guards both against the live modules.

Downgrade removes ONLY the 4 recruited slugs' ``character_def`` rows — it
never touches cards/skills (progression history survives a downgrade).

Module-global / cross-worker audit: pure DML, no singleton, no cache.

Revision ID: 0254
Revises: 0253
Create Date: 2026-07-02
backwards-compat: safe (additive, conflict-skipping)
"""
from __future__ import annotations

from alembic import op


revision = "0254"
down_revision = "0253"
branch_labels = None
depends_on = None


# Roster definitions for the 4 recruits (config, like 0253's built-in seed).
# The companion test drift-guards brains/guilds/tiers against the live
# registries. Blurbs are the recruit records' role descriptions; on an
# existing DB the ON CONFLICT skip preserves whatever prod carries.
RECRUITED_CHARACTERS: tuple[dict[str, str], ...] = (
    {
        "slug": "iris",
        "display_name": "Iris",
        "brain": "subscription-claude",
        "guild": "isp",
        "max_tier": "L",
        "blurb": "Camera/vision veteran — UVC/IP-camera bring-up and imaging pipelines.",
    },
    {
        "slug": "argus",
        "display_name": "Argus",
        "brain": "subscription-gemini",
        "guild": "auditor",
        "max_tier": "M",
        "blurb": "Security watchman — audits, hardening, and review evidence.",
    },
    {
        "slug": "kai",
        "display_name": "Kai",
        "brain": "subscription-codex",
        "guild": "mobile",
        "max_tier": "M",
        "blurb": "Mobile veteran — Android/iOS app delivery and device integration.",
    },
    {
        "slug": "vega",
        "display_name": "Vega",
        "brain": "subscription-claude",
        "guild": "backend",
        "max_tier": "L",
        "blurb": "Performance-minded backend sibling of Nova — profiling and hot-path work.",
    },
)

RECRUITED_SLUGS: tuple[str, ...] = tuple(r["slug"] for r in RECRUITED_CHARACTERS)

# Grandfather card grants per docs/operations/rpg-grandfather-xp-2026-07-02.md.
# vega deliberately absent (fresh recruit, no grant).
GRANDFATHER_CARDS: tuple[dict[str, object], ...] = (
    {
        "agent_id": "iris",
        "class": "subscription-claude",
        "instance_suffix": "iris",
        "guild": "isp",
        "level": 12,
        "xp": 3280,
    },
    {
        "agent_id": "argus",
        "class": "subscription-gemini",
        "instance_suffix": "argus",
        "guild": "auditor",
        "level": 2,
        "xp": 320,
    },
    {
        "agent_id": "kai",
        "class": "subscription-codex",
        "instance_suffix": "kai",
        "guild": "mobile",
        "level": 4,
        "xp": 710,
    },
)

# Grandfather skill grants: (agent_id, skill_id, xp). branch_choice stays
# NULL — the ledger left all recruit branches PENDING for the operator.
GRANDFATHER_SKILLS: tuple[tuple[str, str, int], ...] = (
    ("iris", "uvc", 390),
    ("iris", "ipcam", 310),
    ("argus", "security_audit", 320),
    ("kai", "mobile_app", 710),
)

# Frozen copy of skill_leveling.LEVEL_THRESHOLDS (a migration must not change
# shape when the module evolves); the companion test drift-guards it.
SKILL_LEVEL_THRESHOLDS: tuple[tuple[int, int], ...] = (
    (5, 600),
    (4, 250),
    (3, 100),
    (2, 25),
)


def compute_skill_level(xp: int) -> int:
    """Frozen equivalent of ``skill_leveling.compute_level`` for the seed xp."""
    for level, threshold in SKILL_LEVEL_THRESHOLDS:
        if xp >= threshold:
            return level
    return 1


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def _insert(
    table: str,
    cols: tuple[str, ...],
    vals: tuple[str, ...],
    conflict_cols: str,
    dialect: str,
) -> str:
    col_sql = ", ".join(cols)
    val_sql = ", ".join(vals)
    if dialect == "postgresql":
        return (
            f"INSERT INTO {table} ({col_sql}) VALUES ({val_sql}) "
            f"ON CONFLICT ({conflict_cols}) DO NOTHING"
        )
    return f"INSERT OR IGNORE INTO {table} ({col_sql}) VALUES ({val_sql})"


def _def_insert(row: dict[str, str], dialect: str) -> str:
    cols = ("slug", "display_name", "brain", "guild", "max_tier", "blurb")
    vals = tuple(f"'{_sql_escape(row[c])}'" for c in cols)
    return _insert("character_def", cols, vals, "slug", dialect)


def _card_insert(row: dict[str, object], dialect: str) -> str:
    cols = ('agent_id', '"class"', 'instance_suffix', 'guild', 'level', 'xp')
    vals = (
        f"'{_sql_escape(str(row['agent_id']))}'",
        f"'{_sql_escape(str(row['class']))}'",
        f"'{_sql_escape(str(row['instance_suffix']))}'",
        f"'{_sql_escape(str(row['guild']))}'",
        str(int(row["level"])),
        str(int(row["xp"])),
    )
    return _insert("agent_character_card", cols, vals, "agent_id", dialect)


def _skill_insert(agent_id: str, skill_id: str, xp: int, dialect: str) -> str:
    cols = ("agent_id", "skill_id", "level", "xp")
    vals = (
        f"'{_sql_escape(agent_id)}'",
        f"'{_sql_escape(skill_id)}'",
        str(compute_skill_level(xp)),
        str(int(xp)),
    )
    return _insert("agent_skill_state", cols, vals, "agent_id, skill_id", dialect)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    for row in RECRUITED_CHARACTERS:
        bind.exec_driver_sql(_def_insert(row, dialect))
    for card in GRANDFATHER_CARDS:
        bind.exec_driver_sql(_card_insert(card, dialect))
    for agent_id, skill_id, xp in GRANDFATHER_SKILLS:
        bind.exec_driver_sql(_skill_insert(agent_id, skill_id, xp, dialect))


def downgrade() -> None:
    # ONLY the 4 recruited definition rows; cards/skills (progression
    # history) are never deleted on downgrade.
    bind = op.get_bind()
    slugs = ", ".join(f"'{_sql_escape(s)}'" for s in RECRUITED_SLUGS)
    bind.exec_driver_sql(f"DELETE FROM character_def WHERE slug IN ({slugs})")
