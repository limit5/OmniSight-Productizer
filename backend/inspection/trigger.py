"""C8.A3 trigger and exposure-sync contracts for inspection acquisition.

This module intentionally stays independent of camera implementations.  A later
conformance leaf wires these contracts into the concrete acquisition layer.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping


ClockFn = Callable[[], float]
TriggerCallback = Callable[["TriggerEvent"], None]
GpioReadFn = Callable[[], bool]


class TriggerKind(str, Enum):
    """Supported acquisition trigger sources."""

    software = "software"
    hardware = "hardware"


@dataclass(frozen=True)
class TriggerEvent:
    """One acquisition trigger observed by the acquisition layer."""

    kind: TriggerKind
    triggered_at_s: float
    sequence: int
    metadata: Mapping[str, Any] = field(default_factory=dict)


class TriggerSource(ABC):
    """Interface shared by hardware-shaped and software trigger sources."""

    @abstractmethod
    def arm(self) -> None:
        """Prepare the source to report future trigger events."""

    @abstractmethod
    def next_trigger(self) -> TriggerEvent | None:
        """Return the next trigger event, or ``None`` when no edge is ready."""


class SoftwareTriggerSource(TriggerSource):
    """Software-trigger source for simulator and operator-driven acquisition."""

    def __init__(self, *, clock: ClockFn | None = None) -> None:
        self._clock = clock or time.monotonic
        self._armed = False
        self._next_sequence = 1
        self._pending: list[TriggerEvent] = []

    def arm(self) -> None:
        self._armed = True

    def fire(
        self,
        *,
        triggered_at_s: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TriggerEvent:
        """Queue one software trigger event and return it for test visibility."""

        if not self._armed:
            raise RuntimeError("software trigger source must be armed before fire")

        event = TriggerEvent(
            kind=TriggerKind.software,
            triggered_at_s=self._clock() if triggered_at_s is None else triggered_at_s,
            sequence=self._next_sequence,
            metadata=dict(metadata or {}),
        )
        self._next_sequence += 1
        self._pending.append(event)
        return event

    def next_trigger(self) -> TriggerEvent | None:
        if not self._armed or not self._pending:
            return None
        return self._pending.pop(0)


class GpioTriggerSource(TriggerSource):
    """GPIO-shaped hardware trigger simulator with rising-edge detection."""

    def __init__(
        self,
        *,
        read_pin: GpioReadFn,
        clock: ClockFn | None = None,
        callback: TriggerCallback | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._read_pin = read_pin
        self._clock = clock or time.monotonic
        self._callback = callback
        self._metadata = dict(metadata or {})
        self._armed = False
        self._last_level = False
        self._next_sequence = 1

    def arm(self) -> None:
        self._last_level = bool(self._read_pin())
        self._armed = True

    def next_trigger(self) -> TriggerEvent | None:
        if not self._armed:
            return None

        level = bool(self._read_pin())
        rising_edge = level and not self._last_level
        self._last_level = level
        if not rising_edge:
            return None

        event = TriggerEvent(
            kind=TriggerKind.hardware,
            triggered_at_s=self._clock(),
            sequence=self._next_sequence,
            metadata=dict(self._metadata),
        )
        self._next_sequence += 1
        if self._callback is not None:
            self._callback(event)
        return event


@dataclass(frozen=True)
class ExposureSyncContract:
    """Timing relation between trigger, strobe, exposure, and frame grab."""

    trigger_to_exposure_delay_s: float
    exposure_duration_s: float
    strobe_fire_offset_s: float
    strobe_duration_s: float
    frame_grab_offset_s: float
    tolerance_s: float = 0.0

    def validate(self) -> None:
        """Validate the contract independent of any camera implementation."""

        if self.trigger_to_exposure_delay_s < 0:
            raise ValueError("trigger_to_exposure_delay_s must be >= 0")
        if self.exposure_duration_s <= 0:
            raise ValueError("exposure_duration_s must be > 0")
        if self.strobe_fire_offset_s < 0:
            raise ValueError("strobe_fire_offset_s must be >= 0")
        if self.strobe_duration_s <= 0:
            raise ValueError("strobe_duration_s must be > 0")
        if self.frame_grab_offset_s < 0:
            raise ValueError("frame_grab_offset_s must be >= 0")
        if self.tolerance_s < 0:
            raise ValueError("tolerance_s must be >= 0")

        strobe_end_offset_s = self.strobe_fire_offset_s + self.strobe_duration_s
        if strobe_end_offset_s > self.exposure_duration_s + self.tolerance_s:
            raise ValueError("strobe window must fit within exposure window")

    def simulate(self, trigger: TriggerEvent) -> "ExposureSyncTimeline":
        """Build a simulated timing observation for one trigger event."""

        self.validate()
        exposure_start_s = trigger.triggered_at_s + self.trigger_to_exposure_delay_s
        exposure_end_s = exposure_start_s + self.exposure_duration_s
        strobe_fire_s = exposure_start_s + self.strobe_fire_offset_s
        strobe_end_s = strobe_fire_s + self.strobe_duration_s
        frame_grab_s = exposure_end_s + self.frame_grab_offset_s

        timeline = ExposureSyncTimeline(
            trigger=trigger,
            exposure_start_s=exposure_start_s,
            exposure_end_s=exposure_end_s,
            strobe_fire_s=strobe_fire_s,
            strobe_end_s=strobe_end_s,
            frame_grab_s=frame_grab_s,
            contract=self,
        )
        timeline.validate()
        return timeline


@dataclass(frozen=True)
class ExposureSyncTimeline:
    """Simulated timing evidence for one acquisition cycle."""

    trigger: TriggerEvent
    exposure_start_s: float
    exposure_end_s: float
    strobe_fire_s: float
    strobe_end_s: float
    frame_grab_s: float
    contract: ExposureSyncContract

    def validate(self) -> None:
        """Assert strobe and frame timing satisfy the contract."""

        tolerance_s = self.contract.tolerance_s
        if self.exposure_start_s < self.trigger.triggered_at_s - tolerance_s:
            raise ValueError("exposure must not start before trigger")
        if self.exposure_end_s <= self.exposure_start_s:
            raise ValueError("exposure window must have positive duration")
        if self.strobe_fire_s < self.exposure_start_s - tolerance_s:
            raise ValueError("strobe must not fire before exposure window")
        if self.strobe_end_s > self.exposure_end_s + tolerance_s:
            raise ValueError("strobe must end within exposure window")
        if self.frame_grab_s < self.exposure_end_s - tolerance_s:
            raise ValueError("frame grab must not precede exposure end")


__all__ = [
    "ClockFn",
    "ExposureSyncContract",
    "ExposureSyncTimeline",
    "GpioReadFn",
    "GpioTriggerSource",
    "SoftwareTriggerSource",
    "TriggerCallback",
    "TriggerEvent",
    "TriggerKind",
    "TriggerSource",
]
