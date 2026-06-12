"""C8.C4 reject-gate protocol over part tracking and fieldbus writes.

This software is never part of the e-stop/safety chain and claims no safety
function (ISO 13849/IEC 62061 out of scope). It MONITORS safety state; on
unknown/violated state it WITHHOLDS the pass signal - fail-closed protects
product disposition only; the PLC decides line behavior.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from backend.inspection.fieldbus import (
    D3_SAFETY_INVARIANT,
    FieldbusAdapter,
    RegisterWrite,
)
from backend.inspection.part_tracking import (
    GateDecision,
    PartCaptureResult,
    PartDisposition,
    PartTrackingVerdictQueue,
    VerdictAttachResult,
)
from backend.inspection.verdict import InspectionVerdict


ClockMsFn = Callable[[], float]


@dataclass(frozen=True)
class RejectGateRegisterMap:
    """Fieldbus register used to hand product disposition to the PLC."""

    reject_command_address: Any
    reject_value: Any = 1
    allow_value: Any = 0


@dataclass(frozen=True)
class RejectGateConfig:
    """Reject-gate timing and queue configuration."""

    gate_offset_counts: int
    max_depth: int
    verdict_deadline_ms: float
    register_map: RejectGateRegisterMap

    def __post_init__(self) -> None:
        if self.verdict_deadline_ms < 0:
            raise ValueError("verdict_deadline_ms must be non-negative")


@dataclass(frozen=True)
class RejectGateCaptureResult:
    """Part enqueue result plus its reject-window deadline."""

    capture: PartCaptureResult
    gate_window_at_ms: float
    verdict_deadline_at_ms: float
    pending_verdict: VerdictAttachResult | None = None


@dataclass(frozen=True)
class RejectGateVerdictResult:
    """Verdict delivery result against the reject-window contract."""

    part_id: str
    accepted: bool
    attached: bool
    late: bool
    delivered_ms_before_window: float | None = None
    deadline_ms: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class RejectGateActuationResult:
    """Fieldbus write result for the part currently at the reject gate."""

    decision: GateDecision
    write: RegisterWrite
    command_value: Any
    pass_signal_allowed: bool
    fail_closed: bool
    reason: str = ""


@dataclass(frozen=True)
class _PendingVerdict:
    verdict: InspectionVerdict
    delivered_at_ms: float


class RejectGateController:
    """Drive reject-register disposition writes for gate-windowed parts.

    The PLC owns actuation timing. This controller only delivers the
    recipe-mapped disposition value before the gate window; C8.D will re-plumb
    ``RejectGateRegisterMap`` from versioned recipe data instead of the static
    constructor value used in this Wave-2 leaf.
    """

    def __init__(
        self,
        adapter: FieldbusAdapter,
        config: RejectGateConfig,
        *,
        clock_ms: ClockMsFn | None = None,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._clock_ms = clock_ms or _monotonic_ms
        self._queue = PartTrackingVerdictQueue(
            gate_offset_counts=config.gate_offset_counts,
            max_depth=config.max_depth,
        )
        self._gate_windows_ms: dict[str, float] = {}
        self._pending_verdicts: dict[str, _PendingVerdict] = {}

    def enqueue_part(
        self,
        part_id: str,
        *,
        position: int,
        gate_window_at_ms: float,
    ) -> RejectGateCaptureResult:
        """Track a captured part and bind its PLC-owned reject window."""

        capture = self._queue.enqueue(part_id, position)
        deadline_at_ms = gate_window_at_ms - self._config.verdict_deadline_ms

        if not capture.accepted:
            return RejectGateCaptureResult(
                capture=capture,
                gate_window_at_ms=gate_window_at_ms,
                verdict_deadline_at_ms=deadline_at_ms,
            )

        self._gate_windows_ms[part_id] = gate_window_at_ms
        pending = self._pending_verdicts.pop(part_id, None)
        if pending is None:
            return RejectGateCaptureResult(
                capture=capture,
                gate_window_at_ms=gate_window_at_ms,
                verdict_deadline_at_ms=deadline_at_ms,
            )

        pending_result = self._attach_if_on_time(pending.verdict, pending.delivered_at_ms)
        return RejectGateCaptureResult(
            capture=capture,
            gate_window_at_ms=gate_window_at_ms,
            verdict_deadline_at_ms=deadline_at_ms,
            pending_verdict=VerdictAttachResult(
                part_id=pending_result.part_id,
                attached=pending_result.attached,
                late=pending_result.late,
                reason=pending_result.reason,
            ),
        )

    def deliver_verdict(self, verdict: InspectionVerdict) -> RejectGateVerdictResult:
        """Accept a verdict only if it meets the configured gate lead time."""

        delivered_at_ms = self._clock_ms()
        if verdict.part_id not in self._gate_windows_ms:
            self._pending_verdicts[verdict.part_id] = _PendingVerdict(
                verdict=verdict,
                delivered_at_ms=delivered_at_ms,
            )
            return RejectGateVerdictResult(
                part_id=verdict.part_id,
                accepted=True,
                attached=False,
                late=False,
                reason="part_not_yet_tracked",
            )

        return self._attach_if_on_time(verdict, delivered_at_ms)

    def actuate_at_gate(self, *, position: int) -> RejectGateActuationResult:
        """Write the product disposition for the part now at the reject gate."""

        decision = self._queue.dequeue_at_gate(position)
        command_value = self._command_value(decision.disposition)
        write = self._adapter.write_register(
            self._config.register_map.reject_command_address,
            command_value,
        )

        if decision.part_id is not None:
            self._gate_windows_ms.pop(decision.part_id, None)

        if not write.ok:
            return RejectGateActuationResult(
                decision=decision,
                write=write,
                command_value=command_value,
                pass_signal_allowed=False,
                fail_closed=True,
                reason=f"reject_write_{write.status}",
            )

        return RejectGateActuationResult(
            decision=decision,
            write=write,
            command_value=command_value,
            pass_signal_allowed=decision.pass_signal_allowed,
            fail_closed=decision.fail_closed,
            reason=decision.reason,
        )

    def _attach_if_on_time(
        self,
        verdict: InspectionVerdict,
        delivered_at_ms: float,
    ) -> RejectGateVerdictResult:
        gate_window_at_ms = self._gate_windows_ms[verdict.part_id]
        delivered_ms_before_window = gate_window_at_ms - delivered_at_ms

        if delivered_ms_before_window < self._config.verdict_deadline_ms:
            return RejectGateVerdictResult(
                part_id=verdict.part_id,
                accepted=False,
                attached=False,
                late=True,
                delivered_ms_before_window=delivered_ms_before_window,
                deadline_ms=self._config.verdict_deadline_ms,
                reason="verdict_deadline_missed",
            )

        attached = self._queue.attach_verdict(verdict)
        return RejectGateVerdictResult(
            part_id=verdict.part_id,
            accepted=attached.attached,
            attached=attached.attached,
            late=attached.late,
            delivered_ms_before_window=delivered_ms_before_window,
            deadline_ms=self._config.verdict_deadline_ms,
            reason=attached.reason,
        )

    def _command_value(self, disposition: PartDisposition) -> Any:
        if disposition is PartDisposition.allow:
            return self._config.register_map.allow_value
        return self._config.register_map.reject_value


def _monotonic_ms() -> float:
    return time.monotonic() * 1000.0


__all__ = [
    "D3_SAFETY_INVARIANT",
    "RejectGateActuationResult",
    "RejectGateCaptureResult",
    "RejectGateConfig",
    "RejectGateController",
    "RejectGateRegisterMap",
    "RejectGateVerdictResult",
]
