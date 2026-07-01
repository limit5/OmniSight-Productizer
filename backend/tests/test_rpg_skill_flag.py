"""OP-2505 — RPG.W12 S3 live-activation helper unit tests."""
from __future__ import annotations

import pytest

from backend.agents import rpg_skill_flag


def test_unset_env_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNISIGHT_RPG_SKILL_XP_ENABLED", raising=False)
    assert rpg_skill_flag.skill_xp_enabled() is False


@pytest.mark.parametrize(
    "value",
    ["1", "true", "yes", "on", "TRUE", "Yes", "On", " 1 ", "  true  "],
)
def test_truthy_values_enable(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("OMNISIGHT_RPG_SKILL_XP_ENABLED", value)
    assert rpg_skill_flag.skill_xp_enabled() is True


@pytest.mark.parametrize(
    "value",
    ["0", "false", "no", "off", "", "maybe", "2", "enabled"],
)
def test_non_truthy_values_disable(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("OMNISIGHT_RPG_SKILL_XP_ENABLED", value)
    assert rpg_skill_flag.skill_xp_enabled() is False


def test_flag_read_is_not_cached_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNISIGHT_RPG_SKILL_XP_ENABLED", raising=False)
    assert rpg_skill_flag.skill_xp_enabled() is False

    monkeypatch.setenv("OMNISIGHT_RPG_SKILL_XP_ENABLED", "on")
    assert rpg_skill_flag.skill_xp_enabled() is True

    monkeypatch.setenv("OMNISIGHT_RPG_SKILL_XP_ENABLED", "off")
    assert rpg_skill_flag.skill_xp_enabled() is False


def test_module_imports_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNISIGHT_RPG_SKILL_XP_ENABLED", raising=False)
    import importlib

    reloaded = importlib.reload(rpg_skill_flag)
    assert reloaded.skill_xp_enabled() is False
