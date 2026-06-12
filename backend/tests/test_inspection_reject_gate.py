"""Tests for the C8.C4 reject-gate protocol."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.inspection.fieldbus import ModbusFieldbusAdapter, SimulatedModbusEndpoint
from backend.inspection.part_tracking import PartDisposition
from backend.inspection.reject_gate import (
    D3_SAFETY_INVARIANT,
    RejectGateConfig,
    RejectGateController,
    RejectGateRegisterMap,
)
from backend.inspection.verdict import Defect, InspectionVerdict


NOW = datetime(2026, 6, 12, 8, 15, 30, tzinfo=timezone.utc)


class FakeClockMs:
    def __init__(self, ticks: list[float]) -> None:
        self._ticks = list(ticks)

    def __call__(self) -> float:
        return self._ticks.pop(0)


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


def _controller(
    endpoint: SimulatedModbusEndpoint,
    *,
    ticks: list[float],
    deadline_ms: float = 25.0,
) -> RejectGateController:
    adapter = ModbusFieldbusAdapter(
        read_register_fn=endpoint.read_register,
        write_register_fn=endpoint.write_register,
    )
    return RejectGateController(
        adapter,
        RejectGateConfig(
            gate_offset_counts=50,
            max_depth=4,
            verdict_deadline_ms=deadline_ms,
            register_map=RejectGateRegisterMap(
                reject_command_address=40003,
                reject_value=1,
                allow_value=0,
            ),
        ),
        clock_ms=FakeClockMs(ticks),
    )


def test_d3_invariant_is_available_from_reject_gate_module() -> None:
    assert D3_SAFETY_INVARIANT == (
        "This software is never part of the e-stop/safety chain and claims no "
        "safety function (ISO 13849/IEC 62061 out of scope). It MONITORS safety "
        "state; on unknown/violated state it WITHHOLDS the pass signal - "
        "fail-closed protects product disposition only; the PLC decides line "
        "behavior."
    )


def test_on_time_pass_verdict_writes_allow_command_for_part_at_gate() -> None:
    endpoint = SimulatedModbusEndpoint({40003: 1}, writable={40003})
    controller = _controller(endpoint, ticks=[970.0])

    capture = controller.enqueue_part(
        "part-00042",
        position=100,
        gate_window_at_ms=1000.0,
    )
    verdict = controller.deliver_verdict(_verdict("part-00042", "pass"))
    actuation = controller.actuate_at_gate(position=150)

    assert capture.capture.accepted is True
    assert capture.verdict_deadline_at_ms == pytest.approx(975.0)
    assert verdict.accepted is True
    assert verdict.attached is True
    assert verdict.delivered_ms_before_window == pytest.approx(30.0)
    assert actuation.decision.part_id == "part-00042"
    assert actuation.decision.disposition is PartDisposition.allow
    assert actuation.command_value == 0
    assert actuation.write.ok is True
    assert endpoint.registers[40003] == 0
    assert actuation.pass_signal_allowed is True
    assert actuation.fail_closed is False


def test_fail_verdict_writes_reject_command_for_part_at_gate() -> None:
    endpoint = SimulatedModbusEndpoint({40003: 0}, writable={40003})
    controller = _controller(endpoint, ticks=[950.0])

    controller.enqueue_part("part-00043", position=100, gate_window_at_ms=1000.0)
    verdict = controller.deliver_verdict(_verdict("part-00043", "fail"))
    actuation = controller.actuate_at_gate(position=150)

    assert verdict.accepted is True
    assert actuation.decision.part_id == "part-00043"
    assert actuation.decision.disposition is PartDisposition.reject
    assert actuation.decision.reason == "non_pass_verdict"
    assert actuation.command_value == 1
    assert endpoint.registers[40003] == 1
    assert actuation.pass_signal_allowed is False
    assert actuation.fail_closed is False


def test_late_pass_verdict_is_not_attached_and_fails_closed_at_gate() -> None:
    endpoint = SimulatedModbusEndpoint({40003: 0}, writable={40003})
    controller = _controller(endpoint, ticks=[980.0])

    controller.enqueue_part("part-00044", position=100, gate_window_at_ms=1000.0)
    verdict = controller.deliver_verdict(_verdict("part-00044", "pass"))
    actuation = controller.actuate_at_gate(position=150)

    assert verdict.accepted is False
    assert verdict.attached is False
    assert verdict.late is True
    assert verdict.delivered_ms_before_window == pytest.approx(20.0)
    assert verdict.deadline_ms == pytest.approx(25.0)
    assert verdict.reason == "verdict_deadline_missed"
    assert actuation.decision.part_id == "part-00044"
    assert actuation.decision.deadline_missed is True
    assert actuation.decision.reason == "deadline_missed"
    assert actuation.command_value == 1
    assert endpoint.registers[40003] == 1
    assert actuation.pass_signal_allowed is False
    assert actuation.fail_closed is True


def test_verdict_delivered_exactly_at_deadline_is_accepted() -> None:
    endpoint = SimulatedModbusEndpoint({40003: 1}, writable={40003})
    controller = _controller(endpoint, ticks=[975.0])

    controller.enqueue_part("part-00045", position=100, gate_window_at_ms=1000.0)
    verdict = controller.deliver_verdict(_verdict("part-00045", "pass"))
    actuation = controller.actuate_at_gate(position=150)

    assert verdict.accepted is True
    assert verdict.delivered_ms_before_window == pytest.approx(25.0)
    assert actuation.command_value == 0
    assert endpoint.registers[40003] == 0


def test_pending_verdict_is_checked_against_window_when_part_is_tracked() -> None:
    endpoint = SimulatedModbusEndpoint({40003: 1}, writable={40003})
    controller = _controller(endpoint, ticks=[900.0])

    verdict = controller.deliver_verdict(_verdict("part-00046", "pass"))
    capture = controller.enqueue_part(
        "part-00046",
        position=100,
        gate_window_at_ms=1000.0,
    )
    actuation = controller.actuate_at_gate(position=150)

    assert verdict.accepted is True
    assert verdict.attached is False
    assert verdict.reason == "part_not_yet_tracked"
    assert capture.pending_verdict is not None
    assert capture.pending_verdict.attached is True
    assert actuation.command_value == 0
    assert endpoint.registers[40003] == 0


def test_fieldbus_write_failure_reports_fail_closed_disposition() -> None:
    endpoint = SimulatedModbusEndpoint({40003: 0})
    controller = _controller(endpoint, ticks=[950.0])

    controller.enqueue_part("part-00047", position=100, gate_window_at_ms=1000.0)
    controller.deliver_verdict(_verdict("part-00047", "pass"))
    actuation = controller.actuate_at_gate(position=150)

    assert actuation.decision.disposition is PartDisposition.allow
    assert actuation.write.status == "read_only"
    assert actuation.pass_signal_allowed is False
    assert actuation.fail_closed is True
    assert actuation.reason == "reject_write_read_only"
