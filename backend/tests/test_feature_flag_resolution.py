"""WP.7.3 -- feature flag resolution priority contract."""

from __future__ import annotations

import pytest

from backend import feature_flags as ff
from backend.feature_flags import (
    FeatureFlagResolutionSource,
    FeatureFlagState,
)


def test_state_enum_has_exact_members() -> None:
    assert [state.name for state in FeatureFlagState] == [
        "DISABLED",
        "ENABLED",
    ]
    assert [state.value for state in FeatureFlagState] == [
        "disabled",
        "enabled",
    ]


def test_state_values_match_enum() -> None:
    assert ff.FEATURE_FLAG_STATE_VALUES == tuple(
        s.value for s in FeatureFlagState
    )
    assert ff.FEATURE_FLAG_STATE_VALUE_SET == frozenset(
        ff.FEATURE_FLAG_STATE_VALUES
    )


def test_resolution_source_order_is_pinned() -> None:
    assert ff.FEATURE_FLAG_RESOLUTION_SOURCE_ORDER == (
        FeatureFlagResolutionSource.TEST_OVERRIDE,
        FeatureFlagResolutionSource.USER_PREFERENCE,
        FeatureFlagResolutionSource.GLOBAL_STATE,
        FeatureFlagResolutionSource.DEFAULT,
    )


def test_state_parse_accepts_lowercase_labels_and_enum_members() -> None:
    assert (
        FeatureFlagState.parse(FeatureFlagState.ENABLED)
        is FeatureFlagState.ENABLED
    )
    assert FeatureFlagState.parse("enabled") is FeatureFlagState.ENABLED
    assert FeatureFlagState.parse(" DISABLED ") is FeatureFlagState.DISABLED


def test_state_parse_rejects_unknown_values() -> None:
    with pytest.raises(ValueError):
        FeatureFlagState.parse("on")


def test_is_feature_flag_state_is_permissive_probe() -> None:
    assert ff.is_feature_flag_state(FeatureFlagState.ENABLED) is True
    assert ff.is_feature_flag_state("disabled") is True
    assert ff.is_feature_flag_state(" ENABLED ") is True
    assert ff.is_feature_flag_state("on") is False


def test_default_wins_when_no_other_source_is_present() -> None:
    resolved = ff.resolve_feature_flag_state(default="enabled")

    assert resolved.state is FeatureFlagState.ENABLED
    assert resolved.source is FeatureFlagResolutionSource.DEFAULT


def test_global_state_beats_default() -> None:
    resolved = ff.resolve_feature_flag_state(
        default="disabled",
        global_state="enabled",
    )

    assert resolved.state is FeatureFlagState.ENABLED
    assert resolved.source is FeatureFlagResolutionSource.GLOBAL_STATE


def test_user_preference_beats_global_state() -> None:
    resolved = ff.resolve_feature_flag_state(
        default="disabled",
        global_state="disabled",
        user_preference="enabled",
    )

    assert resolved.state is FeatureFlagState.ENABLED
    assert resolved.source is FeatureFlagResolutionSource.USER_PREFERENCE


def test_test_override_beats_user_preference() -> None:
    resolved = ff.resolve_feature_flag_state(
        default="enabled",
        global_state="enabled",
        user_preference="enabled",
        test_override="disabled",
    )

    assert resolved.state is FeatureFlagState.DISABLED
    assert resolved.source is FeatureFlagResolutionSource.TEST_OVERRIDE


def test_invalid_winning_source_raises() -> None:
    with pytest.raises(ValueError):
        ff.resolve_feature_flag_state(
            default="enabled",
            global_state="on",
        )


def test_lower_priority_invalid_source_is_not_parsed() -> None:
    resolved = ff.resolve_feature_flag_state(
        default="bogus",
        global_state="enabled",
    )

    assert resolved.state is FeatureFlagState.ENABLED
    assert resolved.source is FeatureFlagResolutionSource.GLOBAL_STATE


def test_public_exports_include_resolution_contract() -> None:
    assert "FeatureFlagState" in ff.__all__
    assert "FeatureFlagResolution" in ff.__all__
    assert "FeatureFlagResolutionSource" in ff.__all__
    assert "resolve_feature_flag_state" in ff.__all__
