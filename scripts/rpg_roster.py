"""OP-2512 -- merged RPG roster CLI (built-ins + DB-recruited characters).

Live-fleet smoke proof for the DB-backed character registry (RECRUIT C2,
``docs/architecture/2026-07-02-character-recruit-epic-design.md``): prints
the effective roster from
:func:`backend.agents.character_registry.load_characters`
(``include_retired=True`` — code built-ins merged with recruited
``character_def`` rows) joined with each character's progression card
(level/xp from ``agent_character_card``) and skill rows
(``agent_skill_state``), as an aligned table. A DB-recruited character
(e.g. ``vega``) must appear alongside the 4 built-ins.

READ-ONLY: the script never mutates any table. DB access uses the same env
DSN convention as the registry loader (OMNISIGHT_DATABASE_URL /
DATABASE_URL / OMNI_TEST_PG_URL); with no reachable DB it degrades to the
built-in roster plus a stderr notice.

Usage::

    python -m scripts.rpg_roster            # aligned table
    python -m scripts.rpg_roster --json     # same data as JSON
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_DB_CONNECT_TIMEOUT_SECONDS = 3

_HEADERS = (
    "SLUG", "NAME", "BRAIN", "GUILD", "TIER",
    "ACTIVE", "BUILT_IN", "LV", "XP", "SKILLS",
)


def _fetch_progression() -> tuple[dict[str, dict[str, int]], dict[str, list[dict[str, Any]]]]:
    """Read-only fetch of card level/xp + skill rows, keyed by agent_id (slug).

    Raises on ANY failure (missing psycopg2, no DSN, connect/query error);
    :func:`main` translates the failure into blank card/skill columns plus a
    stderr notice — the roster itself still prints.
    """
    import psycopg2  # lazy: the script must run without DB deps installed

    # Same DSN resolution as the registry loader (design §C2).
    from backend.agents.provider_quota_tracker import _resolve_dsn

    dsn = _resolve_dsn()
    if not dsn:
        raise RuntimeError(
            "no PostgreSQL DSN via OMNISIGHT_DATABASE_URL / DATABASE_URL / "
            "OMNI_TEST_PG_URL"
        )
    conn = psycopg2.connect(dsn, connect_timeout=_DB_CONNECT_TIMEOUT_SECONDS)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT agent_id, level, xp FROM agent_character_card")
                cards = {
                    str(agent_id): {"level": level, "xp": xp}
                    for agent_id, level, xp in cur.fetchall()
                }
                cur.execute(
                    "SELECT agent_id, skill_id, level, branch_choice "
                    "FROM agent_skill_state ORDER BY agent_id, skill_id"
                )
                skills: dict[str, list[dict[str, Any]]] = {}
                for agent_id, skill_id, level, branch in cur.fetchall():
                    skills.setdefault(str(agent_id), []).append(
                        {"skill_id": skill_id, "level": level, "branch": branch}
                    )
        return cards, skills
    finally:
        conn.close()


def _format_skill(skill: Mapping[str, Any]) -> str:
    """``skill:LvN(branch)`` — the parenthesised branch only once locked."""
    base = f"{skill['skill_id']}:Lv{skill['level']}"
    branch = skill.get("branch")
    return f"{base}({branch})" if branch else base


def build_roster(
    characters: Mapping[str, Any],
    cards: Mapping[str, Mapping[str, int]],
    skills: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Join the merged roster with progression; one dict per character."""
    from backend.agents.character_registry import CHARACTERS

    rows = []
    for slug in sorted(characters):
        char = characters[slug]
        card = cards.get(slug)
        rows.append({
            "slug": slug,
            "display_name": char.display_name,
            "brain": char.brain,
            "guild": char.guild,
            "max_tier": char.max_tier,
            "active": char.active,
            "built_in": slug in CHARACTERS,
            "level": card["level"] if card else None,
            "xp": card["xp"] if card else None,
            "skills": [_format_skill(s) for s in skills.get(slug, [])],
        })
    return rows


def render_table(rows: list[dict[str, Any]]) -> str:
    table = [_HEADERS]
    for row in rows:
        table.append((
            row["slug"],
            row["display_name"],
            row["brain"],
            row["guild"],
            row["max_tier"],
            "yes" if row["active"] else "no",
            "yes" if row["built_in"] else "no",
            "" if row["level"] is None else str(row["level"]),
            "" if row["xp"] is None else str(row["xp"]),
            " ".join(row["skills"]),
        ))
    widths = [max(len(r[i]) for r in table) for i in range(len(_HEADERS))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(r, widths)).rstrip()
        for r in table
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the merged RPG character roster (built-ins + "
        "DB-recruited) with progression cards and skills.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the roster as JSON instead of an aligned table",
    )
    args = parser.parse_args(argv)

    from backend.agents.character_registry import load_characters, registry_db_loaded

    characters = load_characters(include_retired=True)
    db_loaded = registry_db_loaded()
    if not db_loaded:
        print(
            "NOTICE: character_def DB unreachable — built-in roster only",
            file=sys.stderr,
        )

    try:
        cards, skills = _fetch_progression()
        progression_loaded = True
    except Exception as exc:  # noqa: BLE001 — any DB failure degrades gracefully
        cards, skills = {}, {}
        progression_loaded = False
        print(
            f"NOTICE: progression DB unreachable ({exc}) — "
            f"card/skill columns blank",
            file=sys.stderr,
        )

    rows = build_roster(characters, cards, skills)
    if args.json:
        print(json.dumps(
            {
                "registry_db_loaded": db_loaded,
                "progression_db_loaded": progression_loaded,
                "characters": rows,
            },
            indent=2,
        ))
    else:
        print(render_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
