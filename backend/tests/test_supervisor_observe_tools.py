"""Sora supervisor observe tools (P1, supervisor roadmap).

Read-only fleet-sight tools bound to the orchestrator chat. Covers:
registration in TOOL_MAP, the SORA_SUPERVISOR_TOOLS bundle shape, and the
fail-open contract (no DB pool → friendly string, never an exception).
See backend/agents/tools.py + docs/design/rpg/sora-supervisor-roadmap.md.
"""

import asyncio

import pytest

from backend.agents.tools import (
    SORA_SUPERVISOR_TOOLS,
    SUPERVISOR_OBSERVE_TOOLS,
    TOOL_MAP,
    supervisor_quota_status,
    supervisor_recent_incidents,
    supervisor_delivery_summary,
)


def test_observe_tools_registered_in_tool_map():
    for t in SUPERVISOR_OBSERVE_TOOLS:
        assert t.name in TOOL_MAP, f"{t.name} not in TOOL_MAP executor registry"


def test_bundle_includes_l3_recall_and_is_readonly_set():
    names = [t.name for t in SORA_SUPERVISOR_TOOLS]
    assert "search_past_solutions" in names  # L3 recall
    assert "supervisor_quota_status" in names
    # save_solution (WRITE) is NOT bound in P1 — recall only.
    assert "save_solution" not in names


@pytest.mark.parametrize(
    "tool",
    [supervisor_quota_status, supervisor_recent_incidents, supervisor_delivery_summary],
)
def test_fail_open_without_pool(tool):
    # No initialised DB pool → get_pool() raises → tool must catch and return
    # a friendly string, never propagate the exception. The unavailable path is
    # a FAILURE, so it now carries the [FAILED] status token (audit r2 rank 9:
    # the loop's telemetry/breaker classify by prefix, so a real failure must
    # NOT masquerade as a [SUPERVISOR] success).
    out = asyncio.run(tool.ainvoke({}))
    assert isinstance(out, str)
    assert out.startswith("[FAILED]") and "unavailable" in out
