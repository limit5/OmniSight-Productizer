"""OP-825 B0 — F4/F10 regression: runner pickup refuses stale bridge.

Pins the rule that ``pickup_bridge_check`` raises ``BridgeStaleError``
when the gerrit→JIRA bridge cursor lags more than 1h behind develop,
and passes silently when the cursor is fresh. Without this gate the
runner can pick up an Approved ticket whose Published transition is
still queued and re-do already-merged work (incident class F4/F10).
"""
from __future__ import annotations

import pytest

from backend.agents.runner_health_checks import (
    PICKUP_BRIDGE_LAG_CEILING_SECONDS,
    BridgeStaleError,
    pickup_bridge_check,
)


@pytest.fixture
def stale_bridge_state() -> dict:
    return {"bridge_lag_seconds": 7200}


@pytest.fixture
def fresh_bridge_state() -> dict:
    return {"bridge_lag_seconds": 60}


def test_pickup_bridge_check_raises_when_lag_exceeds_ceiling(
    stale_bridge_state: dict,
) -> None:
    with pytest.raises(BridgeStaleError) as exc:
        pickup_bridge_check(stale_bridge_state["bridge_lag_seconds"])
    assert exc.value.lag_seconds == 7200


def test_pickup_bridge_check_passes_silently_when_lag_below_ceiling(
    fresh_bridge_state: dict,
) -> None:
    # Should return None and not raise.
    assert pickup_bridge_check(fresh_bridge_state["bridge_lag_seconds"]) is None


def test_pickup_bridge_check_edge_at_ceiling_boundary() -> None:
    # Lag exactly equal to the ceiling is still considered fresh —
    # only strictly greater triggers the failure mode.
    assert pickup_bridge_check(PICKUP_BRIDGE_LAG_CEILING_SECONDS) is None
    with pytest.raises(BridgeStaleError):
        pickup_bridge_check(PICKUP_BRIDGE_LAG_CEILING_SECONDS + 1)
