"""OP-1293 branch coverage for the incident recorder seam."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agents import incident_recorder
from backend.agents.failure_class import FailureClass
from backend.agents.memory_tool_handler import MemoryAuditWriteFailed


@pytest.fixture(autouse=True)
def _reset_buffers():
    incident_recorder.reset_for_tests()
    yield
    incident_recorder.reset_for_tests()


def _audit_request() -> SimpleNamespace:
    return SimpleNamespace(query_fleet="fleet-prod", target_fleet="fleet-dev")


def _audit_decision(
    *,
    summary: str,
    permitted: bool,
    escalate: bool = False,
    tier: str = "S",
) -> SimpleNamespace:
    return SimpleNamespace(
        audit_summary=summary,
        permitted=permitted,
        escalate=escalate,
        tier=SimpleNamespace(value=tier),
    )


def test_record_memory_recall_audit_snapshots_and_filters_events():
    incident_recorder.record_memory_recall_audit(
        _audit_request(),
        _audit_decision(summary="permitted recall", permitted=True),
    )
    incident_recorder.record_memory_recall_audit(
        _audit_request(),
        _audit_decision(summary="denied recall", permitted=False, escalate=True, tier="X"),
    )

    rows = incident_recorder.get_recorded_events()
    filtered = incident_recorder.get_recorded_events(FailureClass.MEMORY_RECALL_AUDIT)

    assert [row.summary for row in rows] == ["permitted recall", "denied recall"]
    assert filtered == rows
    assert rows[1].permitted is False
    assert rows[1].escalate is True
    assert rows[1].tier == "X"


def test_record_memory_recall_audit_translates_buffer_write_failure(monkeypatch):
    class BrokenLock:
        def __enter__(self):
            raise RuntimeError("lock unavailable")

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(incident_recorder, "_buffer_lock", BrokenLock())

    with pytest.raises(MemoryAuditWriteFailed, match="lock unavailable"):
        incident_recorder.record_memory_recall_audit(
            _audit_request(),
            _audit_decision(summary="audit", permitted=True),
        )


def test_record_runner_incident_coerces_string_failure_class_non_strict():
    record = incident_recorder.record_runner_incident(
        ticket_key="OP-1293",
        failure_class="not_registered",
        summary="unknown failure class",
        incident_id="incident-op-1293",
    )

    assert record.incident_id == "incident-op-1293"
    assert record.failure_class is FailureClass.OTHER
    assert incident_recorder.get_runner_incidents(failure_class=FailureClass.OTHER) == [
        record
    ]


def test_get_runner_incidents_filters_by_ticket_key():
    first = incident_recorder.record_runner_incident(
        ticket_key="OP-1293",
        failure_class=FailureClass.TEST_FAILURE,
        summary="target incident",
        area="backend",
    )
    incident_recorder.record_runner_incident(
        ticket_key="OP-0001",
        failure_class=FailureClass.TEST_FAILURE,
        summary="other ticket",
        area="backend",
    )

    assert incident_recorder.get_runner_incidents(ticket_key="OP-1293") == [first]
