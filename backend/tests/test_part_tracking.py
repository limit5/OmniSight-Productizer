"""Tests for the C8 part-tracking verdict queue."""

from __future__ import annotations

from datetime import datetime, timezone

from backend.inspection.part_tracking import (
    PartDisposition,
    PartTrackingVerdictQueue,
)
from backend.inspection.verdict import Defect, InspectionVerdict


NOW = datetime(2026, 6, 12, 8, 15, 30, tzinfo=timezone.utc)


def _verdict(part_id: str, value: str = "pass") -> InspectionVerdict:
    defects = []
    if value == "fail":
        defects = [
            Defect.model_validate(
                {"class": "scratch", "loc": [4.0, 8.0], "score": 0.9}
            )
        ]

    return InspectionVerdict(
        part_id=part_id,
        station_id="station-aoi-1",
        verdict=value,
        defects=defects,
        model_version="rknn-scratch-detector@2026.06.12",
        recipe_version="recipe-aluminum-v3",
        ts_capture=NOW,
        ts_verdict=NOW,
    )


def test_dequeue_returns_verdict_for_part_now_at_gate() -> None:
    queue = PartTrackingVerdictQueue(gate_offset_counts=50, max_depth=4)

    capture = queue.enqueue("part-00042", position=100)
    attached = queue.attach_verdict(_verdict("part-00042", "pass"))
    decision = queue.dequeue_at_gate(position=150)

    assert capture.accepted is True
    assert attached.attached is True
    assert decision.part_id == "part-00042"
    assert decision.gate_position == 150
    assert decision.verdict == _verdict("part-00042", "pass")
    assert decision.disposition is PartDisposition.allow
    assert decision.pass_signal_allowed is True
    assert decision.fail_closed is False


def test_missed_trigger_gap_fails_closed_without_consuming_tracked_part() -> None:
    queue = PartTrackingVerdictQueue(gate_offset_counts=50, max_depth=4)
    queue.enqueue("part-00043", position=100)
    queue.attach_verdict(_verdict("part-00043", "pass"))

    gap = queue.dequeue_at_gate(position=125)
    decision = queue.dequeue_at_gate(position=150)

    assert gap.part_id is None
    assert gap.missed_trigger is True
    assert gap.reason == "missed_trigger_gap"
    assert gap.disposition is PartDisposition.reject
    assert gap.pass_signal_allowed is False
    assert gap.fail_closed is True
    assert decision.part_id == "part-00043"
    assert decision.disposition is PartDisposition.allow


def test_late_verdict_after_gate_deadline_fails_closed_and_does_not_reopen_part() -> None:
    queue = PartTrackingVerdictQueue(gate_offset_counts=50, max_depth=4)
    queue.enqueue("part-00044", position=100)

    decision = queue.dequeue_at_gate(position=150)
    attached = queue.attach_verdict(_verdict("part-00044", "pass"))

    assert decision.part_id == "part-00044"
    assert decision.deadline_missed is True
    assert decision.reason == "deadline_missed"
    assert decision.disposition is PartDisposition.reject
    assert decision.pass_signal_allowed is False
    assert decision.fail_closed is True
    assert attached.attached is False
    assert attached.late is True
    assert attached.reason == "deadline_missed"


def test_queue_overflow_reports_fail_closed_gate_decision() -> None:
    queue = PartTrackingVerdictQueue(gate_offset_counts=50, max_depth=1)
    first = queue.enqueue("part-00045", position=100)
    overflow = queue.enqueue("part-00046", position=110)

    decision = queue.dequeue_at_gate(position=160)

    assert first.accepted is True
    assert overflow.accepted is False
    assert overflow.fail_closed is True
    assert overflow.reason == "queue_overflow"
    assert decision.overflow is True
    assert decision.reason == "queue_overflow"
    assert decision.disposition is PartDisposition.reject
    assert decision.pass_signal_allowed is False
    assert decision.fail_closed is True


def test_out_of_order_verdicts_attach_by_part_id_not_queue_order() -> None:
    queue = PartTrackingVerdictQueue(gate_offset_counts=50, max_depth=4)
    queue.enqueue("part-00047", position=100)
    queue.enqueue("part-00048", position=110)

    second_attached = queue.attach_verdict(_verdict("part-00048", "fail"))
    first_attached = queue.attach_verdict(_verdict("part-00047", "pass"))
    first = queue.dequeue_at_gate(position=150)
    second = queue.dequeue_at_gate(position=160)

    assert second_attached.attached is True
    assert first_attached.attached is True
    assert first.part_id == "part-00047"
    assert first.disposition is PartDisposition.allow
    assert second.part_id == "part-00048"
    assert second.verdict == _verdict("part-00048", "fail")
    assert second.disposition is PartDisposition.reject
    assert second.reason == "non_pass_verdict"
