"""Tests for B1 #209: Cross-agent observation routing.

Verifies the full chain:
  agent A emits finding → DE proposal appears → agent B notified via SSE.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend import decision_engine as de
from backend.cross_agent_router import route_cross_agent_finding
from backend.events import bus, emit_debug_finding
from backend.finding_types import FindingType


class TestFindingTypeEnum:

    def test_cross_agent_observation_value(self):
        assert FindingType.cross_agent_observation.value == "cross_agent/observation"

    def test_all_legacy_values_present(self):
        assert FindingType.error_repeated.value == "error_repeated"
        assert FindingType.stuck_loop.value == "stuck_loop"
        assert FindingType.timeout.value == "timeout"
        assert FindingType.loop_breaker_trigger.value == "loop_breaker_trigger"


class TestCrossAgentRouter:

    def setup_method(self):
        de._reset_for_tests()

    def test_creates_de_proposal(self):
        de.set_mode("manual")
        decision = route_cross_agent_finding(
            finding_id="dbg-test001",
            task_id="task-42",
            reporter_agent_id="firmware-alpha",
            target_agent_id="software-beta",
            message="ISP register map changed, SDK headers need update",
            blocking=False,
        )
        assert decision is not None
        assert decision.kind == "cross_agent/observation"
        assert decision.status == de.DecisionStatus.pending
        assert decision.source["reporter_agent_id"] == "firmware-alpha"
        assert decision.source["target_agent_id"] == "software-beta"
        assert decision.source["blocking"] is False

    def test_blocking_flag_raises_severity(self):
        de.set_mode("manual")
        decision = route_cross_agent_finding(
            finding_id="dbg-test002",
            task_id="task-43",
            reporter_agent_id="validator-alpha",
            target_agent_id="firmware-alpha",
            message="Regression detected — firmware agent blocked",
            blocking=True,
        )
        assert decision is not None
        assert decision.severity == de.DecisionSeverity.risky
        assert decision.source["blocking"] is True

    def test_non_blocking_is_routine_severity(self):
        de.set_mode("manual")
        decision = route_cross_agent_finding(
            finding_id="dbg-test003",
            task_id="task-44",
            reporter_agent_id="reporter-alpha",
            target_agent_id="software-beta",
            message="FYI: docs updated",
            blocking=False,
        )
        assert decision is not None
        assert decision.severity == de.DecisionSeverity.routine

    def test_default_option_is_relay(self):
        de.set_mode("manual")
        decision = route_cross_agent_finding(
            finding_id="dbg-test004",
            task_id="task-45",
            reporter_agent_id="firmware-alpha",
            target_agent_id="software-beta",
            message="test",
        )
        assert decision is not None
        assert decision.default_option_id == "relay"
        option_ids = [o["id"] for o in decision.options]
        assert "relay" in option_ids
        assert "dismiss" in option_ids


class TestEndToEndChain:
    """Agent A emits finding → DE proposal appears → agent B notified."""

    def setup_method(self):
        de._reset_for_tests()

    @pytest.mark.asyncio
    async def test_emit_debug_finding_triggers_proposal(self):
        de.set_mode("manual")

        q = bus.subscribe()
        try:
            emit_debug_finding(
                task_id="task-100",
                agent_id="firmware-alpha",
                finding_type=FindingType.cross_agent_observation.value,
                severity="warn",
                message="ISP config mismatch detected",
                context={
                    "target_agent_id": "software-beta",
                    "blocking": True,
                },
            )

            events: list[dict] = []
            deadline = asyncio.get_event_loop().time() + 1.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = q.get_nowait()
                    events.append(msg)
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.01)

            event_types = [e["event"] for e in events]
            assert "debug_finding" in event_types
            assert "cross_agent_observation" in event_types

            cross_evt = next(e for e in events if e["event"] == "cross_agent_observation")
            data = json.loads(cross_evt["data"])
            assert data["target_agent_id"] == "software-beta"
            assert data["blocking"] is True

            pending = de.list_pending()
            cross_proposals = [d for d in pending if d.kind == "cross_agent/observation"]
            assert len(cross_proposals) == 1
            assert cross_proposals[0].source["reporter_agent_id"] == "firmware-alpha"
            assert cross_proposals[0].source["target_agent_id"] == "software-beta"
            assert cross_proposals[0].source["blocking"] is True
        finally:
            bus.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_non_cross_agent_finding_no_proposal(self):
        de.set_mode("manual")

        emit_debug_finding(
            task_id="task-200",
            agent_id="firmware-alpha",
            finding_type="stuck_loop",
            severity="warn",
            message="agent stuck",
        )

        pending = de.list_pending()
        cross_proposals = [d for d in pending if d.kind == "cross_agent/observation"]
        assert len(cross_proposals) == 0
