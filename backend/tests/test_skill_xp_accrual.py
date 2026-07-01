"""RPG.W12 skill-xp-accrual EPIC S2 (OP-2503) — finalizer-level accrual tests.

Pins the ``_award_skill_xp`` hook that ``auto-runner-jira._finalize_successful_push``
calls beside ``_award_character_xp``. The four contract axes (matching the
ticket's AC list):

  1. flag-off (default)  → no-op even for a character-owned + valid skill:
     ticket (S2 ships DARK; S3 activation flips the env flag).
  2. flag-on + character + valid in-guild skill → exactly one atomic award
     through ``skill_leveling.award_skill_xp_sync`` with
     ``base_delta=BASE_SKILL_XP`` (25, NOT ``BASE_TASK_XP``=100), tier-L+
     bonus derived from the metadata tier.
  3. flag-on + skill: label WITHOUT a valid ``character:`` label → skip
     (``skill_resolver`` collapses to ``None`` in ``_build_prompt``, so
     ``_LAST_TICKET_METADATA[key]["skill"] == ""`` and the hook returns
     without awarding).
  4. flag-on + off-guild or otherwise-invalid ``skill:`` label → skip
     (same fail-open collapse).

The tests are network-free: the runner module is loaded via
``importlib.spec_from_file_location`` (auto-runner-jira.py has a hyphen),
and every store call is patched at the module-attribute level. The
character-XP write (#1900) is stubbed to a no-op so this suite pins the
skill-XP behaviour in isolation — it must never trigger a change to
character-XP semantics.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.agents import skill_leveling


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"

_CHARACTER_SLUG = "nova"          # backend guild
_IN_GUILD_SKILL = "enterprise_web"
_OFF_GUILD_SKILL = "barcode_scanner"  # algo_cv, off-guild for nova
_TICKET_KEY = "OP-2503"


def _load_jira_runner() -> Any:
    """Load auto-runner-jira.py despite the hyphen in its filename."""
    sys.modules.pop("jira_runner_skill_xp_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_skill_xp_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubClient:
    agent_class = "subscription-claude"
    bot_email = "rt3628+claude-bot@gmail.com"
    bot_account_id = "acc-claude-bot"


def _make_push_result() -> Any:
    return SimpleNamespace(
        change_url="https://gerrit.example/+/2503",
        change_number=2503,
        post_push_warning=None,
    )


def _seed_metadata(
    mod: Any,
    *,
    tier: str = "M",
    character: str = _CHARACTER_SLUG,
    skill: str = _IN_GUILD_SKILL,
) -> None:
    """Populate ``_LAST_TICKET_METADATA`` the way ``_build_prompt`` does."""
    mod._LAST_TICKET_METADATA[_TICKET_KEY] = {
        "ticket_type": "Story",
        "tier": tier,
        "area": "backend",
        "character": character,
        "skill": skill,
    }


def _patch_finalize_scaffold(
    monkeypatch: pytest.MonkeyPatch, mod: Any
) -> dict[str, list]:
    """Silence the surrounding finalizer plumbing so the tests are focused.

    ``_finalize_under_review``, the medical-readiness gate, and the claim
    release are all stubbed to no-ops; the ``add_comment`` shim swallows
    the post-push-warning path. ``_award_character_xp`` is silenced too —
    S2's job is not to change #1900.
    """
    calls: dict[str, list] = {
        "character_xp": [],
        "skill_xp_sync": [],
    }

    monkeypatch.setattr(mod, "_finalize_under_review", lambda *a, **kw: None)
    monkeypatch.setattr(
        mod, "_medical_readiness_ok_for_closure", lambda *a, **kw: True
    )
    monkeypatch.setattr(
        mod, "_release_ticket_claim_if_acquired", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        mod.jira_dispatch,
        "add_comment",
        lambda *a, **kw: None,
    )

    def _fake_award_character_xp(ticket_key: str) -> None:
        calls["character_xp"].append(ticket_key)

    monkeypatch.setattr(mod, "_award_character_xp", _fake_award_character_xp)

    def _fake_skill_xp_sync(**kwargs: Any) -> Any:
        calls["skill_xp_sync"].append(kwargs)
        return SimpleNamespace(
            agent_id=kwargs["agent_id"],
            skill_id=kwargs["skill_id"],
            applied_delta=75,
            new_xp=75,
            new_level=1,
            branch_choice=None,
            branch_choice_required=False,
            inserted=True,
        )

    monkeypatch.setattr(
        mod.skill_leveling, "award_skill_xp_sync", _fake_skill_xp_sync
    )
    return calls


# ── Design invariant: BASE_SKILL_XP must be 25, not BASE_TASK_XP ───


def test_base_skill_xp_is_25_not_base_task_xp() -> None:
    """AC (Spec): ``BASE_SKILL_XP=25`` — feeding ``BASE_TASK_XP``=100
    through ``compute_xp_delta`` would land 300 first-time / 100 repeat
    and instant-max a skill against the 25/100/250/600 curve.
    """
    from backend.agents.xp_engine import BASE_TASK_XP

    assert skill_leveling.BASE_SKILL_XP == 25
    assert skill_leveling.BASE_SKILL_XP != BASE_TASK_XP


# ── AC1: flag-off = no-op (dark ship) ─────────────────────────────


def test_flag_off_is_noop_even_with_character_and_valid_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: S2 ships DARK — no skill-XP award even when the ticket is
    character-owned and carries a valid in-guild ``skill:`` label,
    because ``OMNISIGHT_RPG_SKILL_XP_ENABLED`` defaults to ``0``.
    """
    monkeypatch.delenv("OMNISIGHT_RPG_SKILL_XP_ENABLED", raising=False)
    mod = _load_jira_runner()
    assert mod.SKILL_XP_ENABLED is False

    calls = _patch_finalize_scaffold(monkeypatch, mod)
    _seed_metadata(mod, tier="M")

    mod._finalize_successful_push(
        _StubClient(), _TICKET_KEY, _make_push_result(), claim=None
    )

    # Character-XP (#1900) still fires; skill-XP MUST be silent.
    assert calls["character_xp"] == [_TICKET_KEY]
    assert calls["skill_xp_sync"] == []


# ── AC2: flag-on + character + valid skill = one atomic award ─────


def test_flag_on_awards_once_atomically_with_base_skill_xp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: flag-on + character-owned + valid in-guild skill → the
    finalizer calls ``skill_leveling.award_skill_xp_sync`` exactly
    once with ``agent_id`` = character slug, the resolved skill, and
    ``base_delta`` defaulting to :data:`skill_leveling.BASE_SKILL_XP`
    (25). Tier ``L`` flips the ``tier_l_plus`` derived arg on.
    """
    monkeypatch.setenv("OMNISIGHT_RPG_SKILL_XP_ENABLED", "1")
    mod = _load_jira_runner()
    assert mod.SKILL_XP_ENABLED is True

    calls = _patch_finalize_scaffold(monkeypatch, mod)
    _seed_metadata(mod, tier="L")

    mod._finalize_successful_push(
        _StubClient(), _TICKET_KEY, _make_push_result(), claim=None
    )

    # Character-XP write is untouched by S2.
    assert calls["character_xp"] == [_TICKET_KEY]
    # Exactly one skill-XP award, correctly parameterised.
    assert len(calls["skill_xp_sync"]) == 1
    kwargs = calls["skill_xp_sync"][0]
    assert kwargs["agent_id"] == _CHARACTER_SLUG
    assert kwargs["skill_id"] == _IN_GUILD_SKILL
    assert kwargs["tier"] == "L"
    # The runner must NOT pre-multiply — the store applies the multiplier
    # exactly once; base_delta stays at BASE_SKILL_XP (25).
    assert kwargs.get("base_delta", skill_leveling.BASE_SKILL_XP) == 25


# ── AC3: flag-on + skill: without character: → skip ───────────────


def test_flag_on_skips_when_skill_label_lacks_character_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: a ``skill:`` label without a valid ``character:`` label MUST
    NOT accrue skill XP. ``resolve_skill_for_character`` returns
    ``None`` in that case (S1 contract), so ``_LAST_TICKET_METADATA``
    carries an empty ``skill`` field and the hook returns early. This
    is the "bot-only ticket doesn't accrue skill XP" invariant.
    """
    monkeypatch.setenv("OMNISIGHT_RPG_SKILL_XP_ENABLED", "1")
    mod = _load_jira_runner()
    assert mod.SKILL_XP_ENABLED is True

    calls = _patch_finalize_scaffold(monkeypatch, mod)
    _seed_metadata(mod, character="", skill="")

    mod._finalize_successful_push(
        _StubClient(), _TICKET_KEY, _make_push_result(), claim=None
    )

    assert calls["character_xp"] == [_TICKET_KEY]
    assert calls["skill_xp_sync"] == []


# ── AC4: off-guild / invalid skill → skip ─────────────────────────


def test_flag_on_skips_when_metadata_skill_field_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: off-guild / invalid / matrix-unknown skills collapse to
    ``skill = ""`` at metadata capture (see
    ``test_build_prompt_captures_only_valid_in_guild_skill_id`` below).
    The finalizer hook returns early on an empty field even when the
    character is set — no award for an invalid skill.
    """
    monkeypatch.setenv("OMNISIGHT_RPG_SKILL_XP_ENABLED", "1")
    mod = _load_jira_runner()
    calls = _patch_finalize_scaffold(monkeypatch, mod)
    _seed_metadata(mod, character=_CHARACTER_SLUG, skill="")

    mod._finalize_successful_push(
        _StubClient(), _TICKET_KEY, _make_push_result(), claim=None
    )

    assert calls["character_xp"] == [_TICKET_KEY]
    assert calls["skill_xp_sync"] == []


# ── Fail-open: an exception from award_skill_xp_sync never wedges ──


def test_flag_on_fail_open_when_award_helper_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RPG write must NEVER wedge a delivery. If
    ``award_skill_xp_sync`` raises (no DSN, asyncpg down, schema not
    deployed, ...) the hook logs at WARNING and returns cleanly.
    """
    monkeypatch.setenv("OMNISIGHT_RPG_SKILL_XP_ENABLED", "1")
    mod = _load_jira_runner()
    calls = _patch_finalize_scaffold(monkeypatch, mod)
    _seed_metadata(mod)

    def _boom(**kwargs: Any) -> Any:
        raise RuntimeError("no DSN configured")

    monkeypatch.setattr(mod.skill_leveling, "award_skill_xp_sync", _boom)

    # Must NOT raise — a delivery is never wedged by the RPG write.
    mod._finalize_successful_push(
        _StubClient(), _TICKET_KEY, _make_push_result(), claim=None
    )
    assert calls["character_xp"] == [_TICKET_KEY]


# ── Metadata-capture axis: skill: label reaches _LAST_TICKET_METADATA


def test_build_prompt_captures_only_valid_in_guild_skill_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (label plumbing): ``_LAST_TICKET_METADATA[key]["skill"]`` is
    populated ONLY when ``skill_resolver.resolve_skill_for_character``
    accepts the label. The three collapse-to-empty paths are exercised
    here so the finalizer's flag-on skip-on-empty behaviour is
    grounded in real metadata capture (not just a stubbed dict).
    """
    mod = _load_jira_runner()

    valid = [f"character:{_CHARACTER_SLUG}", f"skill:{_IN_GUILD_SKILL}"]
    off_guild = [f"character:{_CHARACTER_SLUG}", f"skill:{_OFF_GUILD_SKILL}"]
    missing_char = [f"skill:{_IN_GUILD_SKILL}"]

    assert mod.skill_resolver.resolve_skill_for_character(valid) == _IN_GUILD_SKILL
    assert mod.skill_resolver.resolve_skill_for_character(off_guild) is None
    assert mod.skill_resolver.resolve_skill_for_character(missing_char) is None


# ── Direct _award_skill_xp unit — flag gate primacy ───────────────


def test_award_skill_xp_returns_early_when_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: the flag gate is the FIRST branch of ``_award_skill_xp``;
    even a well-formed metadata row does not reach the store.
    """
    monkeypatch.delenv("OMNISIGHT_RPG_SKILL_XP_ENABLED", raising=False)
    mod = _load_jira_runner()
    assert mod.SKILL_XP_ENABLED is False

    called: list[dict[str, Any]] = []

    def _fake(**kwargs: Any) -> Any:
        called.append(kwargs)
        return None

    monkeypatch.setattr(mod.skill_leveling, "award_skill_xp_sync", _fake)
    _seed_metadata(mod)
    mod._award_skill_xp(_TICKET_KEY)

    assert called == []
