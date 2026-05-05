"""WP.7.7 -- existing env rollback knobs resolve through registry."""

from __future__ import annotations

import pytest

from backend import feature_flags as ff
from backend.feature_flags import (
    FeatureFlagRecord,
    FeatureFlagRegistry,
    FeatureFlagState,
    FeatureFlagTier,
)
from backend.agents import tools_patch
from backend.agents.skills_loader import SKILLS_LOADER_ENABLED_ENV
from backend.security import cmek_wizard, envelope

BLOCK_MODEL_ENABLED_ENV = "OMNISIGHT_WP_BLOCK_MODEL_ENABLED"


def _registry(flag_name: str, state: FeatureFlagState) -> FeatureFlagRegistry:
    return FeatureFlagRegistry(lambda: [
        FeatureFlagRecord(
            flag_name=flag_name,
            tier=FeatureFlagTier.RELEASE,
            state=state,
            owner="test",
        ),
    ])


def test_env_knob_manifest_covers_existing_ks_and_wp_knobs() -> None:
    assert set(ff.FEATURE_FLAG_ENV_KNOBS) == {
        envelope.ENVELOPE_ENABLED_ENV,
        cmek_wizard.CMEK_ENABLED_ENV,
        cmek_wizard.BYOG_ENABLED_ENV,
        tools_patch.DIFF_VALIDATION_ENABLED_ENV,
        SKILLS_LOADER_ENABLED_ENV,
        BLOCK_MODEL_ENABLED_ENV,
    }
    assert ff.FEATURE_FLAG_ENV_KNOBS[cmek_wizard.CMEK_ENABLED_ENV].flag_name == (
        "ks.cmek.enabled"
    )
    assert ff.FEATURE_FLAG_ENV_KNOBS[BLOCK_MODEL_ENABLED_ENV].flag_name == (
        "wp.block_model.enabled"
    )


@pytest.mark.parametrize(
    ("env_name", "flag_name"),
    [
        ("OMNISIGHT_BP_EXAMPLE_ENABLED", "bp.example.enabled"),
        ("OMNISIGHT_HD_PCB_SI_ENABLED", "hd.pcb.si.enabled"),
        ("OMNISIGHT_KS_CMEK_ENABLED", "ks.cmek.enabled"),
        ("OMNISIGHT_WP_DIFF_VALIDATION_ENABLED", "wp.diff_validation.enabled"),
        (BLOCK_MODEL_ENABLED_ENV, "wp.block_model.enabled"),
    ],
)
def test_feature_flag_name_for_env_knob_is_progressive(
    env_name: str,
    flag_name: str,
) -> None:
    assert ff.is_feature_flag_env_knob(env_name) is True
    assert ff.feature_flag_name_for_env_knob(env_name) == flag_name


def test_feature_flag_name_for_env_knob_rejects_unmanaged_prefix() -> None:
    assert ff.is_feature_flag_env_knob("OMNISIGHT_AS_ENABLED") is False
    with pytest.raises(ValueError):
        ff.feature_flag_name_for_env_knob("OMNISIGHT_AS_ENABLED")


def test_env_fallback_preserves_existing_false_values(monkeypatch) -> None:
    monkeypatch.setenv(cmek_wizard.CMEK_ENABLED_ENV, "false")

    assert (
        ff.resolve_env_backed_feature_flag(cmek_wizard.CMEK_ENABLED_ENV)
        is False
    )


def test_registry_global_state_overrides_env_fallback(monkeypatch) -> None:
    env_name = cmek_wizard.CMEK_ENABLED_ENV
    flag_name = ff.feature_flag_name_for_env_knob(env_name)
    monkeypatch.setenv(env_name, "false")

    assert (
        ff.resolve_env_backed_feature_flag(
            env_name,
            registry=_registry(flag_name, FeatureFlagState.ENABLED),
        )
        is True
    )


def test_module_knob_reads_default_registry_before_env(monkeypatch) -> None:
    flag_name = ff.feature_flag_name_for_env_knob(envelope.ENVELOPE_ENABLED_ENV)
    registry = _registry(flag_name, FeatureFlagState.ENABLED)
    monkeypatch.setattr(ff, "default_feature_flag_registry", registry)
    monkeypatch.setenv(envelope.ENVELOPE_ENABLED_ENV, "false")

    assert envelope.is_enabled() is True
