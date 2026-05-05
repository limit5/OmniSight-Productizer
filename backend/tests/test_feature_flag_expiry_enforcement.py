"""WP.7.5 -- feature flag expiry enforcement contract."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend import feature_flags as ff
from backend.feature_flags import (
    FeatureFlagRecord,
    FeatureFlagState,
    FeatureFlagTier,
)


FROZEN_NOW = datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc)


def _record(
    flag_name: str,
    *,
    expires_at: str | None,
    tier: FeatureFlagTier = FeatureFlagTier.PREVIEW,
    state: FeatureFlagState = FeatureFlagState.DISABLED,
    owner: str = "platform",
) -> FeatureFlagRecord:
    return FeatureFlagRecord(
        flag_name=flag_name,
        tier=tier,
        state=state,
        expires_at=expires_at,
        owner=owner,
    )


def test_future_and_unset_expiry_pass_ci_guard() -> None:
    records = [
        _record("wp.expiry.future", expires_at="2026-05-06T00:00:00Z"),
        _record("wp.expiry.unset", expires_at=None),
    ]

    assert ff.find_expired_feature_flags(records, now=FROZEN_NOW) == ()
    ff.assert_no_expired_feature_flags(records, now=FROZEN_NOW)


def test_expired_flag_returns_ci_violation() -> None:
    violations = ff.find_expired_feature_flags(
        [
            _record(
                "wp.expiry.stale",
                tier=FeatureFlagTier.DOGFOOD,
                state=FeatureFlagState.ENABLED,
                expires_at="2026-05-04T23:59:59Z",
                owner="bp",
            ),
        ],
        now=FROZEN_NOW,
    )

    assert violations == (
        ff.FeatureFlagExpiryViolation(
            flag_name="wp.expiry.stale",
            tier=FeatureFlagTier.DOGFOOD,
            state=FeatureFlagState.ENABLED,
            expires_at="2026-05-04T23:59:59Z",
            owner="bp",
        ),
    )


def test_ci_guard_fails_closed_for_expired_flags() -> None:
    with pytest.raises(AssertionError) as excinfo:
        ff.assert_no_expired_feature_flags(
            [
                _record(
                    "wp.expiry.deadline",
                    expires_at="2026-05-04 23:00:00+00:00",
                    owner="wp",
                ),
            ],
            now=FROZEN_NOW,
        )

    message = str(excinfo.value)
    assert "expired feature flags must be cleaned before CI passes" in message
    assert "wp.expiry.deadline" in message
    assert "owner=wp" in message


def test_naive_timestamps_are_treated_as_utc() -> None:
    violations = ff.find_expired_feature_flags(
        [_record("wp.expiry.naive", expires_at="2026-05-04T23:59:59")],
        now=FROZEN_NOW,
    )

    assert [v.flag_name for v in violations] == ["wp.expiry.naive"]


def test_registry_snapshot_can_feed_expiry_guard() -> None:
    registry = ff.FeatureFlagRegistry(lambda: [
        {
            "flag_name": "wp.expiry.snapshot",
            "tier": "runtime",
            "state": "enabled",
            "expires_at": "2026-05-04T00:00:00Z",
            "owner": "runtime",
        },
    ])

    violations = ff.find_expired_feature_flags(
        registry.snapshot().flags.values(),
        now=FROZEN_NOW,
    )

    assert [v.flag_name for v in violations] == ["wp.expiry.snapshot"]


def test_public_exports_include_expiry_contract() -> None:
    assert "FeatureFlagExpiryViolation" in ff.__all__
    assert "find_expired_feature_flags" in ff.__all__
    assert "assert_no_expired_feature_flags" in ff.__all__
