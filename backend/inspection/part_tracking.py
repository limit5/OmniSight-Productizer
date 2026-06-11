"""C8.C3 position-indexed part-tracking verdict queue.

This software is never part of the e-stop/safety chain and claims no safety
function (ISO 13849/IEC 62061 out of scope). It MONITORS safety state; on
unknown/violated state it WITHHOLDS the pass signal - fail-closed protects
product disposition only; the PLC decides line behavior.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum

from backend.inspection.fieldbus import D3_SAFETY_INVARIANT
from backend.inspection.verdict import InspectionVerdict


class PartDisposition(str, Enum):
    """Product-disposition command returned when a tracked part reaches gate."""

    allow = "allow"
    reject = "reject"


@dataclass(frozen=True)
class PartCaptureResult:
    """Capture-trigger enqueue result with fail-closed overflow evidence."""

    part_id: str
    position: int
    accepted: bool
    fail_closed: bool = False
    reason: str = ""


@dataclass(frozen=True)
class VerdictAttachResult:
    """Verdict-to-part attach result."""

    part_id: str
    attached: bool
    late: bool = False
    reason: str = ""


@dataclass(frozen=True)
class GateDecision:
    """Verdict/disposition for the physical part now at the reject gate."""

    part_id: str | None
    position: int
    gate_position: int | None
    disposition: PartDisposition
    pass_signal_allowed: bool
    fail_closed: bool
    verdict: InspectionVerdict | None = None
    reason: str = ""
    missed_trigger: bool = False
    deadline_missed: bool = False
    overflow: bool = False


@dataclass
class _TrackedPart:
    part_id: str
    capture_position: int
    gate_position: int
    verdict: InspectionVerdict | None = None


class PartTrackingVerdictQueue:
    """Map asynchronous inspection verdicts to gate-positioned parts.

    The queue models the encoder-count/shift-register path between capture and
    reject gate. Capture triggers enqueue parts by physical position; verdicts
    attach by ``part_id`` and may arrive out of order; gate dequeue returns the
    disposition for the oldest tracked part whose gate position has arrived.
    """

    def __init__(self, *, gate_offset_counts: int, max_depth: int) -> None:
        if gate_offset_counts <= 0:
            raise ValueError("gate_offset_counts must be positive")
        if max_depth <= 0:
            raise ValueError("max_depth must be positive")

        self._gate_offset_counts = gate_offset_counts
        self._max_depth = max_depth
        self._parts: deque[_TrackedPart] = deque()
        self._pending_verdicts: dict[str, InspectionVerdict] = {}
        self._completed_part_ids: set[str] = set()
        self._overflowed = False

    @property
    def depth(self) -> int:
        """Return the number of tracked parts between camera and gate."""

        return len(self._parts)

    def enqueue(self, part_id: str, position: int) -> PartCaptureResult:
        """Track one captured part at its encoder position."""

        if not part_id:
            raise ValueError("part_id must be non-empty")
        if self._has_active_part(part_id) or part_id in self._completed_part_ids:
            raise ValueError(f"part already tracked: {part_id}")
        if len(self._parts) >= self._max_depth:
            self._overflowed = True
            return PartCaptureResult(
                part_id=part_id,
                position=position,
                accepted=False,
                fail_closed=True,
                reason="queue_overflow",
            )

        part = _TrackedPart(
            part_id=part_id,
            capture_position=position,
            gate_position=position + self._gate_offset_counts,
            verdict=self._pending_verdicts.pop(part_id, None),
        )
        self._parts.append(part)
        return PartCaptureResult(part_id=part_id, position=position, accepted=True)

    def attach_verdict(self, verdict: InspectionVerdict) -> VerdictAttachResult:
        """Attach an inspection verdict to its tracked part by ``part_id``."""

        part_id = verdict.part_id
        for part in self._parts:
            if part.part_id == part_id:
                part.verdict = verdict
                return VerdictAttachResult(part_id=part_id, attached=True)

        if part_id in self._completed_part_ids:
            return VerdictAttachResult(
                part_id=part_id,
                attached=False,
                late=True,
                reason="deadline_missed",
            )

        self._pending_verdicts[part_id] = verdict
        return VerdictAttachResult(
            part_id=part_id,
            attached=False,
            reason="part_not_yet_tracked",
        )

    def dequeue_at_gate(self, position: int) -> GateDecision:
        """Return the fail-closed disposition for the part now at the gate."""

        if self._overflowed:
            self._overflowed = False
            return GateDecision(
                part_id=None,
                position=position,
                gate_position=None,
                disposition=PartDisposition.reject,
                pass_signal_allowed=False,
                fail_closed=True,
                reason="queue_overflow",
                overflow=True,
            )

        if not self._parts or self._parts[0].gate_position > position:
            return GateDecision(
                part_id=None,
                position=position,
                gate_position=None,
                disposition=PartDisposition.reject,
                pass_signal_allowed=False,
                fail_closed=True,
                reason="missed_trigger_gap",
                missed_trigger=True,
            )

        part = self._parts.popleft()
        self._completed_part_ids.add(part.part_id)

        if part.verdict is None:
            return GateDecision(
                part_id=part.part_id,
                position=position,
                gate_position=part.gate_position,
                disposition=PartDisposition.reject,
                pass_signal_allowed=False,
                fail_closed=True,
                reason="deadline_missed",
                deadline_missed=True,
            )

        if part.verdict.verdict == "pass":
            return GateDecision(
                part_id=part.part_id,
                position=position,
                gate_position=part.gate_position,
                disposition=PartDisposition.allow,
                pass_signal_allowed=True,
                fail_closed=False,
                verdict=part.verdict,
            )

        return GateDecision(
            part_id=part.part_id,
            position=position,
            gate_position=part.gate_position,
            disposition=PartDisposition.reject,
            pass_signal_allowed=False,
            fail_closed=False,
            verdict=part.verdict,
            reason="non_pass_verdict",
        )

    def _has_active_part(self, part_id: str) -> bool:
        return any(part.part_id == part_id for part in self._parts)


__all__ = [
    "D3_SAFETY_INVARIANT",
    "GateDecision",
    "PartCaptureResult",
    "PartDisposition",
    "PartTrackingVerdictQueue",
    "VerdictAttachResult",
]
