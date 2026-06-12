"""C8.A2 camera-source abstraction for inspection acquisition.

The concrete bindings are intentionally CI-safe: V4L2/MIPI is a thin wrapper
around injectable frame capture, and GigE Vision is a protocol-shaped simulator
client rather than a real GVCP/GVSP implementation.
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Union


ClockFn = Callable[[], float]
CaptureFn = Callable[[str, "CameraConfig", int], Union[bytes, "CameraFrame"]]


class CameraBus(str, Enum):
    """Supported acquisition buses for the inspection pipeline."""

    v4l2_mipi = "v4l2_mipi"
    gige_vision_sim = "gige_vision_sim"
    fixture = "fixture"


class TriggerMode(str, Enum):
    """Camera trigger modes surfaced to line-acquisition logic."""

    free_run = "free_run"
    software = "software"
    hardware = "hardware"


@dataclass(frozen=True)
class CameraConfig:
    """Normalised camera configuration shared by source implementations."""

    width: int
    height: int
    pixel_format: str = "MONO8"
    fps: float = 30.0
    exposure_us: int | None = None
    gain_db: float | None = None
    trigger_mode: TriggerMode = TriggerMode.free_run
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.width <= 0:
            raise ValueError("width must be > 0")
        if self.height <= 0:
            raise ValueError("height must be > 0")
        if not self.pixel_format:
            raise ValueError("pixel_format must be set")
        if self.fps <= 0:
            raise ValueError("fps must be > 0")
        if self.exposure_us is not None and self.exposure_us <= 0:
            raise ValueError("exposure_us must be > 0")


@dataclass(frozen=True)
class CameraFrame:
    """One frame returned by an inspection camera source."""

    frame_id: int
    timestamp_s: float
    width: int
    height: int
    pixel_format: str
    payload: bytes
    source_id: str
    bus: CameraBus
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def size_bytes(self) -> int:
        return len(self.payload)


class CameraSource(ABC):
    """Interface for inspection camera acquisition sources."""

    @abstractmethod
    def open(self) -> None:
        """Open the camera transport."""

    @abstractmethod
    def configure(self, config: CameraConfig) -> None:
        """Apply a normalised camera configuration."""

    @abstractmethod
    def set_trigger_mode(self, mode: TriggerMode) -> None:
        """Set the camera trigger mode."""

    @abstractmethod
    def grab(self) -> CameraFrame:
        """Grab and return one frame."""


class FixtureFrameGenerator:
    """Deterministic frame generator used by simulator and CI tests."""

    def __init__(
        self,
        *,
        clock: ClockFn | None = None,
        source_id: str = "fixture",
    ) -> None:
        self._clock = clock or time.monotonic
        self.source_id = source_id

    def make_frame(
        self,
        frame_id: int,
        config: CameraConfig,
        *,
        bus: CameraBus = CameraBus.fixture,
        metadata: Mapping[str, Any] | None = None,
    ) -> CameraFrame:
        config.validate()
        payload = self._payload(frame_id, config)
        merged_metadata = dict(config.metadata)
        merged_metadata.update(metadata or {})
        return CameraFrame(
            frame_id=frame_id,
            timestamp_s=self._clock(),
            width=config.width,
            height=config.height,
            pixel_format=config.pixel_format,
            payload=payload,
            source_id=self.source_id,
            bus=bus,
            metadata=merged_metadata,
        )

    def _payload(self, frame_id: int, config: CameraConfig) -> bytes:
        bytes_per_pixel = _bytes_per_pixel(config.pixel_format)
        size = config.width * config.height * bytes_per_pixel
        return bytes(((frame_id + offset) % 256 for offset in range(size)))


class FixtureCameraSource(CameraSource):
    """In-process camera source backed by the deterministic frame generator."""

    bus = CameraBus.fixture

    def __init__(
        self,
        *,
        source_id: str = "fixture-camera",
        generator: FixtureFrameGenerator | None = None,
    ) -> None:
        self.source_id = source_id
        self._generator = generator or FixtureFrameGenerator(source_id=source_id)
        self._is_open = False
        self._config: CameraConfig | None = None
        self._next_frame_id = 1

    def open(self) -> None:
        self._is_open = True

    def configure(self, config: CameraConfig) -> None:
        config.validate()
        self._config = config

    def set_trigger_mode(self, mode: TriggerMode) -> None:
        self._require_config()
        self._config = _replace_trigger_mode(self._config, mode)

    def grab(self) -> CameraFrame:
        self._require_ready()
        frame = self._generator.make_frame(
            self._next_frame_id,
            self._config,
            bus=self.bus,
        )
        self._next_frame_id += 1
        return frame

    def _require_config(self) -> None:
        if self._config is None:
            raise RuntimeError("camera source must be configured before use")

    def _require_ready(self) -> None:
        if not self._is_open:
            raise RuntimeError("camera source must be opened before grab")
        self._require_config()


class V4L2CameraSource(CameraSource):
    """V4L2/MIPI camera binding shaped after the Phase-0 host conventions."""

    bus = CameraBus.v4l2_mipi

    def __init__(
        self,
        *,
        device_path: str = "/dev/video0",
        source_id: str | None = None,
        capture_frame: CaptureFn | None = None,
        clock: ClockFn | None = None,
    ) -> None:
        self.device_path = device_path
        self.source_id = source_id or device_path
        self._capture_frame = capture_frame
        self._clock = clock or time.monotonic
        self._is_open = False
        self._config: CameraConfig | None = None
        self._next_frame_id = 1

    def open(self) -> None:
        if self._capture_frame is None and not os.path.exists(self.device_path):
            raise FileNotFoundError(self.device_path)
        self._is_open = True

    def configure(self, config: CameraConfig) -> None:
        config.validate()
        self._config = config

    def set_trigger_mode(self, mode: TriggerMode) -> None:
        self._require_config()
        self._config = _replace_trigger_mode(self._config, mode)

    def grab(self) -> CameraFrame:
        self._require_ready()
        if self._capture_frame is None:
            raise RuntimeError("no V4L2 capture backend configured")

        frame_id = self._next_frame_id
        captured = self._capture_frame(self.device_path, self._config, frame_id)
        self._next_frame_id += 1
        if isinstance(captured, CameraFrame):
            return captured
        return CameraFrame(
            frame_id=frame_id,
            timestamp_s=self._clock(),
            width=self._config.width,
            height=self._config.height,
            pixel_format=self._config.pixel_format,
            payload=captured,
            source_id=self.source_id,
            bus=self.bus,
            metadata=dict(self._config.metadata),
        )

    def _require_config(self) -> None:
        if self._config is None:
            raise RuntimeError("camera source must be configured before use")

    def _require_ready(self) -> None:
        if not self._is_open:
            raise RuntimeError("camera source must be opened before grab")
        self._require_config()


class GigEVisionSimulatorClient(CameraSource):
    """Protocol-shaped GigE Vision simulator client for CI and host simulation."""

    bus = CameraBus.gige_vision_sim

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 3956,
        camera_id: str = "gige-sim-0",
        generator: FixtureFrameGenerator | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.camera_id = camera_id
        self._generator = generator or FixtureFrameGenerator(source_id=camera_id)
        self.command_log: list[dict[str, Any]] = []
        self._is_open = False
        self._config: CameraConfig | None = None
        self._next_frame_id = 1
        self._pending_triggers = 0

    def discover(self) -> dict[str, Any]:
        reply = self._command("DISCOVER")
        reply.update(
            {
                "camera_id": self.camera_id,
                "model": "OmniSight-GigE-Sim",
                "protocol": "gige_vision_sim",
            }
        )
        return reply

    def open(self) -> None:
        self._command("OPEN")
        self._is_open = True

    def configure(self, config: CameraConfig) -> None:
        config.validate()
        self._command(
            "CONFIGURE",
            width=config.width,
            height=config.height,
            pixel_format=config.pixel_format,
            fps=config.fps,
            trigger_mode=config.trigger_mode.value,
        )
        self._config = config

    def set_trigger_mode(self, mode: TriggerMode) -> None:
        self._require_config()
        self._command("SET_TRIGGER_MODE", trigger_mode=mode.value)
        self._config = _replace_trigger_mode(self._config, mode)
        self._pending_triggers = 0

    def software_trigger(self) -> None:
        self._require_ready()
        if self._config.trigger_mode != TriggerMode.software:
            raise RuntimeError("software trigger requires software trigger mode")
        self._command("TRIGGER", trigger_mode=TriggerMode.software.value)
        self._pending_triggers += 1

    def hardware_trigger(self) -> None:
        self._require_ready()
        if self._config.trigger_mode != TriggerMode.hardware:
            raise RuntimeError("hardware trigger requires hardware trigger mode")
        self._command("TRIGGER", trigger_mode=TriggerMode.hardware.value)
        self._pending_triggers += 1

    def grab(self) -> CameraFrame:
        self._require_ready()
        if self._config.trigger_mode in {TriggerMode.software, TriggerMode.hardware}:
            if self._pending_triggers <= 0:
                raise RuntimeError("triggered camera requires a pending trigger before grab")
            self._pending_triggers -= 1

        self._command("GRAB", frame_id=self._next_frame_id)
        frame = self._generator.make_frame(
            self._next_frame_id,
            self._config,
            bus=self.bus,
            metadata={
                "camera_id": self.camera_id,
                "host": self.host,
                "port": self.port,
                "simulated": True,
            },
        )
        self._next_frame_id += 1
        return frame

    def _command(self, name: str, **fields: Any) -> dict[str, Any]:
        command = {
            "command": name,
            "host": self.host,
            "port": self.port,
            **fields,
        }
        self.command_log.append(command)
        return {"status": "ok", **command}

    def _require_config(self) -> None:
        if self._config is None:
            raise RuntimeError("camera source must be configured before use")

    def _require_ready(self) -> None:
        if not self._is_open:
            raise RuntimeError("camera source must be opened before grab")
        self._require_config()


def _bytes_per_pixel(pixel_format: str) -> int:
    normalised = pixel_format.upper()
    if normalised in {"RGB24", "BGR24"}:
        return 3
    if normalised in {"YUYV", "UYVY"}:
        return 2
    return 1


def _replace_trigger_mode(config: CameraConfig, mode: TriggerMode) -> CameraConfig:
    return CameraConfig(
        width=config.width,
        height=config.height,
        pixel_format=config.pixel_format,
        fps=config.fps,
        exposure_us=config.exposure_us,
        gain_db=config.gain_db,
        trigger_mode=mode,
        metadata=dict(config.metadata),
    )


__all__ = [
    "CameraBus",
    "CameraConfig",
    "CameraFrame",
    "CameraSource",
    "CaptureFn",
    "ClockFn",
    "FixtureCameraSource",
    "FixtureFrameGenerator",
    "GigEVisionSimulatorClient",
    "TriggerMode",
    "V4L2CameraSource",
]
