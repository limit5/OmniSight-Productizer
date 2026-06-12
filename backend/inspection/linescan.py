"""C8.A5 line-scan strip stitching stub for inspection acquisition.

This module models the host-side contract only: line-scan cameras produce
ordered strips, and this stub assembles a bounded ring of strips into synthetic
frames. Real transport, encoder timing, and camera exposure timing remain
hardware follow-ups.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


Number = int | float
PixelRow = Sequence[Number]
PixelGrid = Sequence[PixelRow]


@dataclass(frozen=True)
class LineScanStrip:
    """One ordered line-scan strip emitted by a camera-shaped source."""

    sequence: int
    image: PixelGrid
    captured_at_s: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Validate the strip is non-empty and rectangular."""

        if self.sequence < 0:
            raise ValueError("sequence must be >= 0")
        width = _grid_width(self.image)
        if width <= 0:
            raise ValueError("strip image must include at least one pixel")

    @property
    def width(self) -> int:
        """Return the strip width in pixels."""

        return _grid_width(self.image)

    @property
    def height(self) -> int:
        """Return the strip height in rows."""

        return len(self.image)


@dataclass(frozen=True)
class LineScanFrame:
    """Frame assembled from a contiguous window of line-scan strips."""

    frame_id: int
    start_sequence: int
    end_sequence: int
    image: list[list[float]]
    strips: tuple[LineScanStrip, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def width(self) -> int:
        """Return the assembled frame width in pixels."""

        return len(self.image[0]) if self.image else 0

    @property
    def height(self) -> int:
        """Return the assembled frame height in rows."""

        return len(self.image)


class LineScanStripSource(ABC):
    """Interface for future CameraSource-compatible line-scan strip producers."""

    @abstractmethod
    def next_strip(self) -> LineScanStrip | None:
        """Return the next strip, or ``None`` when no strip is ready."""


@runtime_checkable
class CameraSourceLike(Protocol):
    """Optional structural shape for future camera sources.

    The real CameraSource ABC is intentionally not imported here so this C8.A5
    stub stays independent of the acquisition implementation that lands later.
    """

    def next_strip(self) -> LineScanStrip | None:
        """Return the next line-scan strip when available."""


class SyntheticLineScanSource(LineScanStripSource):
    """Deterministic strip source for host tests and simulator plumbing."""

    def __init__(self, strips: Iterable[LineScanStrip]) -> None:
        self._strips = deque(strips)

    def next_strip(self) -> LineScanStrip | None:
        if not self._strips:
            return None
        return self._strips.popleft()


class LineScanStitcher:
    """Assemble a bounded ring of contiguous line-scan strips into frames."""

    def __init__(
        self,
        *,
        strips_per_frame: int,
        ring_capacity: int | None = None,
        frame_stride_strips: int | None = None,
    ) -> None:
        if strips_per_frame <= 0:
            raise ValueError("strips_per_frame must be positive")
        stride = (
            strips_per_frame
            if frame_stride_strips is None
            else frame_stride_strips
        )
        if stride <= 0:
            raise ValueError("frame_stride_strips must be positive")

        capacity = ring_capacity if ring_capacity is not None else strips_per_frame
        if capacity < strips_per_frame:
            raise ValueError("ring_capacity must be >= strips_per_frame")

        self._strips_per_frame = strips_per_frame
        self._frame_stride_strips = stride
        self._ring: deque[LineScanStrip] = deque(maxlen=capacity)
        self._next_frame_id = 1
        self._next_frame_start_sequence: int | None = None
        self._last_sequence: int | None = None
        self._strip_width: int | None = None

    @property
    def buffered_count(self) -> int:
        """Return the number of strips currently retained in the ring."""

        return len(self._ring)

    def buffered_sequences(self) -> list[int]:
        """Return retained strip sequence numbers from oldest to newest."""

        return [strip.sequence for strip in self._ring]

    def append(self, strip: LineScanStrip) -> LineScanFrame | None:
        """Append one strip and return a frame when a full window is ready."""

        strip.validate()
        self._validate_sequence(strip)
        self._validate_width(strip)

        self._ring.append(strip)
        if self._next_frame_start_sequence is None:
            self._next_frame_start_sequence = strip.sequence

        return self._try_build_frame()

    def drain_source(self, source: CameraSourceLike) -> list[LineScanFrame]:
        """Read ready strips from ``source`` and return all completed frames."""

        frames: list[LineScanFrame] = []
        while True:
            strip = source.next_strip()
            if strip is None:
                return frames
            frame = self.append(strip)
            if frame is not None:
                frames.append(frame)

    def _try_build_frame(self) -> LineScanFrame | None:
        if self._next_frame_start_sequence is None:
            return None

        start = self._next_frame_start_sequence
        end = start + self._strips_per_frame - 1
        strips_by_sequence = {strip.sequence: strip for strip in self._ring}
        try:
            frame_strips = tuple(
                strips_by_sequence[sequence] for sequence in range(start, end + 1)
            )
        except KeyError:
            return None

        image: list[list[float]] = []
        metadata: dict[str, Any] = {"strip_count": len(frame_strips)}
        for strip in frame_strips:
            image.extend([float(pixel) for pixel in row] for row in strip.image)

        frame = LineScanFrame(
            frame_id=self._next_frame_id,
            start_sequence=start,
            end_sequence=end,
            image=image,
            strips=frame_strips,
            metadata=metadata,
        )
        self._next_frame_id += 1
        self._next_frame_start_sequence += self._frame_stride_strips
        return frame

    def _validate_sequence(self, strip: LineScanStrip) -> None:
        if (
            self._last_sequence is not None
            and strip.sequence != self._last_sequence + 1
        ):
            raise ValueError("line-scan strips must be contiguous")
        self._last_sequence = strip.sequence

    def _validate_width(self, strip: LineScanStrip) -> None:
        if self._strip_width is None:
            self._strip_width = strip.width
            return
        if strip.width != self._strip_width:
            raise ValueError("line-scan strip width changed")


def _grid_width(image: PixelGrid) -> int:
    if not image:
        return 0

    width = len(image[0])
    for row in image:
        if len(row) != width:
            raise ValueError("strip image rows must have consistent width")
    return width


__all__ = [
    "CameraSourceLike",
    "LineScanFrame",
    "LineScanStitcher",
    "LineScanStrip",
    "LineScanStripSource",
    "Number",
    "PixelGrid",
    "PixelRow",
    "SyntheticLineScanSource",
]
