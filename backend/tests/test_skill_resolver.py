"""RPG.W12 skill-xp-accrual EPIC S1 (OP-2500) -- skill_resolver contract tests.

Pins the ``resolve_skill_for_character(labels) -> str | None`` contract of
:mod:`backend.agents.skill_resolver` plus the ``scripts/file_jira_ticket.py``
``--skill`` filer flag it depends on. The resolver is deliberately pure and
fail-open — every failure mode returns ``None`` — so the tests cover both the
"resolves to the skill" happy path and every "returns None because …" fallback
so a later stage cannot silently regress the safe floor.

Four AC axes exercised (matching the ticket's AC list):
  1. valid character + in-guild skill  → skill_id
  2. missing character                 → None
  3. missing / invalid skill           → None
  4. off-guild skill                   → None
Plus the filer AC: ``--check --character nova --skill enterprise_web`` prints
the ``skill:enterprise_web`` label; off-guild ``--skill`` rejects at filing
time (matrix is guild-keyed → a character trains only what its guild owns).
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

from backend.agents import character_registry
from backend.agents.skill_matrix import load_skill_matrix
from backend.agents.skill_resolver import resolve_skill_for_character
from backend.sandbox_tier import Guild


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "file_jira_ticket.py"


def _load_filer():
    spec = importlib.util.spec_from_file_location("file_jira_ticket", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _pick_character_with_in_guild_skill() -> tuple[str, str]:
    """Return ``(character_slug, skill_id)`` for a character whose guild owns skills.

    Prefers the ticket's named case (``nova`` + ``enterprise_web``) so the
    "positive resolves" path exercises the exact pair the AC calls out; falls
    back to the first character whose guild has any matrix-declared skill so
    the test survives roster / matrix churn.
    """

    matrix = load_skill_matrix()
    if (
        "nova" in character_registry.CHARACTERS
        and Guild(character_registry.CHARACTERS["nova"].guild) in matrix
        and any(
            definition.skill_id == "enterprise_web"
            for definition in matrix[Guild(character_registry.CHARACTERS["nova"].guild)]
        )
    ):
        return "nova", "enterprise_web"
    for slug, character in character_registry.CHARACTERS.items():
        try:
            guild = Guild(character.guild)
        except ValueError:
            continue
        skills = matrix.get(guild) or ()
        if skills:
            return slug, skills[0].skill_id
    pytest.skip("no character in the registry has an in-guild skill declared")


def _pick_off_guild_skill_for(character_slug: str) -> str:
    matrix = load_skill_matrix()
    character = character_registry.CHARACTERS[character_slug]
    for guild, skills in matrix.items():
        if guild.value == character.guild:
            continue
        if skills:
            return skills[0].skill_id
    pytest.skip(
        f"no off-guild skill exists for {character_slug} — matrix has only "
        f"one populated guild"
    )


# ── AC1: valid character + in-guild skill → skill_id ──────────────────────


def test_resolves_when_character_and_skill_are_in_guild() -> None:
    """AC: ``resolve_skill_for_character`` returns the skill for a valid
    character with an in-guild skill label."""

    slug, skill = _pick_character_with_in_guild_skill()
    assert resolve_skill_for_character(
        [f"character:{slug}", f"skill:{skill}"]
    ) == skill


def test_nova_plus_enterprise_web_is_the_ticket_named_case() -> None:
    """The ticket AC #4 names ``nova`` + ``enterprise_web`` as the exercised
    happy-path case; if this ever stops resolving, S1's safe floor is broken."""

    # Guard: skip if the roster / matrix no longer contains this exact pair —
    # the more general test above still enforces the invariant.
    if "nova" not in character_registry.CHARACTERS:
        pytest.skip("character 'nova' is not in the registry")
    if "enterprise_web" not in {
        definition.skill_id
        for definitions in load_skill_matrix().values()
        for definition in definitions
    }:
        pytest.skip("skill 'enterprise_web' is not in the canonical matrix")
    assert resolve_skill_for_character(
        ["character:nova", "skill:enterprise_web"]
    ) == "enterprise_web"


def test_extra_unrelated_labels_do_not_interfere() -> None:
    """Well-formed extra labels (class, tier, area, ...) must not affect the
    resolver — it looks only at ``character:`` and ``skill:``."""

    slug, skill = _pick_character_with_in_guild_skill()
    labels = [
        "agent:auto",
        "tier:S",
        "area:backend",
        "class:subscription-claude",
        f"character:{slug}",
        f"skill:{skill}",
        "priority:meta",
    ]
    assert resolve_skill_for_character(labels) == skill


# ── AC2: missing character → None ─────────────────────────────────────────


def test_returns_none_when_no_character_label_present() -> None:
    _, skill = _pick_character_with_in_guild_skill()
    assert resolve_skill_for_character([f"skill:{skill}"]) is None


def test_returns_none_for_unknown_character_slug() -> None:
    """``character_from_labels`` fails-open on unknown slugs — resolver must too."""

    _, skill = _pick_character_with_in_guild_skill()
    assert resolve_skill_for_character(
        ["character:no_such_character_slug", f"skill:{skill}"]
    ) is None


# ── AC3: missing / invalid skill → None ───────────────────────────────────


def test_returns_none_when_no_skill_label_present() -> None:
    slug, _ = _pick_character_with_in_guild_skill()
    assert resolve_skill_for_character([f"character:{slug}"]) is None


def test_returns_none_for_off_matrix_skill_id() -> None:
    """A ``skill:<slug>`` that is not declared in the canonical matrix falls
    open to ``None`` — bad label cannot escape as XP accrual."""

    slug, _ = _pick_character_with_in_guild_skill()
    assert resolve_skill_for_character(
        [f"character:{slug}", "skill:ghost_skill_not_in_matrix"]
    ) is None


def test_returns_none_for_empty_skill_value() -> None:
    slug, _ = _pick_character_with_in_guild_skill()
    assert resolve_skill_for_character([f"character:{slug}", "skill:"]) is None


def test_returns_none_for_whitespace_only_skill_value() -> None:
    slug, _ = _pick_character_with_in_guild_skill()
    assert resolve_skill_for_character([f"character:{slug}", "skill:   "]) is None


# ── AC4: off-guild skill → None (the guild-keyed invariant) ───────────────


def test_returns_none_for_off_guild_skill() -> None:
    """A skill that IS in the matrix but under a different guild than the
    character's must resolve to ``None`` — this is the "characters may only
    train what their guild owns" invariant."""

    slug, _ = _pick_character_with_in_guild_skill()
    off_guild_skill = _pick_off_guild_skill_for(slug)
    assert resolve_skill_for_character(
        [f"character:{slug}", f"skill:{off_guild_skill}"]
    ) is None


def test_pixel_plus_enterprise_web_is_off_guild() -> None:
    """Concrete: pixel is frontend guild, enterprise_web is backend guild →
    off-guild → None. Skips if the exact pair no longer exists in the fixtures."""

    if "pixel" not in character_registry.CHARACTERS:
        pytest.skip("character 'pixel' is not in the registry")
    if character_registry.CHARACTERS["pixel"].guild == "backend":
        pytest.skip("pixel is on the backend guild now — pair is not off-guild")
    matrix = load_skill_matrix()
    if not any(
        definition.skill_id == "enterprise_web"
        for definitions in matrix.values()
        for definition in definitions
    ):
        pytest.skip("skill 'enterprise_web' is not in the canonical matrix")
    assert resolve_skill_for_character(
        ["character:pixel", "skill:enterprise_web"]
    ) is None


def test_returns_none_when_character_guild_has_no_skills_declared() -> None:
    """A character whose guild has NO skills in the matrix (a valid state
    while the matrix is being filled in) must always resolve to ``None`` —
    it has no allowed skills, off-guild for everything."""

    matrix = load_skill_matrix()
    for slug, character in character_registry.CHARACTERS.items():
        try:
            guild = Guild(character.guild)
        except ValueError:
            continue
        if not matrix.get(guild):
            # Try every skill in the matrix — none should resolve.
            for definitions in matrix.values():
                for definition in definitions:
                    assert resolve_skill_for_character(
                        [f"character:{slug}", f"skill:{definition.skill_id}"]
                    ) is None
            return
    pytest.skip("every registered character's guild has skills in the matrix")


# ── Fail-open / purity ────────────────────────────────────────────────────


def test_returns_none_for_none_labels_iterable() -> None:
    assert resolve_skill_for_character(None) is None


def test_returns_none_for_empty_labels_iterable() -> None:
    assert resolve_skill_for_character([]) is None


def test_ignores_non_string_label_entries() -> None:
    """Non-string entries in ``labels`` must be silently skipped, not raise."""

    slug, skill = _pick_character_with_in_guild_skill()
    assert resolve_skill_for_character(
        [None, 42, object(), f"character:{slug}", f"skill:{skill}"]
    ) == skill


def test_never_raises_on_malformed_iterable() -> None:
    """A pathological iterable that raises during iteration must be
    absorbed into the fail-open ``None`` return."""

    def boom():
        raise RuntimeError("iteration exploded")
        yield  # pragma: no cover  (unreachable — declares this as a generator)

    assert resolve_skill_for_character(boom()) is None


# ── AC4 (filer): --skill emits the label + off-guild rejects ──────────────


def _filer_check_args(mod, **overrides) -> argparse.Namespace:
    """Build a filer argparse.Namespace pre-populated for a --check run."""

    values = {
        "summary": "OP-2500 synthetic --skill check",
        "description_file": "",
        "priority": "Medium",
        "tier": "S",
        "cls": None,
        "character": None,
        "type": "feature",
        "areas": ["backend", "tests"],
        "scope": None,
        "check": True,
        "force": False,
        "no_push_capability": False,
        "capability": [],
        "assignee": None,
        "no_agent_auto": False,
        "skill": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_filer_labels_emit_skill_when_flag_set() -> None:
    """AC #4 filer half: --character nova --skill enterprise_web puts a
    ``skill:enterprise_web`` label into the outgoing label set."""

    if "nova" not in character_registry.CHARACTERS:
        pytest.skip("character 'nova' is not in the registry")
    mod = _load_filer()
    args = _filer_check_args(
        mod,
        cls="subscription-claude",
        character="nova",
        skill="enterprise_web",
    )
    labels = mod._labels(args)
    assert "character:nova" in labels
    assert "skill:enterprise_web" in labels


def test_filer_omits_skill_label_when_flag_not_set() -> None:
    """No --skill → no ``skill:*`` label; the filer must not synthesise one."""

    mod = _load_filer()
    args = _filer_check_args(mod, cls="subscription-claude")
    labels = mod._labels(args)
    assert not any(label.startswith("skill:") for label in labels)


def test_filer_check_prints_skill_label(tmp_path, capsys) -> None:
    """AC #4: ``file_jira_ticket.py --check --character nova --skill
    enterprise_web`` prints the ``skill:enterprise_web`` label. Uses --check
    so no network is touched."""

    if "nova" not in character_registry.CHARACTERS:
        pytest.skip("character 'nova' is not in the registry")
    mod = _load_filer()
    desc = tmp_path / "desc.md"
    desc.write_text(
        "## Acceptance Criteria\n- backend/agents/skill_resolver.py exists\n"
        "## Files / Paths\n- backend/agents/skill_resolver.py\n",
        encoding="utf-8",
    )
    rc = mod.main(
        [
            "--summary", "OP-2500 filer --check",
            "--description-file", str(desc),
            "--priority", "Medium",
            "--tier", "S",
            "--character", "nova",
            "--skill", "enterprise_web",
            "--type", "feature",
            "--areas", "backend,tests",
            "--check",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "'skill:enterprise_web'" in out
    assert "'character:nova'" in out


def test_filer_rejects_off_guild_skill_at_filing() -> None:
    """AC #4: off-guild --skill rejects — the guild-keyed invariant.
    pixel is frontend guild; enterprise_web is a backend skill → rejected."""

    if "pixel" not in character_registry.CHARACTERS:
        pytest.skip("character 'pixel' is not in the registry")
    if character_registry.CHARACTERS["pixel"].guild == "backend":
        pytest.skip("pixel is on the backend guild now — pair is not off-guild")
    mod = _load_filer()
    with pytest.raises(SystemExit) as excinfo:
        mod._apply_character(
            _filer_check_args(mod, character="pixel", skill="enterprise_web")
        )
    message = str(excinfo.value)
    assert "off-guild" in message
    assert "enterprise_web" in message
    assert "pixel" in message


def test_filer_rejects_skill_without_character() -> None:
    """--skill without --character is nonsensical (no guild to check against)
    and must reject early — a "one of --class/--character" style guard."""

    mod = _load_filer()
    with pytest.raises(SystemExit) as excinfo:
        mod._apply_character(
            _filer_check_args(
                mod, cls="subscription-claude", character=None, skill="enterprise_web"
            )
        )
    assert "--skill requires --character" in str(excinfo.value)


def test_filer_accepts_in_guild_skill_at_filing() -> None:
    """The positive path: --skill in the character's guild passes
    _apply_character without exit."""

    if "nova" not in character_registry.CHARACTERS:
        pytest.skip("character 'nova' is not in the registry")
    mod = _load_filer()
    args = _filer_check_args(mod, character="nova", skill="enterprise_web")
    # Must not raise; brain-derivation still runs.
    mod._apply_character(args)
    assert args.cls == character_registry.CHARACTERS["nova"].brain
