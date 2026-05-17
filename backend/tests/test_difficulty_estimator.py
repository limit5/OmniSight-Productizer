"""Regression coverage for the Meta-LLM difficulty estimator edge cases."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agents.difficulty_estimator import (
    DifficultyClass,
    estimate_difficulty,
    is_enabled,
    provider_preference_index,
    provider_preference_multiplier,
)


def test_partial_task_shape_defaults_to_trivial() -> None:
    estimate = estimate_difficulty(SimpleNamespace(prompt=None))

    assert estimate.difficulty is DifficultyClass.TRIVIAL
    assert estimate.score == 0.0
    assert estimate.recommended_provider_families == (
        "ollama",
        "openai",
        "gemini",
        "anthropic",
    )


def test_dict_task_shape_preserves_baseline_medium_rounding() -> None:
    estimate = estimate_difficulty(
        {
            "tier": "m",
            "size": "m",
            "agent_class": "subscription-codex",
            "prompt": "x" * 200,
        },
    )

    assert estimate.difficulty is DifficultyClass.MEDIUM
    assert estimate.score == 4.602
    assert estimate.recommended_provider_families == (
        "gemini",
        "openai",
        "anthropic",
        "ollama",
    )


def test_trivial_keyword_clamp_never_exposes_negative_score() -> None:
    estimate = estimate_difficulty(
        {
            "tier": "S",
            "size": "S",
            "prompt": "rename typo comment docstring format lint whitespace spelling",
        },
    )

    assert estimate.difficulty is DifficultyClass.TRIVIAL
    assert estimate.score == 0.0


@pytest.mark.parametrize(
    ("provider_family", "expected_index", "expected_multiplier"),
    [
        ("  Anthropic  ", 0, 1.6),
        ("OPENAI", 2, 1.0),
        ("internal-tool", 4, 1.0),
    ],
)
def test_provider_preference_family_normalization_and_unknown_neutrality(
    provider_family: str,
    expected_index: int,
    expected_multiplier: float,
) -> None:
    assert provider_preference_index(DifficultyClass.HARD, provider_family) == expected_index
    assert provider_preference_multiplier(DifficultyClass.HARD, provider_family) == expected_multiplier


def test_feature_flag_is_default_disabled_and_env_backed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNISIGHT_MP_META_LLM_DIFFICULTY_ROUTING", raising=False)
    assert is_enabled() is False

    monkeypatch.setenv("OMNISIGHT_MP_META_LLM_DIFFICULTY_ROUTING", "1")
    assert is_enabled() is True

    monkeypatch.setenv("OMNISIGHT_MP_META_LLM_DIFFICULTY_ROUTING", "0")
    assert is_enabled() is False
