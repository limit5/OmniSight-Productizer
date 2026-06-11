"""OP-2115 - C8.C1 fieldbus adapter over connectivity/modbus."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from backend.inspection.fieldbus import (
    D3_SAFETY_INVARIANT,
    ConnectionState,
    FieldbusAdapter,
    ModbusFieldbusAdapter,
    SimulatedModbusEndpoint,
)


class FakeClock:
    def __init__(self, ticks: list[float]) -> None:
        self._ticks = list(ticks)

    def __call__(self) -> float:
        return self._ticks.pop(0)


def _adapter(endpoint: SimulatedModbusEndpoint, **kwargs) -> ModbusFieldbusAdapter:
    return ModbusFieldbusAdapter(
        read_register_fn=endpoint.read_register,
        write_register_fn=endpoint.write_register,
        **kwargs,
    )


class TestFieldbusAdapterSurface:
    def test_interface_is_abstract(self):
        with pytest.raises(TypeError):
            FieldbusAdapter()  # type: ignore[abstract]

    def test_d3_invariant_is_copied_verbatim(self):
        assert D3_SAFETY_INVARIANT == (
            "This software is never part of the e-stop/safety chain and claims no "
            "safety function (ISO 13849/IEC 62061 out of scope). It MONITORS safety "
            "state; on unknown/violated state it WITHHOLDS the pass signal - "
            "fail-closed protects product disposition only; the PLC decides line "
            "behavior."
        )


class TestModbusFieldbusAdapter:
    def test_read_and_write_registers_through_modbus_binding(self):
        endpoint = SimulatedModbusEndpoint(
            {40001: 7, 40003: 0},
            writable={40003},
        )
        adapter = _adapter(endpoint)

        read = adapter.read_register(40001)
        assert read.ok is True
        assert read.value == 7

        write = adapter.write_register(40003, 1)
        assert write.ok is True
        assert write.value == 1
        assert endpoint.registers[40003] == 1

    def test_read_only_write_marks_fail_closed_degraded(self):
        endpoint = SimulatedModbusEndpoint({40001: 7})
        adapter = _adapter(endpoint)

        write = adapter.write_register(40001, 0)
        state = adapter.connection_state()

        assert write.status == "read_only"
        assert state.state == ConnectionState.degraded
        assert state.fail_closed is True
        assert state.pass_signal_allowed is False

    def test_poll_cycle_timing_surface_uses_injected_clock(self):
        endpoint = SimulatedModbusEndpoint({10001: 1, 10002: 1})
        adapter = _adapter(endpoint, clock=FakeClock([10.0, 10.012]))

        cycle = adapter.poll_cycle([10001, 10002], deadline_s=0.020)

        assert cycle.duration_s == pytest.approx(0.012)
        assert cycle.deadline_s == pytest.approx(0.020)
        assert cycle.deadline_missed is False
        assert cycle.connection.state == ConnectionState.connected
        assert cycle.pass_signal_allowed is True

    def test_poll_cycle_deadline_miss_fails_closed(self):
        endpoint = SimulatedModbusEndpoint({10001: 1})
        adapter = _adapter(endpoint, clock=FakeClock([5.0, 5.030]))

        cycle = adapter.poll_cycle([10001], deadline_s=0.010)

        assert cycle.deadline_missed is True
        assert cycle.fail_closed is True
        assert cycle.connection.state == ConnectionState.degraded
        assert cycle.connection.last_error == "deadline_missed"

    def test_poll_cycle_violated_safety_state_fails_closed(self):
        endpoint = SimulatedModbusEndpoint({10002: 0})
        adapter = _adapter(endpoint, clock=FakeClock([8.0, 8.001]))

        cycle = adapter.poll_cycle([10002], expected_values={10002: 1})

        assert cycle.fail_closed is True
        assert cycle.pass_signal_allowed is False
        assert cycle.connection.state == ConnectionState.degraded
        assert cycle.connection.last_error == "safety_state_violated:10002"

    def test_wait_for_next_poll_is_mockable_without_real_sleep(self):
        slept: list[float] = []
        adapter = ModbusFieldbusAdapter(
            clock=FakeClock([20.010]),
            sleeper=slept.append,
        )

        remaining = adapter.wait_for_next_poll(20.0, 0.025)

        assert remaining == pytest.approx(0.015)
        assert slept == pytest.approx([0.015])

    def test_connection_loss_exposes_fail_closed_state(self):
        endpoint = SimulatedModbusEndpoint({10001: 1}, connected=False)
        adapter = _adapter(endpoint, clock=FakeClock([30.0, 30.001]))

        cycle = adapter.poll_cycle([10001], deadline_s=0.010)

        assert cycle.fail_closed is True
        assert cycle.pass_signal_allowed is False
        assert cycle.connection.state == ConnectionState.disconnected
        assert cycle.reads[10001].status == "transport_error"

    def test_unknown_register_exposes_fail_closed_state(self):
        endpoint = SimulatedModbusEndpoint({10001: 1})
        adapter = _adapter(endpoint, clock=FakeClock([40.0, 40.001]))

        cycle = adapter.poll_cycle([99999], deadline_s=0.010)

        assert cycle.fail_closed is True
        assert cycle.connection.state == ConnectionState.degraded
        assert cycle.connection.last_error == "read_not_found:99999"


class TestConnectivityOwnership:
    def test_connectivity_skill_keeps_plc_transport_and_no_new_line_tokens(self):
        skill_path = Path("configs/skills/connectivity/skill.yaml")
        data = yaml.safe_load(skill_path.read_text())

        assert data["provides"] == ["plc-transport"]
        assert "reject-gate" not in data["provides"]
        assert "tact-sync" not in data["provides"]
