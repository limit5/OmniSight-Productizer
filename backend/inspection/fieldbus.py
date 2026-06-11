"""C8.C1 fieldbus adapter layer over connectivity-owned Modbus transport.

This software is never part of the e-stop/safety chain and claims no safety
function (ISO 13849/IEC 62061 out of scope). It MONITORS safety state; on
unknown/violated state it WITHHOLDS the pass signal - fail-closed protects
product disposition only; the PLC decides line behavior.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

from backend import machine_vision

D3_SAFETY_INVARIANT = (
    "This software is never part of the e-stop/safety chain and claims no "
    "safety function (ISO 13849/IEC 62061 out of scope). It MONITORS safety "
    "state; on unknown/violated state it WITHHOLDS the pass signal - "
    "fail-closed protects product disposition only; the PLC decides line "
    "behavior."
)

ReadRegisterFn = Callable[[str, Any], Mapping[str, Any]]
WriteRegisterFn = Callable[[str, Any, Any], Mapping[str, Any]]
ClockFn = Callable[[], float]
SleepFn = Callable[[float], None]


class ConnectionState(str, Enum):
    """Observed fieldbus connection state."""

    connected = "connected"
    degraded = "degraded"
    disconnected = "disconnected"


@dataclass(frozen=True)
class RegisterRead:
    """Normalised register-read result returned by fieldbus adapters."""

    address: Any
    status: str
    value: Any = None
    name: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class RegisterWrite:
    """Normalised register-write result returned by fieldbus adapters."""

    address: Any
    status: str
    value: Any = None
    name: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class FieldbusConnectionState:
    """Connection and product-disposition state exposed to line logic."""

    state: ConnectionState
    fail_closed: bool
    pass_signal_allowed: bool
    last_error: str = ""
    last_poll_duration_s: float = 0.0
    last_poll_started_at: float | None = None


@dataclass(frozen=True)
class PollCycleResult:
    """Single poll-cycle summary with injectable timing evidence."""

    reads: dict[Any, RegisterRead]
    connection: FieldbusConnectionState
    duration_s: float
    deadline_s: float | None = None
    deadline_missed: bool = False
    slept_s: float = 0.0

    @property
    def fail_closed(self) -> bool:
        return self.connection.fail_closed

    @property
    def pass_signal_allowed(self) -> bool:
        return self.connection.pass_signal_allowed


class FieldbusAdapter(ABC):
    """Interface for line-logic fieldbus layers above connectivity transports."""

    @abstractmethod
    def connection_state(self) -> FieldbusConnectionState:
        """Return the latest observed fieldbus and pass-withhold state."""

    @abstractmethod
    def read_register(self, address: Any) -> RegisterRead:
        """Read one fieldbus register."""

    @abstractmethod
    def write_register(self, address: Any, value: Any) -> RegisterWrite:
        """Write one fieldbus register."""

    @abstractmethod
    def poll_cycle(
        self,
        addresses: Iterable[Any],
        *,
        deadline_s: float | None = None,
        expected_values: Mapping[Any, Any] | None = None,
    ) -> PollCycleResult:
        """Poll a register set and expose timing/fail-closed state."""


class ModbusFieldbusAdapter(FieldbusAdapter):
    """Modbus binding layered above the connectivity-owned transport token."""

    protocol = "modbus"

    def __init__(
        self,
        *,
        read_register_fn: ReadRegisterFn | None = None,
        write_register_fn: WriteRegisterFn | None = None,
        clock: ClockFn | None = None,
        sleeper: SleepFn | None = None,
    ) -> None:
        self._read_register_fn = read_register_fn or machine_vision.read_plc_register
        self._write_register_fn = write_register_fn or machine_vision.write_plc_register
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._connection = FieldbusConnectionState(
            state=ConnectionState.disconnected,
            fail_closed=True,
            pass_signal_allowed=False,
            last_error="not_polled",
        )

    def connection_state(self) -> FieldbusConnectionState:
        return self._connection

    def read_register(self, address: Any) -> RegisterRead:
        try:
            raw = self._read_register_fn(self.protocol, address)
        except Exception as exc:  # pragma: no cover - defensive transport guard
            self._mark_disconnected(str(exc))
            return RegisterRead(address=address, status="transport_error")

        result = RegisterRead(
            address=address,
            status=str(raw.get("status", "unknown")),
            value=raw.get("value"),
            name=str(raw.get("name", "")),
            raw=dict(raw),
        )
        if not result.ok:
            self._mark_degraded(f"read_{result.status}:{address}")
        return result

    def write_register(self, address: Any, value: Any) -> RegisterWrite:
        try:
            raw = self._write_register_fn(self.protocol, address, value)
        except Exception as exc:  # pragma: no cover - defensive transport guard
            self._mark_disconnected(str(exc))
            return RegisterWrite(address=address, status="transport_error", value=value)

        result = RegisterWrite(
            address=address,
            status=str(raw.get("status", "unknown")),
            value=raw.get("value", value),
            name=str(raw.get("name", "")),
            raw=dict(raw),
        )
        if not result.ok:
            self._mark_degraded(f"write_{result.status}:{address}")
        return result

    def poll_cycle(
        self,
        addresses: Iterable[Any],
        *,
        deadline_s: float | None = None,
        expected_values: Mapping[Any, Any] | None = None,
    ) -> PollCycleResult:
        start = self._clock()
        reads: dict[Any, RegisterRead] = {}
        first_error = ""
        expected = dict(expected_values or {})

        for address in addresses:
            read = self.read_register(address)
            reads[address] = read
            if not read.ok and not first_error:
                first_error = f"read_{read.status}:{address}"
            if (
                read.ok
                and address in expected
                and read.value != expected[address]
                and not first_error
            ):
                first_error = f"safety_state_violated:{address}"

        duration_s = self._clock() - start
        deadline_missed = deadline_s is not None and duration_s > deadline_s
        if deadline_missed and not first_error:
            first_error = "deadline_missed"

        if first_error:
            state = (
                ConnectionState.disconnected
                if any(read.status == "transport_error" for read in reads.values())
                else ConnectionState.degraded
            )
            self._connection = FieldbusConnectionState(
                state=state,
                fail_closed=True,
                pass_signal_allowed=False,
                last_error=first_error,
                last_poll_duration_s=duration_s,
                last_poll_started_at=start,
            )
        else:
            self._connection = FieldbusConnectionState(
                state=ConnectionState.connected,
                fail_closed=False,
                pass_signal_allowed=True,
                last_poll_duration_s=duration_s,
                last_poll_started_at=start,
            )

        return PollCycleResult(
            reads=reads,
            connection=self._connection,
            duration_s=duration_s,
            deadline_s=deadline_s,
            deadline_missed=deadline_missed,
        )

    def wait_for_next_poll(self, cycle_started_at: float, poll_period_s: float) -> float:
        """Sleep until the next cycle boundary; tests inject clock/sleeper."""

        remaining_s = poll_period_s - (self._clock() - cycle_started_at)
        if remaining_s <= 0:
            return 0.0
        self._sleeper(remaining_s)
        return remaining_s

    def _mark_degraded(self, error: str) -> None:
        self._connection = FieldbusConnectionState(
            state=ConnectionState.degraded,
            fail_closed=True,
            pass_signal_allowed=False,
            last_error=error,
        )

    def _mark_disconnected(self, error: str) -> None:
        self._connection = FieldbusConnectionState(
            state=ConnectionState.disconnected,
            fail_closed=True,
            pass_signal_allowed=False,
            last_error=error,
        )


class SimulatedModbusEndpoint:
    """Small in-memory Modbus endpoint for fieldbus adapter tests."""

    def __init__(
        self,
        registers: Mapping[Any, Any] | None = None,
        *,
        writable: Iterable[Any] = (),
        connected: bool = True,
    ) -> None:
        self.registers = dict(registers or {})
        self.writable = set(writable)
        self.connected = connected

    def read_register(self, protocol: str, address: Any) -> dict[str, Any]:
        if protocol != ModbusFieldbusAdapter.protocol:
            return {"protocol": protocol, "status": "unsupported_protocol"}
        if not self.connected:
            raise ConnectionError("modbus endpoint disconnected")
        if address not in self.registers:
            return {"protocol": protocol, "address": address, "status": "not_found"}
        return {
            "protocol": protocol,
            "address": address,
            "value": self.registers[address],
            "status": "ok",
            "simulated": True,
        }

    def write_register(self, protocol: str, address: Any, value: Any) -> dict[str, Any]:
        if protocol != ModbusFieldbusAdapter.protocol:
            return {"protocol": protocol, "status": "unsupported_protocol"}
        if not self.connected:
            raise ConnectionError("modbus endpoint disconnected")
        if address not in self.registers:
            return {"protocol": protocol, "address": address, "status": "not_found"}
        if address not in self.writable:
            return {"protocol": protocol, "address": address, "status": "read_only"}
        self.registers[address] = value
        return {
            "protocol": protocol,
            "address": address,
            "value": value,
            "status": "ok",
            "simulated": True,
        }


__all__ = [
    "D3_SAFETY_INVARIANT",
    "ConnectionState",
    "FieldbusAdapter",
    "FieldbusConnectionState",
    "ModbusFieldbusAdapter",
    "PollCycleResult",
    "RegisterRead",
    "RegisterWrite",
    "SimulatedModbusEndpoint",
]
