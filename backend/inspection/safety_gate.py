"""C8.C2 safety-state monitor and product-disposition gate.

This software is never part of the e-stop/safety chain and claims no safety
function (ISO 13849/IEC 62061 out of scope). It MONITORS safety state; on
unknown/violated state it WITHHOLDS the pass signal - fail-closed protects
product disposition only; the PLC decides line behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from backend.inspection.fieldbus import D3_SAFETY_INVARIANT, FieldbusAdapter
from backend.inspection.verdict import InspectionVerdict


class SafetyState(str, Enum):
    """Observed e-stop/interlock state for product-disposition gating."""

    ok = "ok"
    estop = "estop"
    interlock_open = "interlock_open"
    unknown = "unknown"


@dataclass(frozen=True)
class SafetyRegisterMap:
    """Fieldbus registers used to monitor e-stop and interlock state."""

    estop_address: Any
    interlock_address: Any
    estop_ok_value: Any = 0
    interlock_ok_value: Any = 1


@dataclass(frozen=True)
class SafetyStateSample:
    """Single safety-state poll result."""

    state: SafetyState
    pass_signal_allowed: bool
    fail_closed: bool
    estop_value: Any = None
    interlock_value: Any = None
    reason: str = ""


@dataclass(frozen=True)
class VerdictGateResult:
    """Verdict emission gate result for the PLC-facing pass signal."""

    verdict: InspectionVerdict
    safety: SafetyStateSample
    pass_signal_allowed: bool
    withheld: bool
    reason: str = ""


class SafetyStateMonitor:
    """Poll e-stop/interlock registers through a FieldbusAdapter."""

    def __init__(
        self,
        adapter: FieldbusAdapter,
        register_map: SafetyRegisterMap,
    ) -> None:
        self._adapter = adapter
        self._register_map = register_map

    def poll(self) -> SafetyStateSample:
        """Return the current safety state from e-stop/interlock registers."""

        estop = self._adapter.read_register(self._register_map.estop_address)
        if not estop.ok:
            return SafetyStateSample(
                state=SafetyState.unknown,
                pass_signal_allowed=False,
                fail_closed=True,
                reason=f"estop_read_{estop.status}",
            )

        interlock = self._adapter.read_register(self._register_map.interlock_address)
        if not interlock.ok:
            return SafetyStateSample(
                state=SafetyState.unknown,
                pass_signal_allowed=False,
                fail_closed=True,
                estop_value=estop.value,
                reason=f"interlock_read_{interlock.status}",
            )

        if estop.value != self._register_map.estop_ok_value:
            return SafetyStateSample(
                state=SafetyState.estop,
                pass_signal_allowed=False,
                fail_closed=True,
                estop_value=estop.value,
                interlock_value=interlock.value,
                reason="estop_active",
            )

        if interlock.value != self._register_map.interlock_ok_value:
            return SafetyStateSample(
                state=SafetyState.interlock_open,
                pass_signal_allowed=False,
                fail_closed=True,
                estop_value=estop.value,
                interlock_value=interlock.value,
                reason="interlock_open",
            )

        return SafetyStateSample(
            state=SafetyState.ok,
            pass_signal_allowed=True,
            fail_closed=False,
            estop_value=estop.value,
            interlock_value=interlock.value,
        )


def gate_verdict_pass_signal(
    verdict: InspectionVerdict,
    safety: SafetyStateSample,
) -> VerdictGateResult:
    """Apply the D3 invariant to PLC-facing pass-signal emission.

    This software is never part of the e-stop/safety chain and claims no safety
    function (ISO 13849/IEC 62061 out of scope). It MONITORS safety state; on
    unknown/violated state it WITHHOLDS the pass signal - fail-closed protects
    product disposition only; the PLC decides line behavior.
    """

    if verdict.verdict != "pass":
        return VerdictGateResult(
            verdict=verdict,
            safety=safety,
            pass_signal_allowed=False,
            withheld=False,
            reason="non_pass_verdict",
        )

    if safety.state != SafetyState.ok:
        return VerdictGateResult(
            verdict=verdict,
            safety=safety,
            pass_signal_allowed=False,
            withheld=True,
            reason=safety.reason or f"safety_{safety.state.value}",
        )

    return VerdictGateResult(
        verdict=verdict,
        safety=safety,
        pass_signal_allowed=True,
        withheld=False,
    )


__all__ = [
    "D3_SAFETY_INVARIANT",
    "SafetyRegisterMap",
    "SafetyState",
    "SafetyStateMonitor",
    "SafetyStateSample",
    "VerdictGateResult",
    "gate_verdict_pass_signal",
]
