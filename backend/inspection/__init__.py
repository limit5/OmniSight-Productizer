"""Inspection contracts + runtime helpers shared by production-line vision components."""

from backend.inspection.fieldbus import (
    D3_SAFETY_INVARIANT,
    ConnectionState,
    FieldbusAdapter,
    FieldbusConnectionState,
    ModbusFieldbusAdapter,
    PollCycleResult,
    RegisterRead,
    RegisterWrite,
    SimulatedModbusEndpoint,
)
from backend.inspection.verdict import Defect, InspectionVerdict, verdict_json_schema

__all__ = [
    "D3_SAFETY_INVARIANT",
    "ConnectionState",
    "Defect",
    "FieldbusAdapter",
    "FieldbusConnectionState",
    "InspectionVerdict",
    "ModbusFieldbusAdapter",
    "PollCycleResult",
    "RegisterRead",
    "RegisterWrite",
    "SimulatedModbusEndpoint",
    "verdict_json_schema",
]
