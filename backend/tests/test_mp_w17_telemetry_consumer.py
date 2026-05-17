"""Branch tests for ``backend.agents.mp_w17_telemetry_consumer``."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.agents.mp_w17_telemetry_consumer import (
    consume_batch,
    consume_event,
    consume_invocation_log_line,
    payload_from_invocation_log,
)
from backend.agents.tool_proficiency import InMemoryToolProficiencyStore


AGENT = "agent-alpha"
TOOL = "Read"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_payload_from_invocation_log_rejects_non_payload_lines() -> None:
    assert payload_from_invocation_log(123) is None  # type: ignore[arg-type]
    assert payload_from_invocation_log("INFO runner booted") is None
    assert payload_from_invocation_log("events.tool_invocation not-json") is None
    assert payload_from_invocation_log("events.tool_invocation [1, 2]") is None
    assert payload_from_invocation_log("events.tool_invocation {bad-json") is None


@pytest.mark.asyncio
async def test_consume_invocation_log_line_ignores_non_payload_line() -> None:
    store = InMemoryToolProficiencyStore()

    recorded = await consume_invocation_log_line(store, "INFO runner booted")

    assert recorded is None
    assert await store.get_state(AGENT, TOOL) is None


@pytest.mark.asyncio
async def test_consume_event_drops_missing_tool_before_writing_state() -> None:
    store = InMemoryToolProficiencyStore()

    recorded = await consume_event(
        store,
        {"tool_name": "   ", "agent_id": AGENT, "success": True},
    )

    assert recorded is None
    assert await store.list_states(AGENT) == ()


@pytest.mark.asyncio
async def test_consume_event_drops_missing_agent_before_writing_state() -> None:
    store = InMemoryToolProficiencyStore()

    recorded = await consume_event(
        store,
        {"tool_name": TOOL, "agent_id": "   ", "success": True},
    )

    assert recorded is None
    assert await store.get_state(AGENT, TOOL) is None


@pytest.mark.asyncio
async def test_consume_event_uses_nested_invocation_timestamp_and_tool() -> None:
    store = InMemoryToolProficiencyStore()

    recorded = await consume_event(
        store,
        {
            "invocation": {
                "tool_id": TOOL,
                "agent_id": AGENT,
                "timestamp": "2026-01-01T00:00:00",
            },
            "outcome": {"is_error": False},
        },
    )

    assert recorded is not None
    assert recorded.tool_id == TOOL
    assert recorded.success_count == 1
    state = await store.get_state(AGENT, TOOL)
    assert state is not None
    assert state.last_used_at == T0


@pytest.mark.asyncio
async def test_consume_event_malformed_timestamp_falls_back_to_now() -> None:
    store = InMemoryToolProficiencyStore()
    before = datetime.now(timezone.utc)

    recorded = await consume_event(
        store,
        {
            "tool_name": TOOL,
            "agent_id": AGENT,
            "success": True,
            "timestamp": "not-a-timestamp",
        },
    )

    after = datetime.now(timezone.utc)
    assert recorded is not None
    state = await store.get_state(AGENT, TOOL)
    assert state is not None
    assert before <= state.last_used_at <= after


@pytest.mark.asyncio
async def test_consume_batch_counts_invocation_alias_drops() -> None:
    store = InMemoryToolProficiencyStore()

    stats = await consume_batch(
        store,
        (
            {"invocation": {"tool_id": TOOL}, "success": True},
            {"invocation": {"agent_id": AGENT}, "success": True},
            {
                "invocation": {"tool_id": TOOL, "agent_id": AGENT},
                "outcome": "done",
            },
        ),
    )

    assert stats.received == 3
    assert stats.applied == 1
    assert stats.dropped_missing_agent == 1
    assert stats.dropped_missing_tool == 1
    assert stats.level_ups == 0
    state = await store.get_state(AGENT, TOOL)
    assert state is not None
    assert state.success_count == 1
