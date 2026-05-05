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
        "FEATURE_FLAG_TIER_DEFINITIONS",
        "FEATURE_FLAG_TIER_ORDER",
        "FEATURE_FLAG_TIER_VALUES",
        "FEATURE_FLAG_TIER_VALUE_SET",
        "FeatureFlagTier",
        "FeatureFlagTierDefinition",
        "is_feature_flag_tier",
    ]
