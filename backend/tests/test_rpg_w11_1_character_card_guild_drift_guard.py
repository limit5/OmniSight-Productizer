"""RPG.W11.1 -- drift guard: ``agent_character_card.guild`` ⊆ ``guild_registry.GUILDS``.

ADR-0008 makes ``backend.agents.guild_registry.GUILDS`` the single source of
truth for Guild slugs in the RPG layer. The ``agent_character_card`` storage
shape — Python-side constants in ``backend.agents.character_card`` and the
SQL-side defaults in ``backend/alembic/versions/0239_agent_character_card.py``
— must never declare a Guild slug that the registry does not also know about.

This test pins three subset invariants the W11.1 guard promises:

1. ``character_card.CHARACTER_CARD_GUILDS`` ⊆ ``guild_registry.GUILDS``.
   This is the static set the character-card module exposes; any element
   outside ``GUILDS`` would let an agent be born into a Guild that
   ``guild_registry`` does not enumerate.

2. ``character_card.DEFAULT_GUILD`` ∈ ``guild_registry.GUILDS``. The
   bootstrap default applied by ``FirstTaskCharacterCard.to_create`` /
   ``CharacterCardCreate`` must be a real registry slug, or every fresh
   character card produced from the no-task-area path would itself be
   drift.

3. The alembic ``agent_character_card.guild`` column ``DEFAULT`` literal
   ∈ ``guild_registry.GUILDS`` and matches ``DEFAULT_GUILD`` exactly.
   The SQL and Python defaults must agree, otherwise a row INSERT-ed
   without ``guild`` from raw SQL would diverge from one INSERT-ed
   through the ORM/store.

A failure here means the character-card layer has drifted off the RPG
Guild registry. The fix is to align whichever side genuinely changed —
never to relax these assertions.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.agents.character_card import (
    CHARACTER_CARD_GUILDS,
    DEFAULT_GUILD,
)
from backend.agents.guild_registry import GUILDS

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    REPO_ROOT
    / "backend"
    / "alembic"
    / "versions"
    / "0239_agent_character_card.py"
)

_GUILD_DEFAULT_RE = re.compile(
    r"""guild\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'([^']+)'""",
    re.IGNORECASE,
)


def _migration_guild_defaults() -> frozenset[str]:
    """Return every ``guild ... DEFAULT '<slug>'`` literal in the migration."""

    text = MIGRATION_PATH.read_text(encoding="utf-8")
    return frozenset(_GUILD_DEFAULT_RE.findall(text))


def test_character_card_guilds_subset_of_registry() -> None:
    """RPG.W11.1 invariant #1: ``CHARACTER_CARD_GUILDS`` ⊆ ``GUILDS``.

    Every Guild slug the character-card module declares must already be
    a registered RPG Guild. If this fails, either add the slug to
    ``guild_registry.GUILD_DEFINITIONS`` (and the upstream BP.B Guild
    enum) or remove it from ``CHARACTER_CARD_GUILDS`` — never delete
    this assertion.
    """

    extra = CHARACTER_CARD_GUILDS - GUILDS
    assert not extra, (
        "agent_character_card.guild drifted outside guild_registry.GUILDS. "
        f"Slugs missing from registry: {sorted(extra)!r}. "
        f"Registry currently knows: {sorted(GUILDS)!r}."
    )


def test_default_guild_is_in_registry() -> None:
    """RPG.W11.1 invariant #2: ``DEFAULT_GUILD`` ∈ ``GUILDS``.

    First-task bootstrap falls back to ``DEFAULT_GUILD`` whenever the
    operator has not supplied a Guild override. If that default is not
    in the registry, every bootstrap card is drift on day one.
    """

    assert DEFAULT_GUILD in GUILDS, (
        f"character_card.DEFAULT_GUILD={DEFAULT_GUILD!r} is not a registered "
        f"RPG Guild. Registry currently knows: {sorted(GUILDS)!r}."
    )


def test_migration_default_matches_registry_and_python_default() -> None:
    """RPG.W11.1 invariant #3: SQL ``guild`` DEFAULT ⊆ ``GUILDS`` and == ``DEFAULT_GUILD``.

    The alembic migration declares the Postgres + SQLite ``guild``
    column ``DEFAULT`` literal. That literal must be a registry slug
    and must equal the Python-side ``DEFAULT_GUILD`` constant so a row
    written without an explicit ``guild`` value is identical regardless
    of which insertion path created it.
    """

    sql_defaults = _migration_guild_defaults()
    assert sql_defaults, (
        f"could not find any `guild ... DEFAULT '<slug>'` literal in "
        f"{MIGRATION_PATH.relative_to(REPO_ROOT)}; the regex may need "
        "to be updated if the migration format changed."
    )

    extra = sql_defaults - GUILDS
    assert not extra, (
        "agent_character_card.guild SQL DEFAULT drifted outside "
        f"guild_registry.GUILDS: {sorted(extra)!r}. "
        f"Registry currently knows: {sorted(GUILDS)!r}."
    )

    mismatched = {slug for slug in sql_defaults if slug != DEFAULT_GUILD}
    assert not mismatched, (
        "agent_character_card.guild SQL DEFAULT disagrees with "
        f"character_card.DEFAULT_GUILD={DEFAULT_GUILD!r}: "
        f"SQL declares {sorted(sql_defaults)!r}. Align both sides."
    )
