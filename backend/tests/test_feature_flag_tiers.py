"""WP.7.2 -- feature flag tier definition contract."""

from __future__ import annotations

import pytest

from backend import feature_flags as ff
from backend.feature_flags import FeatureFlagTier


def test_tier_enum_has_exact_five_members() -> None:
    assert [tier.name for tier in FeatureFlagTier] == [
        "DEBUG",
        "DOGFOOD",
        "PREVIEW",
        "RELEASE",
        "RUNTIME",
    ]
    assert [tier.value for tier in FeatureFlagTier] == [
        "debug",
        "dogfood",
        "preview",
        "release",
        "runtime",
    ]


def test_tier_order_and_values_match_enum() -> None:
    assert ff.FEATURE_FLAG_TIER_ORDER == tuple(FeatureFlagTier)
    assert ff.FEATURE_FLAG_TIER_VALUES == tuple(t.value for t in FeatureFlagTier)
    assert ff.FEATURE_FLAG_TIER_VALUE_SET == frozenset(
        ff.FEATURE_FLAG_TIER_VALUES
    )


def test_tier_definitions_cover_every_tier() -> None:
    assert set(ff.FEATURE_FLAG_TIER_DEFINITIONS) == set(FeatureFlagTier)
    for tier in FeatureFlagTier:
        definition = ff.FEATURE_FLAG_TIER_DEFINITIONS[tier]
        assert definition.label == tier.name
        assert definition.audience.strip()
        assert definition.purpose.strip()


def test_tier_definitions_are_immutable() -> None:
    with pytest.raises(TypeError):
        ff.FEATURE_FLAG_TIER_DEFINITIONS[  # type: ignore[index]
            FeatureFlagTier.DEBUG
        ] = (
            ff.FeatureFlagTierDefinition("DEBUG", "x", "x")
        )


def test_parse_accepts_lowercase_labels_and_enum_members() -> None:
    assert FeatureFlagTier.parse(FeatureFlagTier.PREVIEW) is FeatureFlagTier.PREVIEW
    assert FeatureFlagTier.parse("preview") is FeatureFlagTier.PREVIEW
    assert FeatureFlagTier.parse(" RUNTIME ") is FeatureFlagTier.RUNTIME


def test_parse_rejects_unknown_tiers() -> None:
    with pytest.raises(ValueError):
        FeatureFlagTier.parse("beta")


def test_is_feature_flag_tier_is_permissive_probe() -> None:
    assert ff.is_feature_flag_tier(FeatureFlagTier.DEBUG) is True
    assert ff.is_feature_flag_tier("dogfood") is True
    assert ff.is_feature_flag_tier(" DOGFOOD ") is True
    assert ff.is_feature_flag_tier("beta") is False


def test_public_exports_are_pinned() -> None:
    assert ff.__all__ == [
        "FEATURE_FLAGS_INVALIDATE_EVENT",
        "FEATURE_FLAG_ENV_FALSE_VALUES",
        "FEATURE_FLAG_ENV_KNOBS",
        "FEATURE_FLAG_ENV_PREFIXES",
        "FEATURE_FLAG_ENV_TRUE_VALUES",
        "FEATURE_FLAG_RESOLUTION_SOURCE_ORDER",
        "FEATURE_FLAG_STATE_VALUES",
        "FEATURE_FLAG_STATE_VALUE_SET",
        "FEATURE_FLAG_TIER_DEFINITIONS",
        "FEATURE_FLAG_TIER_ORDER",
        "FEATURE_FLAG_TIER_VALUES",
        "FEATURE_FLAG_TIER_VALUE_SET",
        "FeatureFlagResolution",
        "FeatureFlagResolutionSource",
        "FeatureFlagExpiryViolation",
        "FeatureFlagEnvKnob",
        "FeatureFlagRegistry",
        "FeatureFlagRegistrySnapshot",
        "FeatureFlagRecord",
        "FeatureFlagState",
        "FeatureFlagTier",
        "FeatureFlagTierDefinition",
        "assert_no_expired_feature_flags",
        "default_feature_flag_registry",
        "feature_flag_name_for_env_knob",
        "find_expired_feature_flags",
        "get_feature_flag_global_state",
        "invalidate_feature_flags_cache",
        "is_feature_flag_env_knob",
        "is_feature_flag_state",
        "is_feature_flag_tier",
        "publish_feature_flags_invalidate",
        "resolve_env_backed_feature_flag",
        "resolve_feature_flag_state",
    ]
