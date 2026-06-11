"""OP-2129 - C8.C2 safety-state feed and D3 pass-signal gate."""

from __future__ import annotations

from copy import deepcopy

from backend.inspection.fieldbus import ModbusFieldbusAdapter, SimulatedModbusEndpoint
from backend.inspection.safety_gate import (
    D3_SAFETY_INVARIANT,
    SafetyRegisterMap,
    SafetyState,
    SafetyStateMonitor,
    gate_verdict_pass_signal,
)
from backend.inspection.verdict import InspectionVerdict


ESTOP_REGISTER = 10001
INTERLOCK_REGISTER = 10002
REGISTER_MAP = SafetyRegisterMap(
    estop_address=ESTOP_REGISTER,
    interlock_address=INTERLOCK_REGISTER,
    estop_ok_value=0,
    interlock_ok_value=1,
)


def _adapter(endpoint: SimulatedModbusEndpoint) -> ModbusFieldbusAdapter:
    return ModbusFieldbusAdapter(
        read_register_fn=endpoint.read_register,
        write_register_fn=endpoint.write_register,
    )


def _monitor(endpoint: SimulatedModbusEndpoint) -> SafetyStateMonitor:
    return SafetyStateMonitor(_adapter(endpoint), REGISTER_MAP)


def _pass_verdict() -> InspectionVerdict:
    return InspectionVerdict.from_payload(
        {
            "part_id": "part-00042",
            "station_id": "station-aoi-1",
            "verdict": "pass",
            "defects": [],
            "model_version": "rknn-scratch-detector@2026.06.12",
            "recipe_version": "recipe-aluminum-v3",
            "ts_capture": "2026-06-12T08:15:30.123456Z",
            "ts_verdict": "2026-06-12T08:15:30.223456Z",
        }
    )


def _fail_verdict() -> InspectionVerdict:
    payload = deepcopy(_pass_verdict().to_payload())
    payload["verdict"] = "fail"
    payload["defects"] = [
        {
            "class": "scratch",
            "bbox": [12.5, 17.0, 24.25, 31.5],
            "score": 0.97,
        }
    ]
    return InspectionVerdict.from_payload(payload)


def test_d3_invariant_is_exposed_verbatim() -> None:
    assert D3_SAFETY_INVARIANT == (
        "This software is never part of the e-stop/safety chain and claims no "
        "safety function (ISO 13849/IEC 62061 out of scope). It MONITORS safety "
        "state; on unknown/violated state it WITHHOLDS the pass signal - "
        "fail-closed protects product disposition only; the PLC decides line "
        "behavior."
    )


def test_ok_safety_state_allows_pass_signal() -> None:
    sample = _monitor(
        SimulatedModbusEndpoint({ESTOP_REGISTER: 0, INTERLOCK_REGISTER: 1})
    ).poll()
    result = gate_verdict_pass_signal(_pass_verdict(), sample)

    assert sample.state == SafetyState.ok
    assert sample.pass_signal_allowed is True
    assert sample.fail_closed is False
    assert result.pass_signal_allowed is True
    assert result.withheld is False


def test_estop_state_withholds_pass_signal_fail_closed() -> None:
    sample = _monitor(
        SimulatedModbusEndpoint({ESTOP_REGISTER: 1, INTERLOCK_REGISTER: 1})
    ).poll()
    result = gate_verdict_pass_signal(_pass_verdict(), sample)

    assert sample.state == SafetyState.estop
    assert sample.pass_signal_allowed is False
    assert sample.fail_closed is True
    assert sample.reason == "estop_active"
    assert result.pass_signal_allowed is False
    assert result.withheld is True


def test_interlock_open_state_withholds_pass_signal_fail_closed() -> None:
    sample = _monitor(
        SimulatedModbusEndpoint({ESTOP_REGISTER: 0, INTERLOCK_REGISTER: 0})
    ).poll()
    result = gate_verdict_pass_signal(_pass_verdict(), sample)

    assert sample.state == SafetyState.interlock_open
    assert sample.pass_signal_allowed is False
    assert sample.fail_closed is True
    assert sample.reason == "interlock_open"
    assert result.pass_signal_allowed is False
    assert result.withheld is True


def test_connection_loss_becomes_unknown_and_withholds_pass_signal() -> None:
    endpoint = SimulatedModbusEndpoint(
        {ESTOP_REGISTER: 0, INTERLOCK_REGISTER: 1},
        connected=False,
    )

    sample = _monitor(endpoint).poll()
    result = gate_verdict_pass_signal(_pass_verdict(), sample)

    assert sample.state == SafetyState.unknown
    assert sample.pass_signal_allowed is False
    assert sample.fail_closed is True
    assert sample.reason == "estop_read_transport_error"
    assert result.pass_signal_allowed is False
    assert result.withheld is True
    assert result.reason == "estop_read_transport_error"


def test_unknown_interlock_read_withholds_pass_signal() -> None:
    sample = _monitor(SimulatedModbusEndpoint({ESTOP_REGISTER: 0})).poll()
    result = gate_verdict_pass_signal(_pass_verdict(), sample)

    assert sample.state == SafetyState.unknown
    assert sample.reason == "interlock_read_not_found"
    assert result.pass_signal_allowed is False
    assert result.withheld is True


def test_fail_verdict_does_not_emit_pass_signal_even_when_safety_ok() -> None:
    sample = _monitor(
        SimulatedModbusEndpoint({ESTOP_REGISTER: 0, INTERLOCK_REGISTER: 1})
    ).poll()
    result = gate_verdict_pass_signal(_fail_verdict(), sample)

    assert sample.state == SafetyState.ok
    assert result.pass_signal_allowed is False
    assert result.withheld is False
    assert result.reason == "non_pass_verdict"
