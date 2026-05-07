"""OP-71 / MP.W8.4 -- provider quota SSE event regression guards."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest


@pytest.mark.asyncio
async def test_emit_provider_quota_updated_publishes_canonical_payload() -> None:
    from backend import events

    q = events.bus.subscribe()
    try:
        events.emit_provider_quota_updated(
            "anthropic-subscription",
            rolling_5h_tokens=40,
            weekly_tokens=100,
            cap_5h_tokens=200,
            cap_weekly_tokens=1_000,
            circuit_state="closed",
            reason="usage_recorded",
            scopes=[],
            broadcast_scope="global",
        )
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["event"] == "provider.quota.updated"
        payload = json.loads(msg["data"])

        assert payload["provider"] == "anthropic-subscription"
        assert payload["rolling_5h_tokens"] == 40
        assert payload["weekly_tokens"] == 100
        assert payload["cap_5h_tokens"] == 200
        assert payload["cap_weekly_tokens"] == 1_000
        assert payload["remaining_5h_tokens"] == 160
        assert payload["remaining_weekly_tokens"] == 900
        assert payload["remaining_5h_quota_ratio"] == 0.8
        assert payload["remaining_weekly_quota_ratio"] == 0.9
        assert payload["circuit_state"] == "closed"
        assert payload["reason"] == "usage_recorded"
        assert payload["scopes"] == []
        assert "timestamp" in payload
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_emit_provider_quota_updated_serialises_reset_and_cap_timestamps() -> None:
    from backend import events

    reset_at = datetime(2026, 5, 7, 10, 30, tzinfo=timezone.utc)
    cap_hit_at = datetime(2026, 5, 7, 10, 45, tzinfo=timezone.utc)
    q = events.bus.subscribe()
    try:
        events.emit_provider_quota_updated(
            "openai-subscription",
            rolling_5h_tokens=55,
            weekly_tokens=700,
            cap_5h_tokens=100,
            cap_weekly_tokens=1_000,
            circuit_state="open",
            reason="cap_hit",
            scopes=["5h"],
            last_reset_at=reset_at,
            last_cap_hit_at=cap_hit_at,
            broadcast_scope="global",
        )
        msg = await asyncio.wait_for(q.get(), timeout=1)
        payload = json.loads(msg["data"])

        assert payload["circuit_state"] == "open"
        assert payload["reason"] == "cap_hit"
        assert payload["scopes"] == ["5h"]
        assert payload["last_reset_at"] == "2026-05-07T10:30:00+00:00"
        assert payload["last_cap_hit_at"] == "2026-05-07T10:45:00+00:00"
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_emit_provider_quota_updated_clamps_remaining_and_zero_caps() -> None:
    from backend import events

    q = events.bus.subscribe()
    try:
        events.emit_provider_quota_updated(
            "claude-over-cap",
            rolling_5h_tokens=250,
            weekly_tokens=2_500,
            cap_5h_tokens=0,
            cap_weekly_tokens=2_000,
            circuit_state="open",
            broadcast_scope="global",
        )
        msg = await asyncio.wait_for(q.get(), timeout=1)
        payload = json.loads(msg["data"])

        assert payload["remaining_5h_tokens"] == 0
        assert payload["remaining_weekly_tokens"] == 0
        assert payload["remaining_5h_quota_ratio"] == 0.0
        assert payload["remaining_weekly_quota_ratio"] == 0.0
    finally:
        events.bus.unsubscribe(q)


def test_provider_quota_updated_registered_in_sse_schema_exports() -> None:
    from backend.sse_schemas import SSEProviderQuotaUpdated, SSE_EVENT_SCHEMAS

    assert "provider.quota.updated" in SSE_EVENT_SCHEMAS
    assert SSE_EVENT_SCHEMAS["provider.quota.updated"] is SSEProviderQuotaUpdated
    assert set(SSEProviderQuotaUpdated.model_fields.keys()) >= {
        "provider",
        "rolling_5h_tokens",
        "weekly_tokens",
        "cap_5h_tokens",
        "cap_weekly_tokens",
        "remaining_5h_tokens",
        "remaining_weekly_tokens",
        "remaining_5h_quota_ratio",
        "remaining_weekly_quota_ratio",
        "circuit_state",
        "last_reset_at",
        "last_cap_hit_at",
        "reason",
        "scopes",
        "timestamp",
    }


def test_provider_quota_tracker_emits_provider_quota_updated_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.agents import provider_quota_tracker as tracker

    captured: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr("backend.events.emit_provider_quota_updated", _capture)
    state = tracker.QuotaState(
        provider="anthropic-subscription",
        rolling_5h_tokens=75,
        weekly_tokens=125,
        last_reset_at=None,
        last_cap_hit_at=None,
        circuit_state="closed",
    )

    tracker._emit_quota_update(state, "window_reset", ["weekly"])

    assert captured["args"] == (
        "anthropic-subscription",
        75,
        125,
        tracker.DEFAULT_5H_CAP_TOKENS,
        tracker.DEFAULT_WEEKLY_CAP_TOKENS,
        "closed",
    )
    assert captured["kwargs"] == {
        "last_reset_at": None,
        "last_cap_hit_at": None,
        "reason": "window_reset",
        "scopes": ["weekly"],
        "broadcast_scope": "global",
    }
