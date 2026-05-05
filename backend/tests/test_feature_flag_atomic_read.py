"""WP.7.4 -- feature flag atomic hot-path read contract."""

from __future__ import annotations

import pytest

from backend import feature_flags as ff
from backend.feature_flags import (
    FeatureFlagRecord,
    FeatureFlagRegistry,
    FeatureFlagState,
    FeatureFlagTier,
)


def test_registry_loads_once_for_hot_path_reads() -> None:
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        return [
            {
                "flag_name": "wp.atomic",
                "tier": "runtime",
                "state": "enabled",
                "owner": "platform",
            },
        ]

    registry = FeatureFlagRegistry(loader, clock=lambda: 123.0)

    assert registry.get_global_state("wp.atomic") is FeatureFlagState.ENABLED
    assert registry.get_global_state("wp.atomic") is FeatureFlagState.ENABLED
    assert calls == 1
    assert registry.snapshot().loaded_at == 123.0


def test_registry_snapshot_is_immutable() -> None:
    registry = FeatureFlagRegistry(lambda: [
        FeatureFlagRecord(
            flag_name="wp.snapshot",
            tier=FeatureFlagTier.RELEASE,
            state=FeatureFlagState.DISABLED,
        ),
    ])

    snapshot = registry.snapshot()

    with pytest.raises(TypeError):
        snapshot.flags["wp.snapshot"] = FeatureFlagRecord(  # type: ignore[index]
            flag_name="wp.snapshot",
            tier=FeatureFlagTier.RELEASE,
            state=FeatureFlagState.ENABLED,
        )


def test_reload_replaces_snapshot_atomically() -> None:
    states = ["disabled", "enabled"]

    def loader():
        return [
            {
                "flag_name": "wp.reload",
                "tier": "runtime",
                "state": states[0],
            },
        ]

    registry = FeatureFlagRegistry(loader)
    first = registry.snapshot()
    states[0] = "enabled"
    second = registry.reload()

    assert first is not second
    assert first.flags["wp.reload"].state is FeatureFlagState.DISABLED
    assert second.flags["wp.reload"].state is FeatureFlagState.ENABLED
    assert registry.get_global_state("wp.reload") is FeatureFlagState.ENABLED


def test_invalidate_defers_reload_until_next_read() -> None:
    calls = 0
    state = "disabled"

    def loader():
        nonlocal calls
        calls += 1
        return [
            {
                "flag_name": "wp.invalidate",
                "tier": "runtime",
                "state": state,
            },
        ]

    registry = FeatureFlagRegistry(loader)

    assert registry.get_global_state("wp.invalidate") is FeatureFlagState.DISABLED
    state = "enabled"
    registry.invalidate()

    assert calls == 1
    assert registry.get_global_state("wp.invalidate") is FeatureFlagState.ENABLED
    assert calls == 2


def test_missing_flag_uses_default_without_mutating_snapshot() -> None:
    registry = FeatureFlagRegistry(lambda: [])

    assert (
        registry.get_global_state("wp.missing", default="enabled")
        is FeatureFlagState.ENABLED
    )
    assert registry.get_global_state("wp.missing") is None
    assert registry.snapshot().flags == {}


def test_module_helper_reads_supplied_registry() -> None:
    registry = FeatureFlagRegistry(lambda: [
        {
            "flag_name": "wp.helper",
            "tier": "dogfood",
            "state": "enabled",
        },
    ])

    assert (
        ff.get_feature_flag_global_state("wp.helper", registry=registry)
        is FeatureFlagState.ENABLED
    )


def test_callback_is_registered_at_import() -> None:
    from backend import shared_state

    assert ff._on_feature_flags_invalidate_event in shared_state._pubsub_callbacks


def test_event_constant_is_stable() -> None:
    assert ff.FEATURE_FLAGS_INVALIDATE_EVENT == "feature_flags_invalidate"


def test_cross_worker_callback_invalidates_default_registry(monkeypatch) -> None:
    registry = FeatureFlagRegistry(lambda: [
        {
            "flag_name": "wp.pubsub",
            "tier": "runtime",
            "state": "enabled",
        },
    ])
    monkeypatch.setattr(ff, "default_feature_flag_registry", registry)

    assert registry.snapshot() is registry.snapshot()
    before = registry.snapshot()
    ff._on_feature_flags_invalidate_event(
        ff.FEATURE_FLAGS_INVALIDATE_EVENT,
        {"origin_worker": "worker-a", "flag_name": "wp.pubsub"},
    )

    assert registry.snapshot() is not before


def test_cross_worker_callback_ignores_unrelated_events(monkeypatch) -> None:
    registry = FeatureFlagRegistry(lambda: [])
    monkeypatch.setattr(ff, "default_feature_flag_registry", registry)
    before = registry.snapshot()

    ff._on_feature_flags_invalidate_event("pricing_reload", {})

    assert registry.snapshot() is before


def test_publish_invalidates_local_cache_and_broadcasts(monkeypatch) -> None:
    published = []
    registry = FeatureFlagRegistry(lambda: [])
    monkeypatch.setattr(ff, "default_feature_flag_registry", registry)
    before = registry.snapshot()

    def fake_publish(event, data):
        published.append((event, data))
        return True

    import backend.shared_state as shared_state

    monkeypatch.setattr(shared_state, "publish_cross_worker", fake_publish)

    assert (
        ff.publish_feature_flags_invalidate(
            flag_name="wp.publish",
            origin_worker="worker-a",
        )
        is True
    )
    assert registry.snapshot() is not before
    assert published == [
        (
            ff.FEATURE_FLAGS_INVALIDATE_EVENT,
            {"flag_name": "wp.publish", "origin_worker": "worker-a"},
        ),
    ]
