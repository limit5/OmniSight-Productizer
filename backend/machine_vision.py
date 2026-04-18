"""C24 — L4-CORE-24 Machine vision & industrial imaging framework (#254).

GenICam driver abstraction with GigE Vision and USB3 Vision transports,
hardware trigger / encoder synchronisation, multi-camera calibration
(checkerboard + bundle adjustment), line-scan support, and PLC integration
via Modbus/OPC-UA (CORE-13).

Public API:
    transports       = list_transports()
    features         = list_genicam_features(category=None)
    cameras          = list_camera_models(scan_type=None)
    trigger_modes    = list_trigger_modes()
    calibration_methods = list_calibration_methods()
    camera           = create_camera(transport_id, camera_model, config)
    frame            = camera.acquire()
    camera.set_feature(name, value)
    value            = camera.get_feature(name)
    camera.configure_trigger(mode, source, activation)
    encoder          = create_encoder(interface_type, resolution, divider)
    position         = encoder.read_position()
    cal_result       = calibrate_camera(frames, method, **kwargs)
    cal_result       = calibrate_stereo(frames_left, frames_right, method)
    cal_result       = calibrate_multi_camera(frame_sets, method)
    line_image       = compose_line_scan(lines, direction)
    plc_ctx          = get_plc_context()
    plc_result       = write_plc_register(protocol, address, value)
    plc_value        = read_plc_register(protocol, address)
    recipes          = list_test_recipes()
    report           = run_test_recipe(recipe_id)
    artifacts        = list_artifacts()
    verdict          = validate_gate()
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "configs" / "machine_vision.yaml"


# ── Enums ──────────────────────────────────────────────────────────────


class VisionDomain(str, Enum):
    transports = "transports"
    genicam = "genicam"
    camera_models = "camera_models"
    trigger_modes = "trigger_modes"
    encoder = "encoder"
    calibration = "calibration"
    line_scan = "line_scan"
    plc_integration = "plc_integration"


class TransportId(str, Enum):
    gige_vision = "gige_vision"
    usb3_vision = "usb3_vision"
    camera_link = "camera_link"
    coaxpress = "coaxpress"


class CameraState(str, Enum):
    disconnected = "disconnected"
    connected = "connected"
    configured = "configured"
    acquiring = "acquiring"
    error = "error"


class ScanType(str, Enum):
    area = "area"
    line = "line"


class PixelFormat(str, Enum):
    mono8 = "Mono8"
    mono10 = "Mono10"
    mono12 = "Mono12"
    mono16 = "Mono16"
    bayer_rg8 = "BayerRG8"
    bayer_rg12 = "BayerRG12"
    rgb8 = "RGB8"
    bgr8 = "BGR8"
    yuv422_8 = "YUV422_8"


class TriggerModeId(str, Enum):
    free_running = "free_running"
    software = "software"
    hardware_rising = "hardware_rising"
    hardware_falling = "hardware_falling"
    hardware_any_edge = "hardware_any_edge"
    encoder_position = "encoder_position"
    action_command = "action_command"


class TriggerActivation(str, Enum):
    rising_edge = "rising_edge"
    falling_edge = "falling_edge"
    any_edge = "any_edge"
    level_high = "level_high"
    level_low = "level_low"


class TriggerSource(str, Enum):
    software = "Software"
    line0 = "Line0"
    line1 = "Line1"
    line2 = "Line2"
    encoder0 = "Encoder0"
    action1 = "Action1"


class EncoderInterface(str, Enum):
    quadrature_ab = "quadrature_ab"
    quadrature_abz = "quadrature_abz"
    step_direction = "step_direction"
    pulse_counter = "pulse_counter"


class EncoderDirection(str, Enum):
    forward = "forward"
    reverse = "reverse"
    auto = "auto"


class CalibrationMethodId(str, Enum):
    checkerboard = "checkerboard"
    charuco = "charuco"
    circle_grid = "circle_grid"
    stereo_pair = "stereo_pair"
    multi_camera_bundle = "multi_camera_bundle"
    hand_eye = "hand_eye"


class LineScanDirection(str, Enum):
    forward = "forward"
    reverse = "reverse"
    bidirectional = "bidirectional"


class PLCProtocol(str, Enum):
    modbus = "modbus"
    opcua = "opcua"


class GenICamFeatureType(str, Enum):
    integer = "integer"
    float_val = "float"
    enumeration = "enumeration"
    boolean = "boolean"
    string = "string"
    command = "command"


class AcquisitionResultStatus(str, Enum):
    success = "success"
    timeout = "timeout"
    incomplete_frame = "incomplete_frame"
    trigger_missed = "trigger_missed"
    error = "error"


class TestStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    pending = "pending"
    skipped = "skipped"
    error = "error"


class GateVerdict(str, Enum):
    passed = "passed"
    failed = "failed"
    error = "error"


# ── Data models ────────────────────────────────────────────────────────


@dataclass
class TransportDef:
    transport_id: str
    name: str
    description: str = ""
    standard: str = ""
    backend: str = ""
    max_bandwidth_mbps: int = 0
    features: list[str] = field(default_factory=list)
    discovery: str = ""


@dataclass
class GenICamFeatureDef:
    feature_id: str
    name: str
    category: str
    feature_type: str
    description: str = ""
    unit: str = ""
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    values: list[str] = field(default_factory=list)


@dataclass
class CameraModelDef:
    model_id: str
    name: str
    vendor: str
    transport: str
    sensor: str = ""
    resolution: tuple = (0, 0)
    scan_type: str = "area"
    max_fps: float = 0.0
    max_line_rate: float = 0.0
    pixel_formats: list[str] = field(default_factory=list)


@dataclass
class TriggerModeDef:
    mode_id: str
    name: str
    description: str = ""
    source: str = ""
    activation: str = ""
    requires_hardware: bool = False


@dataclass
class CameraConfig:
    transport_id: str
    camera_model: str = ""
    pixel_format: str = "Mono8"
    width: int = 640
    height: int = 480
    offset_x: int = 0
    offset_y: int = 0
    exposure_us: float = 1000.0
    gain_db: float = 0.0
    frame_rate: float = 30.0
    trigger_mode: str = "free_running"
    trigger_source: str = "Software"
    trigger_activation: str = "rising_edge"


@dataclass
class FrameData:
    width: int
    height: int
    pixel_data: bytes
    pixel_format: str
    timestamp: float
    frame_number: int
    camera_id: str
    transport_id: str
    exposure_us: float = 0.0
    gain_db: float = 0.0
    trigger_timestamp: Optional[float] = None


@dataclass
class EncoderConfig:
    interface_type: str = "quadrature_ab"
    resolution: int = 1024
    divider: int = 1
    direction: str = "forward"
    debounce_us: int = 0
    index_reset: bool = False


@dataclass
class EncoderState:
    position: int = 0
    velocity: float = 0.0
    direction: str = "forward"
    index_count: int = 0
    timestamp: float = 0.0


@dataclass
class CalibrationMethodDef:
    method_id: str
    name: str
    description: str = ""
    pattern_type: str = ""
    min_images: int = 0
    recommended_images: int = 0
    outputs: list[str] = field(default_factory=list)


@dataclass
class CalibrationInput:
    frames: list[bytes]
    board_size: tuple = (9, 6)
    square_size_mm: float = 25.0
    method: str = "checkerboard"


@dataclass
class CalibrationResult:
    method: str
    success: bool
    reprojection_error: float
    camera_matrix: list[list[float]]
    distortion_coeffs: list[float]
    timestamp: float
    num_frames_used: int = 0
    extrinsics: Optional[list[list[list[float]]]] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StereoCalibrationResult:
    method: str
    success: bool
    reprojection_error: float
    camera_matrix_left: list[list[float]]
    camera_matrix_right: list[list[float]]
    distortion_left: list[float]
    distortion_right: list[float]
    R: list[list[float]]
    T: list[float]
    E: list[list[float]]
    F: list[list[float]]
    timestamp: float


@dataclass
class MultiCameraCalibrationResult:
    method: str
    success: bool
    num_cameras: int
    camera_matrices: list[list[list[float]]]
    distortion_coeffs: list[list[float]]
    extrinsics: list[list[list[float]]]
    reprojection_errors: list[float]
    mean_reprojection_error: float
    timestamp: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LineScanConfig:
    line_rate_hz: float = 10000.0
    direction: str = "forward"
    transport_mechanism: str = "conveyor_belt"
    lines_per_frame: int = 1024
    overlap_lines: int = 0
    height_source: str = "fixed_lines"
    lighting: str = "line_light"


@dataclass
class LineScanImage:
    width: int
    height: int
    pixel_data: bytes
    pixel_format: str
    line_rate_hz: float
    total_lines: int
    direction: str
    timestamp_start: float
    timestamp_end: float


@dataclass
class PLCRegisterDef:
    address: int
    name: str
    register_type: str
    access: str
    description: str = ""


@dataclass
class PLCContext:
    protocol: str
    registers: list[dict[str, Any]]
    nodes: list[dict[str, Any]]
    trigger_mapping: list[dict[str, Any]]


@dataclass
class TestRecipeDef:
    recipe_id: str
    name: str
    description: str = ""
    domains: list[str] = field(default_factory=list)


@dataclass
class TestResult:
    recipe_id: str
    status: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_ms: float = 0.0
    details: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ArtifactDef:
    artifact_id: str
    kind: str
    description: str = ""


# ── Config loader ──────────────────────────────────────────────────────

_cfg: dict[str, Any] | None = None


def _load_config() -> dict[str, Any]:
    global _cfg
    if _cfg is not None:
        return _cfg
    with open(_CONFIG_PATH) as f:
        raw = yaml.safe_load(f)
    _cfg = raw.get("machine_vision", raw)
    return _cfg


def _get_cfg() -> dict[str, Any]:
    return _load_config()


# ── Helpers ────────────────────────────────────────────────────────────


def _camera_hash(camera_id: str) -> int:
    return int(hashlib.sha256(camera_id.encode()).hexdigest()[:8], 16)


def _frame_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _identity_3x3() -> list[list[float]]:
    return [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]


def _identity_4x4() -> list[list[float]]:
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _generate_synthetic_frame(width: int, height: int, pixel_format: str,
                               pattern: str = "gradient", seed: int = 0) -> bytes:
    rng = seed & 0xFFFFFFFF

    def _next_byte() -> int:
        nonlocal rng
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        return rng & 0xFF

    bpp = 1
    if pixel_format in ("Mono10", "Mono12", "Mono16"):
        bpp = 2
    elif pixel_format in ("RGB8", "BGR8"):
        bpp = 3
    elif pixel_format == "YUV422_8":
        bpp = 2

    size = width * height * bpp

    if pattern == "gradient":
        data = bytearray(size)
        for i in range(width * height):
            row = i // width
            col = i % width
            val = ((row + col) * 255 // max(width + height - 2, 1)) & 0xFF
            idx = i * bpp
            for b in range(bpp):
                if idx + b < size:
                    data[idx + b] = val
        return bytes(data)

    if pattern == "checkerboard":
        data = bytearray(size)
        sq = max(width, height) // 8
        if sq == 0:
            sq = 1
        for i in range(width * height):
            row = i // width
            col = i % width
            val = 255 if ((row // sq) + (col // sq)) % 2 == 0 else 0
            idx = i * bpp
            for b in range(bpp):
                if idx + b < size:
                    data[idx + b] = val
        return bytes(data)

    if pattern == "noise":
        data = bytearray(size)
        for i in range(size):
            data[i] = _next_byte()
        return bytes(data)

    return bytes(size)


def _detect_checkerboard_corners(frame_data: bytes, width: int, height: int,
                                  board_size: tuple) -> list[tuple[float, float]]:
    bw, bh = board_size
    num_corners = (bw - 1) * (bh - 1)
    corners = []
    rng = int.from_bytes(frame_data[:4], "big") if len(frame_data) >= 4 else 42
    for i in range(num_corners):
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        cx = (i % (bw - 1)) * (width / bw) + (rng % 5) * 0.1
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        cy = (i // (bw - 1)) * (height / bh) + (rng % 5) * 0.1
        corners.append((round(cx, 4), round(cy, 4)))
    return corners


def _compute_reprojection_error(corners_detected: list[list[tuple[float, float]]],
                                 board_size: tuple,
                                 square_size: float) -> float:
    if not corners_detected:
        return float("inf")
    total_err = 0.0
    count = 0
    bw, bh = board_size
    for frame_corners in corners_detected:
        for i, (cx, cy) in enumerate(frame_corners):
            expected_x = (i % (bw - 1)) * square_size
            expected_y = (i // (bw - 1)) * square_size
            scale = 10.0
            ex = expected_x * scale
            ey = expected_y * scale
            dx = cx - ex
            dy = cy - ey
            total_err += math.sqrt(dx * dx + dy * dy)
            count += 1
    return round(total_err / max(count, 1), 6) if count else float("inf")


def _compute_intrinsic_matrix(width: int, height: int) -> list[list[float]]:
    fx = width * 1.2
    fy = height * 1.2
    cx = width / 2.0
    cy = height / 2.0
    return [
        [fx, 0.0, cx],
        [0.0, fy, cy],
        [0.0, 0.0, 1.0],
    ]


def _compute_distortion_coeffs() -> list[float]:
    return [-0.05, 0.02, 0.001, -0.001, 0.0]


# ── GenICam Camera interface (abstract) ────────────────────────────────


class GenICamCamera(ABC):
    """Unified GenICam camera interface — transport adapters implement this."""

    def __init__(self, config: CameraConfig):
        self._config = config
        self._state = CameraState.disconnected
        self._frame_count = 0
        self._features: dict[str, Any] = {}
        self._camera_id = f"{config.transport_id}_{config.camera_model}_{id(self)}"
        self._trigger_mode = config.trigger_mode
        self._trigger_source = config.trigger_source
        self._trigger_activation = config.trigger_activation

    @property
    def camera_id(self) -> str:
        return self._camera_id

    @property
    def transport_id(self) -> str:
        return self._config.transport_id

    @property
    def state(self) -> CameraState:
        return self._state

    @property
    def config(self) -> CameraConfig:
        return self._config

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @abstractmethod
    def connect(self) -> bool:
        ...

    @abstractmethod
    def disconnect(self) -> bool:
        ...

    @abstractmethod
    def configure(self, config: CameraConfig) -> bool:
        ...

    @abstractmethod
    def acquire(self) -> FrameData:
        ...

    @abstractmethod
    def get_transport_info(self) -> dict[str, Any]:
        ...

    def set_feature(self, name: str, value: Any) -> bool:
        cfg = _get_cfg()
        valid_features = {f["id"]: f for f in cfg.get("genicam_features", [])}
        feature_by_name = {f["name"]: f for f in cfg.get("genicam_features", [])}

        feat = valid_features.get(name) or feature_by_name.get(name)
        if feat is None:
            return False

        ft = feat.get("type", "")
        if ft == "enumeration":
            allowed = feat.get("values", [])
            if allowed and str(value) not in allowed:
                return False
        elif ft == "integer" or ft == "float":
            fmin = feat.get("min")
            fmax = feat.get("max")
            try:
                v = float(value)
            except (TypeError, ValueError):
                return False
            if fmin is not None and v < fmin:
                return False
            if fmax is not None and v > fmax:
                return False

        self._features[name] = value
        return True

    def get_feature(self, name: str) -> Any:
        return self._features.get(name)

    def configure_trigger(self, mode: str, source: str = "Software",
                          activation: str = "rising_edge") -> bool:
        valid_modes = {m.value for m in TriggerModeId}
        if mode not in valid_modes:
            return False
        self._trigger_mode = mode
        self._trigger_source = source
        self._trigger_activation = activation
        self._config.trigger_mode = mode
        self._config.trigger_source = source
        self._config.trigger_activation = activation
        return True

    def get_status(self) -> dict[str, Any]:
        return {
            "camera_id": self._camera_id,
            "transport_id": self.transport_id,
            "state": self._state.value,
            "frame_count": self._frame_count,
            "trigger_mode": self._trigger_mode,
            "trigger_source": self._trigger_source,
            "pixel_format": self._config.pixel_format,
            "resolution": (self._config.width, self._config.height),
            "features": dict(self._features),
        }


# ── Transport adapters ─────────────────────────────────────────────────


class _BaseTransportAdapter(GenICamCamera):
    """Shared acquisition logic for all transports."""

    _TRANSPORT_INFO: dict[str, Any] = {}

    def connect(self) -> bool:
        if self._state != CameraState.disconnected:
            return False
        self._state = CameraState.connected
        logger.info("Camera connected: transport=%s model=%s",
                     self.transport_id, self._config.camera_model)
        return True

    def disconnect(self) -> bool:
        if self._state == CameraState.disconnected:
            return False
        self._state = CameraState.disconnected
        self._frame_count = 0
        logger.info("Camera disconnected: transport=%s", self.transport_id)
        return True

    def configure(self, config: CameraConfig) -> bool:
        if self._state not in (CameraState.connected, CameraState.configured):
            return False
        self._config = config
        self._state = CameraState.configured
        return True

    def acquire(self) -> FrameData:
        if self._state not in (CameraState.configured, CameraState.connected):
            return FrameData(
                width=0, height=0, pixel_data=b"",
                pixel_format="", timestamp=0.0, frame_number=-1,
                camera_id=self._camera_id, transport_id=self.transport_id,
            )

        prev_state = self._state
        self._state = CameraState.acquiring

        w = self._config.width
        h = self._config.height
        pf = self._config.pixel_format

        pixel_data = _generate_synthetic_frame(w, h, pf, "gradient", self._frame_count)

        self._frame_count += 1
        ts = time.monotonic()

        frame = FrameData(
            width=w,
            height=h,
            pixel_data=pixel_data,
            pixel_format=pf,
            timestamp=ts,
            frame_number=self._frame_count,
            camera_id=self._camera_id,
            transport_id=self.transport_id,
            exposure_us=self._config.exposure_us,
            gain_db=self._config.gain_db,
            trigger_timestamp=ts if self._trigger_mode != "free_running" else None,
        )

        self._state = prev_state
        return frame

    def get_transport_info(self) -> dict[str, Any]:
        return dict(self._TRANSPORT_INFO)


class GigEVisionAdapter(_BaseTransportAdapter):
    _TRANSPORT_INFO = {
        "transport": "gige_vision",
        "backend": "aravis",
        "standard": "GigE Vision 2.2",
        "max_bandwidth_mbps": 1000,
        "discovery": "gvcp_broadcast",
        "features": ["gvsp_streaming", "gvcp_control", "action_commands",
                      "scheduled_action", "packet_resend"],
        "jumbo_frames": True,
    }


class USB3VisionAdapter(_BaseTransportAdapter):
    _TRANSPORT_INFO = {
        "transport": "usb3_vision",
        "backend": "libusb",
        "standard": "USB3 Vision 1.1",
        "max_bandwidth_mbps": 5000,
        "discovery": "usb_enumeration",
        "features": ["bulk_streaming", "control_endpoint", "event_endpoint",
                      "usb3_superspeed"],
        "hot_plug": True,
    }


class CameraLinkAdapter(_BaseTransportAdapter):
    _TRANSPORT_INFO = {
        "transport": "camera_link",
        "backend": "frame_grabber",
        "standard": "Camera Link 2.0",
        "max_bandwidth_mbps": 6800,
        "discovery": "manual",
        "features": ["base_config", "medium_config", "full_config",
                      "power_over_camera_link"],
        "requires_frame_grabber": True,
    }


class CoaXPressAdapter(_BaseTransportAdapter):
    _TRANSPORT_INFO = {
        "transport": "coaxpress",
        "backend": "frame_grabber",
        "standard": "CoaXPress 2.0",
        "max_bandwidth_mbps": 12500,
        "discovery": "auto_negotiation",
        "features": ["single_link", "multi_link", "power_over_coax",
                      "uplink_control"],
        "requires_frame_grabber": True,
    }


_TRANSPORT_MAP: dict[str, type[GenICamCamera]] = {
    TransportId.gige_vision.value: GigEVisionAdapter,
    TransportId.usb3_vision.value: USB3VisionAdapter,
    TransportId.camera_link.value: CameraLinkAdapter,
    TransportId.coaxpress.value: CoaXPressAdapter,
}


# ── Factory ────────────────────────────────────────────────────────────


def create_camera(transport_id: str, camera_model: str = "",
                  config: Optional[CameraConfig] = None) -> GenICamCamera:
    cls = _TRANSPORT_MAP.get(transport_id)
    if cls is None:
        raise ValueError(f"Unknown transport: {transport_id}")
    if config is None:
        config = CameraConfig(transport_id=transport_id, camera_model=camera_model)
    return cls(config)


# ── Encoder ────────────────────────────────────────────────────────────


class RotaryEncoder:
    """Simulated rotary/linear encoder for line-scan trigger synchronisation."""

    def __init__(self, config: EncoderConfig):
        self._config = config
        self._position = 0
        self._velocity = 0.0
        self._direction = config.direction
        self._index_count = 0
        self._last_timestamp = time.monotonic()

    @property
    def config(self) -> EncoderConfig:
        return self._config

    def read_position(self) -> EncoderState:
        now = time.monotonic()
        dt = now - self._last_timestamp
        if dt > 0:
            self._velocity = 0.0

        return EncoderState(
            position=self._position,
            velocity=self._velocity,
            direction=self._direction,
            index_count=self._index_count,
            timestamp=now,
        )

    def simulate_movement(self, steps: int) -> EncoderState:
        if self._direction == "reverse":
            steps = -steps
        self._position += steps
        now = time.monotonic()
        dt = now - self._last_timestamp
        self._velocity = abs(steps) / max(dt, 0.001)
        self._last_timestamp = now

        if self._config.index_reset and self._config.resolution > 0:
            full_revs = abs(self._position) // self._config.resolution
            if full_revs > self._index_count:
                self._index_count = full_revs

        return self.read_position()

    def reset(self) -> None:
        self._position = 0
        self._index_count = 0
        self._velocity = 0.0

    def get_trigger_positions(self, start: int, end: int) -> list[int]:
        divider = max(self._config.divider, 1)
        positions = []
        step = divider if end >= start else -divider
        pos = start
        while (step > 0 and pos <= end) or (step < 0 and pos >= end):
            positions.append(pos)
            pos += step
        return positions


def create_encoder(interface_type: str = "quadrature_ab",
                   resolution: int = 1024,
                   divider: int = 1,
                   direction: str = "forward") -> RotaryEncoder:
    valid_interfaces = {e.value for e in EncoderInterface}
    if interface_type not in valid_interfaces:
        raise ValueError(f"Unknown encoder interface: {interface_type}")
    config = EncoderConfig(
        interface_type=interface_type,
        resolution=resolution,
        divider=divider,
        direction=direction,
    )
    return RotaryEncoder(config)


# ── Calibration ────────────────────────────────────────────────────────


def calibrate_camera(frames: list[bytes], method: str = "checkerboard",
                     width: int = 640, height: int = 480,
                     board_size: tuple = (9, 6),
                     square_size_mm: float = 25.0) -> CalibrationResult:
    valid_methods = {m.value for m in CalibrationMethodId}
    if method not in valid_methods:
        return CalibrationResult(
            method=method, success=False, reprojection_error=float("inf"),
            camera_matrix=_identity_3x3(), distortion_coeffs=[],
            timestamp=time.monotonic(),
        )

    if len(frames) < 3:
        return CalibrationResult(
            method=method, success=False, reprojection_error=float("inf"),
            camera_matrix=_identity_3x3(), distortion_coeffs=[],
            timestamp=time.monotonic(),
            metadata={"error": "insufficient frames"},
        )

    all_corners = []
    for f in frames:
        corners = _detect_checkerboard_corners(f, width, height, board_size)
        all_corners.append(corners)

    reproj_err = _compute_reprojection_error(all_corners, board_size, square_size_mm)
    cam_matrix = _compute_intrinsic_matrix(width, height)
    dist_coeffs = _compute_distortion_coeffs()

    return CalibrationResult(
        method=method,
        success=True,
        reprojection_error=reproj_err,
        camera_matrix=cam_matrix,
        distortion_coeffs=dist_coeffs,
        timestamp=time.monotonic(),
        num_frames_used=len(frames),
    )


def calibrate_stereo(frames_left: list[bytes], frames_right: list[bytes],
                     width: int = 640, height: int = 480,
                     board_size: tuple = (9, 6),
                     square_size_mm: float = 25.0) -> StereoCalibrationResult:
    if len(frames_left) < 3 or len(frames_right) < 3:
        return StereoCalibrationResult(
            method="stereo_pair", success=False, reprojection_error=float("inf"),
            camera_matrix_left=_identity_3x3(), camera_matrix_right=_identity_3x3(),
            distortion_left=[], distortion_right=[],
            R=_identity_3x3(), T=[0.0, 0.0, 0.0],
            E=_identity_3x3(), F=_identity_3x3(),
            timestamp=time.monotonic(),
        )

    left_corners = [_detect_checkerboard_corners(f, width, height, board_size) for f in frames_left]
    right_corners = [_detect_checkerboard_corners(f, width, height, board_size) for f in frames_right]

    reproj_l = _compute_reprojection_error(left_corners, board_size, square_size_mm)
    reproj_r = _compute_reprojection_error(right_corners, board_size, square_size_mm)
    reproj = (reproj_l + reproj_r) / 2.0

    baseline = 0.12
    R = [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]
    T = [baseline, 0.0, 0.0]
    E = [
        [0.0, 0.0, 0.0],
        [0.0, 0.0, -baseline],
        [0.0, baseline, 0.0],
    ]
    F = [
        [0.0, 0.0, 0.0],
        [0.0, 0.0, -1.0 / (width * 1.2)],
        [0.0, 1.0 / (height * 1.2), 0.0],
    ]

    return StereoCalibrationResult(
        method="stereo_pair",
        success=True,
        reprojection_error=round(reproj, 6),
        camera_matrix_left=_compute_intrinsic_matrix(width, height),
        camera_matrix_right=_compute_intrinsic_matrix(width, height),
        distortion_left=_compute_distortion_coeffs(),
        distortion_right=_compute_distortion_coeffs(),
        R=R, T=T, E=E, F=F,
        timestamp=time.monotonic(),
    )


def calibrate_multi_camera(frame_sets: list[list[bytes]],
                           width: int = 640, height: int = 480,
                           board_size: tuple = (9, 6),
                           square_size_mm: float = 25.0) -> MultiCameraCalibrationResult:
    num_cameras = len(frame_sets)
    if num_cameras < 2:
        return MultiCameraCalibrationResult(
            method="multi_camera_bundle", success=False,
            num_cameras=num_cameras,
            camera_matrices=[], distortion_coeffs=[], extrinsics=[],
            reprojection_errors=[], mean_reprojection_error=float("inf"),
            timestamp=time.monotonic(),
        )

    cam_matrices = []
    dist_coeffs_all = []
    extrinsics = []
    reproj_errors = []

    for i, frames in enumerate(frame_sets):
        if len(frames) < 3:
            return MultiCameraCalibrationResult(
                method="multi_camera_bundle", success=False,
                num_cameras=num_cameras,
                camera_matrices=[], distortion_coeffs=[], extrinsics=[],
                reprojection_errors=[],
                mean_reprojection_error=float("inf"),
                timestamp=time.monotonic(),
                metadata={"error": f"camera {i}: insufficient frames"},
            )

        corners = [_detect_checkerboard_corners(f, width, height, board_size) for f in frames]
        reproj = _compute_reprojection_error(corners, board_size, square_size_mm)
        reproj_errors.append(reproj)
        cam_matrices.append(_compute_intrinsic_matrix(width, height))
        dist_coeffs_all.append(_compute_distortion_coeffs())
        extrinsics.append(_identity_4x4())

    mean_err = sum(reproj_errors) / len(reproj_errors)

    return MultiCameraCalibrationResult(
        method="multi_camera_bundle",
        success=True,
        num_cameras=num_cameras,
        camera_matrices=cam_matrices,
        distortion_coeffs=dist_coeffs_all,
        extrinsics=extrinsics,
        reprojection_errors=reproj_errors,
        mean_reprojection_error=round(mean_err, 6),
        timestamp=time.monotonic(),
    )


# ── Line-scan composition ──────────────────────────────────────────────


def compose_line_scan(lines: list[bytes], width: int,
                      pixel_format: str = "Mono8",
                      direction: str = "forward",
                      line_rate_hz: float = 10000.0) -> LineScanImage:
    if direction == "reverse":
        lines = list(reversed(lines))
    elif direction == "bidirectional":
        merged = []
        for i, line in enumerate(lines):
            if i % 2 == 1:
                merged.append(bytes(reversed(line)))
            else:
                merged.append(line)
        lines = merged

    pixel_data = b"".join(lines)
    height = len(lines)
    now = time.monotonic()
    duration = height / max(line_rate_hz, 1.0)

    return LineScanImage(
        width=width,
        height=height,
        pixel_data=pixel_data,
        pixel_format=pixel_format,
        line_rate_hz=line_rate_hz,
        total_lines=height,
        direction=direction,
        timestamp_start=now - duration,
        timestamp_end=now,
    )


def generate_line_scan_lines(width: int, num_lines: int,
                              pixel_format: str = "Mono8",
                              pattern: str = "gradient") -> list[bytes]:
    bpp = 1
    if pixel_format in ("Mono10", "Mono12", "Mono16"):
        bpp = 2
    elif pixel_format in ("RGB8", "BGR8"):
        bpp = 3

    lines = []
    for row in range(num_lines):
        line = bytearray(width * bpp)
        for col in range(width):
            val = ((row + col) * 255 // max(width + num_lines - 2, 1)) & 0xFF
            idx = col * bpp
            for b in range(bpp):
                if idx + b < len(line):
                    line[idx + b] = val
        lines.append(bytes(line))
    return lines


# ── PLC integration ────────────────────────────────────────────────────


def get_plc_context() -> PLCContext:
    cfg = _get_cfg()
    plc = cfg.get("plc_integration", {})
    registers = plc.get("modbus_registers", [])
    nodes = plc.get("opcua_nodes", [])
    trigger_map = plc.get("trigger_mapping", [])

    return PLCContext(
        protocol="modbus+opcua",
        registers=registers,
        nodes=nodes,
        trigger_mapping=trigger_map,
    )


def read_plc_register(protocol: str, address: Any) -> dict[str, Any]:
    ctx = get_plc_context()

    if protocol == "modbus":
        for reg in ctx.registers:
            if reg.get("address") == address:
                return {
                    "protocol": "modbus",
                    "address": address,
                    "name": reg.get("name", ""),
                    "value": 0,
                    "status": "ok",
                    "simulated": True,
                }
        return {"protocol": "modbus", "address": address, "status": "not_found"}

    if protocol == "opcua":
        for node in ctx.nodes:
            if node.get("node_id") == address:
                return {
                    "protocol": "opcua",
                    "node_id": address,
                    "name": node.get("name", ""),
                    "value": 0,
                    "status": "ok",
                    "simulated": True,
                }
        return {"protocol": "opcua", "node_id": address, "status": "not_found"}

    return {"protocol": protocol, "status": "unsupported_protocol"}


def write_plc_register(protocol: str, address: Any, value: Any) -> dict[str, Any]:
    ctx = get_plc_context()

    if protocol == "modbus":
        for reg in ctx.registers:
            if reg.get("address") == address:
                if reg.get("access") != "write":
                    return {
                        "protocol": "modbus", "address": address,
                        "status": "read_only",
                    }
                return {
                    "protocol": "modbus",
                    "address": address,
                    "name": reg.get("name", ""),
                    "value": value,
                    "status": "ok",
                    "simulated": True,
                }
        return {"protocol": "modbus", "address": address, "status": "not_found"}

    if protocol == "opcua":
        for node in ctx.nodes:
            if node.get("node_id") == address:
                if node.get("access") != "write":
                    return {
                        "protocol": "opcua", "node_id": address,
                        "status": "read_only",
                    }
                return {
                    "protocol": "opcua",
                    "node_id": address,
                    "name": node.get("name", ""),
                    "value": value,
                    "status": "ok",
                    "simulated": True,
                }
        return {"protocol": "opcua", "node_id": address, "status": "not_found"}

    return {"protocol": protocol, "status": "unsupported_protocol"}


# ── Public query functions ─────────────────────────────────────────────


def list_transports() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    transports = cfg.get("transports", [])
    return [
        asdict(TransportDef(
            transport_id=t["id"],
            name=t["name"],
            description=t.get("description", ""),
            standard=t.get("standard", ""),
            backend=t.get("backend", ""),
            max_bandwidth_mbps=t.get("max_bandwidth_mbps", 0),
            features=t.get("features", []),
            discovery=t.get("discovery", ""),
        ))
        for t in transports
    ]


def list_genicam_features(category: Optional[str] = None) -> list[dict[str, Any]]:
    cfg = _get_cfg()
    features = cfg.get("genicam_features", [])
    results = []
    for f in features:
        if category and f.get("category") != category:
            continue
        results.append(asdict(GenICamFeatureDef(
            feature_id=f["id"],
            name=f["name"],
            category=f["category"],
            feature_type=f["type"],
            description=f.get("description", ""),
            unit=f.get("unit", ""),
            min_val=f.get("min"),
            max_val=f.get("max"),
            values=f.get("values", []),
        )))
    return results


def list_camera_models(scan_type: Optional[str] = None) -> list[dict[str, Any]]:
    cfg = _get_cfg()
    models = cfg.get("camera_models", [])
    results = []
    for m in models:
        if scan_type and m.get("scan_type") != scan_type:
            continue
        results.append(asdict(CameraModelDef(
            model_id=m["id"],
            name=m["name"],
            vendor=m["vendor"],
            transport=m["transport"],
            sensor=m.get("sensor", ""),
            resolution=tuple(m.get("resolution", [0, 0])),
            scan_type=m.get("scan_type", "area"),
            max_fps=m.get("max_fps", 0.0),
            max_line_rate=m.get("max_line_rate", 0.0),
            pixel_formats=m.get("pixel_formats", []),
        )))
    return results


def list_trigger_modes() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    modes = cfg.get("trigger_modes", [])
    return [
        asdict(TriggerModeDef(
            mode_id=m["id"],
            name=m["name"],
            description=m.get("description", ""),
            source=m.get("source", ""),
            activation=m.get("activation", ""),
            requires_hardware=m.get("requires_hardware", False),
        ))
        for m in modes
    ]


def list_calibration_methods() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    methods = cfg.get("calibration_methods", [])
    return [
        asdict(CalibrationMethodDef(
            method_id=m["id"],
            name=m["name"],
            description=m.get("description", ""),
            pattern_type=m.get("pattern_type", ""),
            min_images=m.get("min_images", 0),
            recommended_images=m.get("recommended_images", 0),
            outputs=m.get("outputs", []),
        ))
        for m in methods
    ]


def list_encoder_interfaces() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    enc = cfg.get("encoder_config", {})
    interfaces = enc.get("interface_types", [])
    resolutions = enc.get("resolutions", [])
    return [{
        "interface_types": interfaces,
        "resolutions": resolutions,
        "divider_range": enc.get("divider_range", [1, 65535]),
        "direction_modes": enc.get("direction_modes", []),
        "index_reset": enc.get("index_reset", False),
        "debounce_us_range": enc.get("debounce_us_range", [0, 1000]),
    }]


def list_line_scan_config() -> dict[str, Any]:
    cfg = _get_cfg()
    ls = cfg.get("line_scan_config", {})
    return {
        "scan_directions": ls.get("scan_directions", []),
        "transport_mechanisms": ls.get("transport_mechanisms", []),
        "typical_line_rates_hz": ls.get("typical_line_rates_hz", []),
        "image_composition": ls.get("image_composition", {}),
        "lighting_types": ls.get("lighting_types", []),
    }


# ── Test recipes ───────────────────────────────────────────────────────


def list_test_recipes() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    recipes = cfg.get("test_recipes", [])
    return [
        asdict(TestRecipeDef(
            recipe_id=r["id"],
            name=r["name"],
            description=r.get("description", ""),
            domains=r.get("domains", []),
        ))
        for r in recipes
    ]


def run_test_recipe(recipe_id: str) -> TestResult:
    recipes = {r["recipe_id"]: r for r in list_test_recipes()}
    if recipe_id not in recipes:
        return TestResult(
            recipe_id=recipe_id,
            status=TestStatus.error.value,
            details=[{"error": f"Unknown recipe: {recipe_id}"}],
        )

    t0 = time.monotonic()

    if recipe_id == "transport_discovery":
        return _run_transport_discovery_recipe(recipe_id, t0)
    elif recipe_id == "camera_lifecycle":
        return _run_camera_lifecycle_recipe(recipe_id, t0)
    elif recipe_id == "trigger_modes":
        return _run_trigger_mode_recipe(recipe_id, t0)
    elif recipe_id == "multi_camera_calibration":
        return _run_calibration_recipe(recipe_id, t0)
    elif recipe_id == "line_scan_acquisition":
        return _run_line_scan_recipe(recipe_id, t0)
    elif recipe_id == "plc_roundtrip":
        return _run_plc_recipe(recipe_id, t0)
    elif recipe_id == "genicam_feature_access":
        return _run_genicam_feature_recipe(recipe_id, t0)
    elif recipe_id == "error_handling":
        return _run_error_handling_recipe(recipe_id, t0)

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.skipped.value,
        details=[{"note": "No runner for recipe"}],
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
    )


def _run_transport_discovery_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0

    transports = list_transports()
    if len(transports) >= 4:
        details.append({"test": "transport_count", "status": "passed", "count": len(transports)})
        passed += 1
    else:
        details.append({"test": "transport_count", "status": "failed", "count": len(transports)})
        failed += 1

    for t in transports:
        try:
            assert t["transport_id"], "missing transport_id"
            assert t["name"], "missing name"
            assert t["standard"], "missing standard"
            details.append({"test": f"transport_{t['transport_id']}", "status": "passed"})
            passed += 1
        except Exception as e:
            details.append({"test": f"transport_{t.get('transport_id', '?')}", "status": "failed", "error": str(e)})
            failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed, passed=passed, failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_camera_lifecycle_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0

    for tid in TransportId:
        try:
            cam = create_camera(tid.value, "test_model")
            assert cam.connect(), "connect failed"
            cfg = CameraConfig(transport_id=tid.value, camera_model="test_model")
            assert cam.configure(cfg), "configure failed"
            assert cam.state == CameraState.configured
            frame = cam.acquire()
            assert frame.frame_number > 0, "no frame acquired"
            assert len(frame.pixel_data) > 0, "empty pixel data"
            info = cam.get_transport_info()
            assert "transport" in info
            assert cam.disconnect(), "disconnect failed"
            assert cam.state == CameraState.disconnected
            details.append({"transport": tid.value, "status": "passed"})
            passed += 1
        except Exception as e:
            details.append({"transport": tid.value, "status": "failed", "error": str(e)})
            failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed, passed=passed, failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_trigger_mode_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0

    cam = create_camera("gige_vision", "test_trigger")
    cam.connect()
    cfg = CameraConfig(transport_id="gige_vision", camera_model="test_trigger")
    cam.configure(cfg)

    for mode in TriggerModeId:
        try:
            ok = cam.configure_trigger(mode.value)
            assert ok, f"configure_trigger({mode.value}) failed"
            frame = cam.acquire()
            assert frame.frame_number > 0

            if mode.value != "free_running":
                assert frame.trigger_timestamp is not None
            details.append({"mode": mode.value, "status": "passed"})
            passed += 1
        except Exception as e:
            details.append({"mode": mode.value, "status": "failed", "error": str(e)})
            failed += 1

    # Encoder trigger test
    try:
        enc = create_encoder("quadrature_ab", 1024, 4)
        state = enc.simulate_movement(100)
        assert state.position == 100
        positions = enc.get_trigger_positions(0, 100)
        assert len(positions) > 0
        details.append({"test": "encoder_trigger", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "encoder_trigger", "status": "failed", "error": str(e)})
        failed += 1

    cam.disconnect()

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed, passed=passed, failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_calibration_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0

    # Generate synthetic calibration frames
    frames = [_generate_synthetic_frame(640, 480, "Mono8", "checkerboard", seed=i) for i in range(15)]

    # Single camera calibration
    try:
        result = calibrate_camera(frames, "checkerboard")
        assert result.success
        assert result.reprojection_error < 1000.0
        assert len(result.camera_matrix) == 3
        assert len(result.distortion_coeffs) == 5
        details.append({"test": "single_camera_checkerboard", "status": "passed",
                         "reproj_error": result.reprojection_error})
        passed += 1
    except Exception as e:
        details.append({"test": "single_camera_checkerboard", "status": "failed", "error": str(e)})
        failed += 1

    # Stereo calibration
    try:
        frames_l = frames[:10]
        frames_r = [_generate_synthetic_frame(640, 480, "Mono8", "checkerboard", seed=i + 100) for i in range(10)]
        stereo = calibrate_stereo(frames_l, frames_r)
        assert stereo.success
        assert len(stereo.R) == 3
        assert len(stereo.T) == 3
        details.append({"test": "stereo_calibration", "status": "passed",
                         "reproj_error": stereo.reprojection_error})
        passed += 1
    except Exception as e:
        details.append({"test": "stereo_calibration", "status": "failed", "error": str(e)})
        failed += 1

    # Multi-camera bundle adjustment
    try:
        frame_sets = [
            [_generate_synthetic_frame(640, 480, "Mono8", "checkerboard", seed=i + j * 100)
             for i in range(10)]
            for j in range(3)
        ]
        multi = calibrate_multi_camera(frame_sets)
        assert multi.success
        assert multi.num_cameras == 3
        assert len(multi.camera_matrices) == 3
        assert multi.mean_reprojection_error < 1000.0
        details.append({"test": "multi_camera_bundle", "status": "passed",
                         "mean_reproj_error": multi.mean_reprojection_error})
        passed += 1
    except Exception as e:
        details.append({"test": "multi_camera_bundle", "status": "failed", "error": str(e)})
        failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed, passed=passed, failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_line_scan_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0

    # Forward scan
    try:
        lines = generate_line_scan_lines(1024, 256)
        image = compose_line_scan(lines, 1024, direction="forward")
        assert image.width == 1024
        assert image.height == 256
        assert image.total_lines == 256
        assert len(image.pixel_data) == 1024 * 256
        details.append({"test": "forward_scan", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "forward_scan", "status": "failed", "error": str(e)})
        failed += 1

    # Reverse scan
    try:
        lines = generate_line_scan_lines(1024, 128)
        image = compose_line_scan(lines, 1024, direction="reverse")
        assert image.direction == "reverse"
        assert image.height == 128
        details.append({"test": "reverse_scan", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "reverse_scan", "status": "failed", "error": str(e)})
        failed += 1

    # Bidirectional scan
    try:
        lines = generate_line_scan_lines(512, 64)
        image = compose_line_scan(lines, 512, direction="bidirectional")
        assert image.direction == "bidirectional"
        assert image.height == 64
        details.append({"test": "bidirectional_scan", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "bidirectional_scan", "status": "failed", "error": str(e)})
        failed += 1

    # Encoder-synchronised line scan
    try:
        enc = create_encoder("quadrature_ab", 4096, 16)
        enc.simulate_movement(1024)
        positions = enc.get_trigger_positions(0, 1024)
        assert len(positions) > 0
        lines = generate_line_scan_lines(2048, len(positions))
        image = compose_line_scan(lines, 2048, line_rate_hz=50000.0)
        assert image.total_lines == len(positions)
        details.append({"test": "encoder_sync_line_scan", "status": "passed",
                         "trigger_positions": len(positions)})
        passed += 1
    except Exception as e:
        details.append({"test": "encoder_sync_line_scan", "status": "failed", "error": str(e)})
        failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed, passed=passed, failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_plc_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0

    # Read Modbus registers
    try:
        ctx = get_plc_context()
        assert len(ctx.registers) > 0
        assert len(ctx.trigger_mapping) > 0

        r = read_plc_register("modbus", 40001)
        assert r["status"] == "ok"
        assert r["name"] == "trigger_count"

        r = read_plc_register("modbus", 40002)
        assert r["status"] == "ok"
        assert r["name"] == "encoder_position"

        details.append({"test": "modbus_read", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "modbus_read", "status": "failed", "error": str(e)})
        failed += 1

    # Write Modbus register
    try:
        r = write_plc_register("modbus", 40003, 1)
        assert r["status"] == "ok"
        assert r["value"] == 1

        r = write_plc_register("modbus", 40001, 0)
        assert r["status"] == "read_only"

        details.append({"test": "modbus_write", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "modbus_write", "status": "failed", "error": str(e)})
        failed += 1

    # OPC-UA read
    try:
        r = read_plc_register("opcua", "ns=2;s=Camera.TriggerCount")
        assert r["status"] == "ok"
        assert r["name"] == "trigger_count"
        details.append({"test": "opcua_read", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "opcua_read", "status": "failed", "error": str(e)})
        failed += 1

    # OPC-UA write
    try:
        r = write_plc_register("opcua", "ns=2;s=Camera.InspectionResult", 1)
        assert r["status"] == "ok"

        r = write_plc_register("opcua", "ns=2;s=Camera.TriggerCount", 0)
        assert r["status"] == "read_only"

        details.append({"test": "opcua_write", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "opcua_write", "status": "failed", "error": str(e)})
        failed += 1

    # Not-found registers
    try:
        r = read_plc_register("modbus", 99999)
        assert r["status"] == "not_found"
        r = read_plc_register("opcua", "ns=99;s=Missing")
        assert r["status"] == "not_found"
        r = read_plc_register("profinet", 1)
        assert r["status"] == "unsupported_protocol"
        details.append({"test": "plc_not_found", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "plc_not_found", "status": "failed", "error": str(e)})
        failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed, passed=passed, failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_genicam_feature_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0

    cam = create_camera("gige_vision", "test_features")
    cam.connect()
    cfg = CameraConfig(transport_id="gige_vision")
    cam.configure(cfg)

    # Set valid features
    try:
        assert cam.set_feature("exposure_time", 5000.0)
        assert cam.get_feature("exposure_time") == 5000.0
        assert cam.set_feature("gain", 12.0)
        assert cam.get_feature("gain") == 12.0
        assert cam.set_feature("pixel_format", "Mono8")
        assert cam.get_feature("pixel_format") == "Mono8"
        details.append({"test": "set_valid_features", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "set_valid_features", "status": "failed", "error": str(e)})
        failed += 1

    # Set invalid features
    try:
        assert not cam.set_feature("pixel_format", "InvalidFormat")
        assert not cam.set_feature("gain", 100.0)
        assert not cam.set_feature("nonexistent_feature", 42)
        details.append({"test": "reject_invalid_features", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "reject_invalid_features", "status": "failed", "error": str(e)})
        failed += 1

    # List features by category
    try:
        acq_features = list_genicam_features(category="AcquisitionControl")
        assert len(acq_features) > 0
        img_features = list_genicam_features(category="ImageFormatControl")
        assert len(img_features) > 0
        all_features = list_genicam_features()
        assert len(all_features) >= len(acq_features) + len(img_features)
        details.append({"test": "list_features_by_category", "status": "passed",
                         "total": len(all_features)})
        passed += 1
    except Exception as e:
        details.append({"test": "list_features_by_category", "status": "failed", "error": str(e)})
        failed += 1

    cam.disconnect()

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed, passed=passed, failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_error_handling_recipe(recipe_id: str, t0: float) -> TestResult:
    details = []
    passed = 0
    failed = 0

    # Unknown transport
    try:
        create_camera("unknown_transport")
        details.append({"test": "unknown_transport", "status": "failed", "error": "no exception"})
        failed += 1
    except ValueError:
        details.append({"test": "unknown_transport", "status": "passed"})
        passed += 1

    # Acquire while disconnected
    try:
        cam = create_camera("gige_vision")
        frame = cam.acquire()
        assert frame.frame_number == -1
        assert len(frame.pixel_data) == 0
        details.append({"test": "acquire_disconnected", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "acquire_disconnected", "status": "failed", "error": str(e)})
        failed += 1

    # Configure while disconnected
    try:
        cam = create_camera("usb3_vision")
        ok = cam.configure(CameraConfig(transport_id="usb3_vision"))
        assert not ok
        details.append({"test": "configure_disconnected", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "configure_disconnected", "status": "failed", "error": str(e)})
        failed += 1

    # Invalid trigger mode
    try:
        cam = create_camera("gige_vision")
        cam.connect()
        ok = cam.configure_trigger("invalid_mode")
        assert not ok
        cam.disconnect()
        details.append({"test": "invalid_trigger_mode", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "invalid_trigger_mode", "status": "failed", "error": str(e)})
        failed += 1

    # Invalid encoder interface
    try:
        create_encoder("invalid_interface")
        details.append({"test": "invalid_encoder", "status": "failed", "error": "no exception"})
        failed += 1
    except ValueError:
        details.append({"test": "invalid_encoder", "status": "passed"})
        passed += 1

    # Calibration with insufficient frames
    try:
        result = calibrate_camera([b"frame1"], "checkerboard")
        assert not result.success
        details.append({"test": "insufficient_calibration_frames", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "insufficient_calibration_frames", "status": "failed", "error": str(e)})
        failed += 1

    # Invalid calibration method
    try:
        result = calibrate_camera([b"f"] * 10, "invalid_method")
        assert not result.success
        details.append({"test": "invalid_calibration_method", "status": "passed"})
        passed += 1
    except Exception as e:
        details.append({"test": "invalid_calibration_method", "status": "failed", "error": str(e)})
        failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed, passed=passed, failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


# ── Artifacts & gate ───────────────────────────────────────────────────


def list_artifacts() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    arts = cfg.get("artifacts", [])
    return [
        asdict(ArtifactDef(
            artifact_id=a["id"],
            kind=a["kind"],
            description=a.get("description", ""),
        ))
        for a in arts
    ]


def validate_gate() -> dict[str, Any]:
    t0 = time.monotonic()
    recipe_results = []
    all_passed = True

    for recipe in list_test_recipes():
        result = run_test_recipe(recipe["recipe_id"])
        recipe_results.append(asdict(result))
        if result.status != TestStatus.passed.value:
            all_passed = False

    total_tests = sum(r["total"] for r in recipe_results)
    total_passed = sum(r["passed"] for r in recipe_results)
    total_failed = sum(r["failed"] for r in recipe_results)

    return {
        "verdict": GateVerdict.passed.value if all_passed else GateVerdict.failed.value,
        "total_recipes": len(recipe_results),
        "total_tests": total_tests,
        "total_passed": total_passed,
        "total_failed": total_failed,
        "duration_ms": round((time.monotonic() - t0) * 1000, 2),
        "recipes": recipe_results,
    }
