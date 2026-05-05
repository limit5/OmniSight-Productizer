"""KS.1.6 -- spend anomaly detector contract tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.security import spend_anomaly as sa


def _event(**overrides) -> sa.TokenUsageEvent:
    base = dict(
        tenant_id="t-ks16",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        request_id="req-ks16",
        user_id="user-ks16",
        model="claude-sonnet-4-6",
    )
    base.update(overrides)
    return sa.TokenUsageEvent(**base)


@pytest.mark.asyncio
async def test_unconfigured_tenant_allows_without_recording() -> None:
    store = sa.InMemorySpendAnomalyStore()
    detector = sa.SpendAnomalyDetector(store=store, alert_sink=None)

    decision = await detector.record_and_check(_event(input_tokens=10_000))

    assert decision.allowed
    assert decision.observed_tokens == 0
    assert await detector.alerts_since() == []


@pytest.mark.asyncio
async def test_configure_threshold_validates_positive_values() -> None:
    detector = sa.SpendAnomalyDetector(
        store=sa.InMemorySpendAnomalyStore(),
        alert_sink=None,
    )

    with pytest.raises(ValueError, match="token_rate_limit"):
        await detector.configure_threshold("t-ks16", token_rate_limit=0)
    with pytest.raises(ValueError, match="window_seconds"):
        await detector.configure_threshold(
            "t-ks16", token_rate_limit=1, window_seconds=0,
        )
    with pytest.raises(ValueError, match="throttle_seconds"):
        await detector.configure_threshold(
            "t-ks16", token_rate_limit=1, throttle_seconds=0,
        )


@pytest.mark.asyncio
async def test_within_threshold_accumulates_window_and_allows() -> None:
    detector = sa.SpendAnomalyDetector(
        store=sa.InMemorySpendAnomalyStore(),
        alert_sink=None,
    )
    await detector.configure_threshold(
        "t-ks16",
        token_rate_limit=500,
        window_seconds=60,
        throttle_seconds=120,
    )
    now = datetime(2026, 5, 3, 1, 0, tzinfo=timezone.utc)

    first = await detector.record_and_check(
        _event(input_tokens=100, output_tokens=50),
        now=now,
    )
    second = await detector.record_and_check(
        _event(input_tokens=200, output_tokens=100),
        now=now,
    )

    assert first.allowed
    assert first.observed_tokens == 150
    assert second.allowed
    assert second.observed_tokens == 450
    assert await detector.alerts_since("t-ks16") == []


@pytest.mark.asyncio
async def test_crossing_threshold_auto_throttles_and_alerts() -> None:
    alerts: list[sa.SpendAnomalyAlert] = []

    async def sink(alert: sa.SpendAnomalyAlert) -> None:
        alerts.append(alert)

    detector = sa.SpendAnomalyDetector(
        store=sa.InMemorySpendAnomalyStore(),
        alert_sink=sink,
    )
    await detector.configure_threshold(
        "t-ks16",
        token_rate_limit=500,
        window_seconds=60,
        throttle_seconds=120,
    )
    now = datetime(2026, 5, 3, 1, 0, tzinfo=timezone.utc)

    decision = await detector.record_and_check(
        _event(input_tokens=400, output_tokens=150),
        now=now,
    )

    assert not decision.allowed
    assert decision.observed_tokens == 550
    assert decision.threshold_tokens == 500
    assert decision.retry_after_seconds == 120
    assert "Tenant token rate exceeded" in decision.reason
    assert decision.alert is not None
    assert decision.alert.action == "throttle"
    assert decision.alert.request_id == "req-ks16"
    assert alerts == [decision.alert]
    assert await detector.alerts_since("t-ks16") == [decision.alert]


@pytest.mark.asyncio
async def test_alert_fires_at_60_second_boundary_before_returning() -> None:
    alerts: list[sa.SpendAnomalyAlert] = []

    async def sink(alert: sa.SpendAnomalyAlert) -> None:
        alerts.append(alert)

    detector = sa.SpendAnomalyDetector(
        store=sa.InMemorySpendAnomalyStore(),
        alert_sink=sink,
    )
    await detector.configure_threshold(
        "t-ks16",
        token_rate_limit=500,
        window_seconds=60,
        throttle_seconds=120,
    )
    started_at = datetime(2026, 5, 3, 1, 0, tzinfo=timezone.utc)

    first = await detector.record_and_check(
        _event(input_tokens=250, output_tokens=100, request_id="req-ks16-a"),
        now=started_at,
    )
    second = await detector.record_and_check(
        _event(input_tokens=200, output_tokens=50, request_id="req-ks16-b"),
        now=datetime(2026, 5, 3, 1, 1, tzinfo=timezone.utc),
    )

    assert first.allowed
    assert not second.allowed
    assert second.alert is not None
    assert (second.alert.fired_at - started_at).total_seconds() == 60
    assert alerts == [second.alert]
    assert await detector.alerts_since("t-ks16") == [second.alert]


@pytest.mark.asyncio
async def test_active_throttle_blocks_without_recording_more_usage() -> None:
    async def sink(alert: sa.SpendAnomalyAlert) -> None:
        del alert

    detector = sa.SpendAnomalyDetector(
        store=sa.InMemorySpendAnomalyStore(),
        alert_sink=sink,
    )
    await detector.configure_threshold(
        "t-ks16",
        token_rate_limit=100,
        window_seconds=60,
        throttle_seconds=120,
    )
    now = datetime(2026, 5, 3, 1, 0, tzinfo=timezone.utc)

    first = await detector.record_and_check(
        _event(input_tokens=150, output_tokens=0),
        now=now,
    )
    second = await detector.record_and_check(
        _event(input_tokens=10_000, output_tokens=0),
        now=datetime(2026, 5, 3, 1, 1, tzinfo=timezone.utc),
    )

    assert not first.allowed
    assert not second.allowed
    assert second.observed_tokens == 0
    assert second.retry_after_seconds == pytest.approx(60.0)
    assert "temporarily throttled" in second.reason
    assert len(await detector.alerts_since("t-ks16")) == 1


@pytest.mark.asyncio
async def test_window_expiry_drops_old_usage() -> None:
    detector = sa.SpendAnomalyDetector(
        store=sa.InMemorySpendAnomalyStore(),
        alert_sink=None,
    )
    await detector.configure_threshold(
        "t-ks16",
        token_rate_limit=500,
        window_seconds=10,
        throttle_seconds=120,
    )

    first = await detector.record_and_check(
        _event(input_tokens=400, output_tokens=0),
        now=datetime(2026, 5, 3, 1, 0, tzinfo=timezone.utc),
    )
    second = await detector.record_and_check(
        _event(input_tokens=200, output_tokens=0),
        now=datetime(2026, 5, 3, 1, 0, 11, tzinfo=timezone.utc),
    )

    assert first.allowed
    assert second.allowed
    assert second.observed_tokens == 200


@pytest.mark.asyncio
async def test_disabled_threshold_allows() -> None:
    detector = sa.SpendAnomalyDetector(
        store=sa.InMemorySpendAnomalyStore(),
        alert_sink=None,
    )
    await detector.configure_threshold(
        "t-ks16",
        token_rate_limit=1,
        enabled=False,
    )

    decision = await detector.record_and_check(_event(input_tokens=10_000))

    assert decision.allowed
    assert await detector.alerts_since("t-ks16") == []


@pytest.mark.asyncio
async def test_default_notification_sink_uses_slack_and_email_tiers(monkeypatch) -> None:
    captured: dict = {}

    async def fake_send_notification(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("backend.notifications.send_notification", fake_send_notification)
    alert = sa.SpendAnomalyAlert(
        alert_id="spend_alert_test",
        tenant_id="t-ks16",
        threshold_tokens=500,
        observed_tokens=900,
        window_seconds=60,
        throttle_until=datetime(2026, 5, 3, 1, 5, tzinfo=timezone.utc),
        action="throttle",
        fired_at=datetime(2026, 5, 3, 1, 0, tzinfo=timezone.utc),
        request_id="req-ks16",
        user_id="user-ks16",
        model="claude-sonnet-4-6",
    )

    await sa.send_spend_anomaly_notification(alert)

    assert captured["tier"] == {"L2_IM_WEBHOOK", "L1_LOG_EMAIL"}
    assert captured["severity"] == "P2"
    assert captured["payload"]["level"] == "action"
    assert captured["payload"]["source"] == "ks.spend_anomaly"
    assert "Auto-throttle active" in captured["payload"]["message"]


def test_token_usage_total_counts_all_token_buckets() -> None:
    event = _event(
        input_tokens=1,
        output_tokens=2,
        cache_read_tokens=3,
        cache_creation_tokens=4,
    )

    assert event.total_tokens == 10


def test_no_module_global_detector_singleton() -> None:
    assert not hasattr(sa, "_detector")
    assert not hasattr(sa, "detector")
