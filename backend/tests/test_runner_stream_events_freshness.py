"""OP-825 B0 — F25 regression: stream-events daemon liveness probe.

Pins the rule that ``check_stream_events`` returns ``StaleStreamEvents``
when the gerrit stream-events daemon hasn't emitted an event for more
than 600s, and the ``Healthy`` sentinel when an event is recent. Without
this probe pickup can run against a silently-dead daemon (incident F25).
"""
from __future__ import annotations

from backend.agents.runner_health_checks import (
    STREAM_EVENT_MAX_AGE_SECONDS,
    Healthy,
    StaleStreamEvents,
    check_stream_events,
)


def test_check_stream_events_returns_stale_when_event_older_than_window() -> None:
    now = 10_000.0
    last = now - 601  # 1s past the freshness ceiling
    result = check_stream_events(last, now=now)
    assert result == StaleStreamEvents(last_event_ts=last)


def test_check_stream_events_returns_healthy_when_event_recent() -> None:
    now = 10_000.0
    last = now - 30
    result = check_stream_events(last, now=now)
    assert result is Healthy


def test_check_stream_events_boundary_at_max_age_is_healthy() -> None:
    # At exactly the window the daemon is still considered healthy —
    # only strictly older fires the failure mode.
    now = 10_000.0
    last = now - STREAM_EVENT_MAX_AGE_SECONDS
    assert check_stream_events(last, now=now) is Healthy
    assert check_stream_events(last - 0.001, now=now) == StaleStreamEvents(
        last_event_ts=last - 0.001
    )
