"""WP.7.9 -- integrated feature flag registry regression coverage."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend import feature_flags as ff
from backend.feature_flags import (
    FeatureFlagResolutionSource,
    FeatureFlagState,
    FeatureFlagTier,
)


FROZEN_NOW = datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("tier", tuple(FeatureFlagTier))
def test_five_tiers_feed_resolution_priority(tier: FeatureFlagTier) -> None:
    flag_name = f"wp.tiered.{tier.value}"
    registry = ff.FeatureFlagRegistry(lambda: [
        {
            "flag_name": flag_name,
            "tier": tier.value,
            "state": "enabled",
            "expires_at": "2026-05-06T00:00:00Z",
            "owner": "wp",
        },
    ])

    global_state = registry.get_global_state(flag_name)
    assert global_state is FeatureFlagState.ENABLED

    resolved = ff.resolve_feature_flag_state(
        default="disabled",
        global_state=global_state,
    )
    assert resolved.state is FeatureFlagState.ENABLED
    assert resolved.source is FeatureFlagResolutionSource.GLOBAL_STATE

    resolved = ff.resolve_feature_flag_state(
        default="disabled",
        global_state=global_state,
        user_preference="disabled",
    )
    assert resolved.state is FeatureFlagState.DISABLED
    assert resolved.source is FeatureFlagResolutionSource.USER_PREFERENCE

    resolved = ff.resolve_feature_flag_state(
        default="disabled",
        global_state=global_state,
        user_preference="disabled",
        test_override="enabled",
    )
    assert resolved.state is FeatureFlagState.ENABLED
    assert resolved.source is FeatureFlagResolutionSource.TEST_OVERRIDE


def test_atomic_snapshot_push_reload_and_expiry_guard(monkeypatch) -> None:
    rows = [
        {
            "flag_name": "wp.runtime.push",
            "tier": "runtime",
            "state": "disabled",
            "expires_at": "2026-05-06T00:00:00Z",
            "owner": "runtime",
        },
    ]
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        return [dict(row) for row in rows]

    registry = ff.FeatureFlagRegistry(loader, clock=lambda: 123.0 + calls)
    monkeypatch.setattr(ff, "default_feature_flag_registry", registry)

    published = []

    def fake_publish(event, data):
        published.append((event, data))
        return True

    import backend.shared_state as shared_state

    monkeypatch.setattr(shared_state, "publish_cross_worker", fake_publish)

    first = registry.snapshot()
    assert first.flags["wp.runtime.push"].state is FeatureFlagState.DISABLED
    assert calls == 1

    rows[0]["state"] = "enabled"
    assert (
        ff.publish_feature_flags_invalidate(
            flag_name="wp.runtime.push",
            origin_worker="operator-ui",
        )
        is True
    )
    second = registry.snapshot()

    assert first is not second
    assert first.flags["wp.runtime.push"].state is FeatureFlagState.DISABLED
    assert second.flags["wp.runtime.push"].state is FeatureFlagState.ENABLED
    assert calls == 2
    assert published == [
        (
            ff.FEATURE_FLAGS_INVALIDATE_EVENT,
            {"flag_name": "wp.runtime.push", "origin_worker": "operator-ui"},
        ),
    ]
    ff.assert_no_expired_feature_flags(second.flags.values(), now=FROZEN_NOW)

    rows[0]["expires_at"] = "2026-05-04T23:59:59Z"
    ff._on_feature_flags_invalidate_event(
        ff.FEATURE_FLAGS_INVALIDATE_EVENT,
        {"origin_worker": "worker-a", "flag_name": "wp.runtime.push"},
    )
    third = registry.snapshot()

    assert second is not third
    assert third.flags["wp.runtime.push"].state is FeatureFlagState.ENABLED
    with pytest.raises(AssertionError) as excinfo:
        ff.assert_no_expired_feature_flags(third.flags.values(), now=FROZEN_NOW)
    assert "wp.runtime.push" in str(excinfo.value)
