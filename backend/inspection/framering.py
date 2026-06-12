"""C8.A4 frame ring buffer for zero-copy inspection handoff.

Camera acquisition copies each grabbed frame into a pre-allocated slot.  The
inference side borrows a memoryview-backed lease and releases it when done, so
the ring never overwrites memory that is still in use by a consumer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from backend.inspection.camera import CameraBus, CameraFrame, CameraSource


@dataclass(frozen=True)
class FrameRingStats:
    """Backpressure counters and current occupancy for the frame ring."""

    capacity: int
    slot_size_bytes: int
    published_frames: int
    borrowed_frames: int
    dropped_frames: int
    lag_events: int
    ready_frames: int
    leased_frames: int


@dataclass
class _FrameSlot:
    payload: bytearray
    generation: int = 0
    frame_id: int | None = None
    timestamp_s: float | None = None
    width: int | None = None
    height: int | None = None
    pixel_format: str | None = None
    payload_size: int = 0
    source_id: str | None = None
    bus: CameraBus | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    ready: bool = False
    leased: bool = False

    def clear(self) -> None:
        self.frame_id = None
        self.timestamp_s = None
        self.width = None
        self.height = None
        self.pixel_format = None
        self.payload_size = 0
        self.source_id = None
        self.bus = None
        self.metadata = {}
        self.ready = False
        self.leased = False


class FrameLease:
    """Consumer lease over one pre-allocated frame slot."""

    def __init__(
        self,
        *,
        ring: "FrameRingBuffer",
        slot_index: int,
        generation: int,
        frame_id: int,
        timestamp_s: float,
        width: int,
        height: int,
        pixel_format: str,
        payload: memoryview,
        source_id: str,
        bus: CameraBus,
        metadata: Mapping[str, Any],
    ) -> None:
        self._ring = ring
        self._slot_index = slot_index
        self._generation = generation
        self.frame_id = frame_id
        self.timestamp_s = timestamp_s
        self.width = width
        self.height = height
        self.pixel_format = pixel_format
        self._payload = payload
        self.source_id = source_id
        self.bus = bus
        self.metadata = dict(metadata)
        self._released = False

    @property
    def payload(self) -> memoryview:
        """Return the borrowed frame payload as a memoryview."""

        if self._released:
            raise RuntimeError("frame lease has been released")
        return self._payload

    @property
    def size_bytes(self) -> int:
        """Return the borrowed payload size in bytes."""

        return len(self.payload)

    @property
    def released(self) -> bool:
        """Return whether this lease has already been released."""

        return self._released

    def release(self) -> None:
        """Release the borrowed slot back to the producer ring."""

        if self._released:
            return
        self._released = True
        self._payload.release()
        self._ring._release(self._slot_index, self._generation)

    def __enter__(self) -> "FrameLease":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


class FrameRingBuffer:
    """Fixed-size ring of pre-allocated frame slots."""

    def __init__(self, *, capacity: int, slot_size_bytes: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if slot_size_bytes <= 0:
            raise ValueError("slot_size_bytes must be > 0")

        self._slots = [
            _FrameSlot(payload=bytearray(slot_size_bytes)) for _ in range(capacity)
        ]
        self._ready: list[int] = []
        self._write_cursor = 0
        self._published_frames = 0
        self._borrowed_frames = 0
        self._dropped_frames = 0
        self._lag_events = 0

    @property
    def capacity(self) -> int:
        """Return the number of pre-allocated slots."""

        return len(self._slots)

    @property
    def slot_size_bytes(self) -> int:
        """Return the fixed byte capacity of each slot."""

        return len(self._slots[0].payload)

    def publish(self, frame: CameraFrame) -> bool:
        """Copy ``frame`` into the ring, dropping the oldest ready frame if full."""

        if frame.size_bytes > self.slot_size_bytes:
            raise ValueError("frame payload exceeds slot_size_bytes")

        slot_index = self._next_writable_slot()
        if slot_index is None:
            self._dropped_frames += 1
            self._lag_events += 1
            return False

        slot = self._slots[slot_index]
        slot.generation += 1
        slot.payload[: frame.size_bytes] = frame.payload
        slot.frame_id = frame.frame_id
        slot.timestamp_s = frame.timestamp_s
        slot.width = frame.width
        slot.height = frame.height
        slot.pixel_format = frame.pixel_format
        slot.payload_size = frame.size_bytes
        slot.source_id = frame.source_id
        slot.bus = frame.bus
        slot.metadata = dict(frame.metadata)
        slot.ready = True
        slot.leased = False
        self._ready.append(slot_index)
        self._published_frames += 1
        self._write_cursor = (slot_index + 1) % self.capacity
        return True

    def grab_from(self, source: CameraSource) -> bool:
        """Grab one frame from ``source`` and publish it into the ring."""

        return self.publish(source.grab())

    def borrow(self) -> FrameLease | None:
        """Borrow the oldest ready frame, or ``None`` when the ring is empty."""

        if not self._ready:
            return None

        slot_index = self._ready.pop(0)
        slot = self._slots[slot_index]
        slot.ready = False
        slot.leased = True
        self._borrowed_frames += 1
        return FrameLease(
            ring=self,
            slot_index=slot_index,
            generation=slot.generation,
            frame_id=_require_int(slot.frame_id, "frame_id"),
            timestamp_s=_require_float(slot.timestamp_s, "timestamp_s"),
            width=_require_int(slot.width, "width"),
            height=_require_int(slot.height, "height"),
            pixel_format=_require_str(slot.pixel_format, "pixel_format"),
            payload=memoryview(slot.payload)[: slot.payload_size],
            source_id=_require_str(slot.source_id, "source_id"),
            bus=_require_bus(slot.bus),
            metadata=slot.metadata,
        )

    def stats(self) -> FrameRingStats:
        """Return immutable counters and current ring occupancy."""

        return FrameRingStats(
            capacity=self.capacity,
            slot_size_bytes=self.slot_size_bytes,
            published_frames=self._published_frames,
            borrowed_frames=self._borrowed_frames,
            dropped_frames=self._dropped_frames,
            lag_events=self._lag_events,
            ready_frames=len(self._ready),
            leased_frames=sum(1 for slot in self._slots if slot.leased),
        )

    def _next_writable_slot(self) -> int | None:
        for offset in range(self.capacity):
            slot_index = (self._write_cursor + offset) % self.capacity
            slot = self._slots[slot_index]
            if not slot.ready and not slot.leased:
                return slot_index

        if not self._ready:
            return None

        self._dropped_frames += 1
        self._lag_events += 1
        slot_index = self._ready.pop(0)
        self._slots[slot_index].clear()
        return slot_index

    def _release(self, slot_index: int, generation: int) -> None:
        slot = self._slots[slot_index]
        if slot.generation != generation or not slot.leased:
            return
        slot.clear()


def _require_int(value: int | None, name: str) -> int:
    if value is None:
        raise RuntimeError(f"slot missing {name}")
    return value


def _require_float(value: float | None, name: str) -> float:
    if value is None:
        raise RuntimeError(f"slot missing {name}")
    return value


def _require_str(value: str | None, name: str) -> str:
    if value is None:
        raise RuntimeError(f"slot missing {name}")
    return value


def _require_bus(value: CameraBus | None) -> CameraBus:
    if value is None:
        raise RuntimeError("slot missing bus")
    return value


__all__ = [
    "FrameLease",
    "FrameRingBuffer",
    "FrameRingStats",
]
