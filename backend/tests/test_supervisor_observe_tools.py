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
    supervisor_release_status,
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


# ── P5 watch-only: supervisor_release_status (read-only deploy state) ──
def test_release_status_registered_and_in_observe_set():
    assert supervisor_release_status in SUPERVISOR_OBSERVE_TOOLS
    assert supervisor_release_status.name in TOOL_MAP


def test_release_status_formats_dashboard(monkeypatch):
    import backend.release_dashboard as rd
    monkeypatch.setattr(rd, "current_prod_tag", lambda **k: "v0.7.33")
    monkeypatch.setattr(rd, "in_flight_deploys", lambda *a, **k: [])
    monkeypatch.setattr(rd, "canary_snapshot", lambda: {"status": "green", "rollout_id": "r1", "stage_index": 2})
    monkeypatch.setattr(rd, "release_history", lambda **k: [{"summary": "sora deployed v0.7.33 at 01:41"}])
    out = asyncio.run(supervisor_release_status.ainvoke({}))
    assert out.startswith("[SUPERVISOR]")
    assert "v0.7.33" in out and "in-flight deploy: none" in out
    assert "green" in out and "sora deployed v0.7.33" in out


def test_release_status_reports_inflight(monkeypatch):
    import backend.release_dashboard as rd
    monkeypatch.setattr(rd, "current_prod_tag", lambda **k: "v0.7.32")
    monkeypatch.setattr(rd, "in_flight_deploys", lambda *a, **k: [
        {"tag": "v0.7.33", "status": "deploying", "progress_percent": 60}])
    monkeypatch.setattr(rd, "canary_snapshot", lambda: None)
    monkeypatch.setattr(rd, "release_history", lambda **k: [])
    out = asyncio.run(supervisor_release_status.ainvoke({}))
    assert "⏳ in-flight deploy" in out and "v0.7.33" in out and "deploying" in out


def test_release_status_fails_open(monkeypatch):
    # a dashboard error must NOT raise — read-only tool returns a [FAILED] string
    import backend.release_dashboard as rd
    def _boom(**k):
        raise RuntimeError("store unavailable")
    monkeypatch.setattr(rd, "current_prod_tag", _boom)
    out = asyncio.run(supervisor_release_status.ainvoke({}))
    assert out.startswith("[FAILED]") and "unavailable" in out


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
