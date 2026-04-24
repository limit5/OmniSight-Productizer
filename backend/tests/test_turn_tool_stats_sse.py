"""ZZ.A3 #303-3 — per-turn ``turn_tool_stats`` SSE event regression guards.

Locks the contract between :func:`backend.agents.nodes.summarizer_node`,
:func:`backend.events.emit_turn_tool_stats`, and the
``SSETurnToolStats`` schema:

1. **Emit helper payload shape.** Canonical 5-field payload
   (agent_type / task_id / tool_call_count / tool_failure_count /
   failed_tools) + bus-auto ``timestamp``.
2. **Failure-count semantics.** ``tool_failure_count`` = number of
   ``ToolResult`` entries with ``success == False`` — the LangGraph
   shape's equivalent of the spec's ``result.error is not None``.
3. **Summarizer end-to-end wire-up.** Calling ``summarizer_node`` with
   a populated ``GraphState.tool_results`` pushes the aggregate on the
   bus with the correct counts + ``failed_tools`` list.
4. **Zeroed snapshot on pass-through.** The "no tools called" branch
   still emits a 0/0 event so the UI can clear any stale
   ``failed N`` badge carried over from a prior turn.
5. **Duplicate-preserving failed_tools.** A retry loop that re-runs the
   same failing tool must surface each attempt in ``failed_tools`` so
   the badge count matches the actual retry attempts rather than a
   de-duped tool-name set.
6. **Schema registry drift guard.** ``turn_tool_stats`` is listed in
   ``SSE_EVENT_SCHEMAS`` with the 5 contract fields so the
   ``/runtime/sse-schema`` export advertises it to the frontend.
"""

from __future__ import annotations

import asyncio
import json

import pytest


@pytest.mark.asyncio
async def test_emit_turn_tool_stats_publishes_canonical_payload():
    from backend import events

    q = events.bus.subscribe()
    try:
        events.emit_turn_tool_stats(
            "firmware",
            5,
            2,
            failed_tools=["run_bash", "apt_install"],
            task_id="task-42",
            broadcast_scope="global",
        )
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["event"] == "turn_tool_stats"
        payload = json.loads(msg["data"])

        assert payload["agent_type"] == "firmware"
        assert payload["task_id"] == "task-42"
        assert payload["tool_call_count"] == 5
        assert payload["tool_failure_count"] == 2
        assert payload["failed_tools"] == ["run_bash", "apt_install"]
        assert "timestamp" in payload
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_summarizer_emits_turn_tool_stats_end_to_end():
    """Full path: seed ``GraphState.tool_results`` with a mixed success /
    failure batch → ``summarizer_node`` → bus. Lock the aggregation
    formula (``sum(not r.success)`` → failure count, in-order
    ``failed_tools`` list).
    """
    from backend import events
    from backend.agents.nodes import summarizer_node
    from backend.agents.state import GraphState, ToolResult

    state = GraphState(
        routed_to="software",
        task_id="task-sw-99",
        tool_results=[
            ToolResult(tool_name="read_file", output="ok", success=True),
            ToolResult(tool_name="run_bash", output="[ERROR] exit 1", success=False),
            ToolResult(tool_name="grep_code", output="3 matches", success=True),
            ToolResult(tool_name="apt_install", output="[ERROR] locked", success=False),
        ],
        # Pre-seed an answer so the summarizer's rule-based branch is a
        # cheap deterministic path — we're testing the emit, not the
        # answer-synthesis logic.
        answer="done",
    )

    q = events.bus.subscribe()
    try:
        summarizer_node(state)
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["event"] == "turn_tool_stats"
        payload = json.loads(msg["data"])

        assert payload["agent_type"] == "software"
        assert payload["task_id"] == "task-sw-99"
        assert payload["tool_call_count"] == 4
        assert payload["tool_failure_count"] == 2
        assert payload["failed_tools"] == ["run_bash", "apt_install"]
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_summarizer_emits_zero_snapshot_on_passthrough():
    """No tools this turn → emit 0/0 anyway. The UI needs a zeroed
    snapshot to clear a "failed 1" badge carried over from a prior turn
    where tools DID run and did fail. Without this, a conversational
    turn after a failing task turn would keep the red badge visible.
    """
    from backend import events
    from backend.agents.nodes import summarizer_node
    from backend.agents.state import GraphState

    state = GraphState(
        routed_to="general",
        task_id="task-conv-1",
        tool_results=[],
        answer="[GENERAL AGENT] hi",
    )

    q = events.bus.subscribe()
    try:
        summarizer_node(state)
        msg = await asyncio.wait_for(q.get(), timeout=1)
        payload = json.loads(msg["data"])
        assert payload["tool_call_count"] == 0
        assert payload["tool_failure_count"] == 0
        assert payload["failed_tools"] == []
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_summarizer_preserves_duplicate_failed_tools():
    """A retry loop re-running the same failing tool must surface each
    attempt — not a de-duped set. The red "failed N" badge must match
    the actual retry attempt count.
    """
    from backend import events
    from backend.agents.nodes import summarizer_node
    from backend.agents.state import GraphState, ToolResult

    state = GraphState(
        routed_to="firmware",
        tool_results=[
            ToolResult(tool_name="run_bash", output="[ERROR] exit 1", success=False),
            ToolResult(tool_name="run_bash", output="[ERROR] exit 1", success=False),
            ToolResult(tool_name="run_bash", output="[ERROR] exit 1", success=False),
        ],
        answer="done",
    )

    q = events.bus.subscribe()
    try:
        summarizer_node(state)
        msg = await asyncio.wait_for(q.get(), timeout=1)
        payload = json.loads(msg["data"])
        assert payload["tool_call_count"] == 3
        assert payload["tool_failure_count"] == 3
        # Duplicates preserved — not a set().
        assert payload["failed_tools"] == ["run_bash", "run_bash", "run_bash"]
    finally:
        events.bus.unsubscribe(q)


def test_turn_tool_stats_registered_in_sse_schema_exports():
    """Drift guard: ``turn_tool_stats`` must appear in the SSE schema
    registry so the frontend codegen sees it. Catches the common "added
    the emit helper but forgot the registry" footgun.
    """
    from backend.sse_schemas import SSE_EVENT_SCHEMAS, SSETurnToolStats

    assert "turn_tool_stats" in SSE_EVENT_SCHEMAS
    assert SSE_EVENT_SCHEMAS["turn_tool_stats"] is SSETurnToolStats

    fields = set(SSETurnToolStats.model_fields.keys())
    assert fields >= {
        "agent_type", "task_id", "tool_call_count",
        "tool_failure_count", "failed_tools",
    }
