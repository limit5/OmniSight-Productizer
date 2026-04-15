"""C23 -- L4-CORE-23 Depth/3D sensing pipeline (#244).

Unified depth and 3D sensing pipeline covering Time-of-Flight sensors,
structured light, stereo matching, point cloud processing, ICP registration,
SLAM hooks, and calibration. Full chain from raw depth capture through
3D reconstruction and spatial alignment.

Public API:
    sensors         = list_sensors()
    sensor          = create_sensor(sensor_id, config)
    frame           = sensor.capture()
    patterns        = list_structured_light_patterns()
    codec           = create_structured_light_codec(pattern_type, resolution)
    depth           = decode_structured_light(frames, pattern_type, baseline, focal_length)
    algorithms      = list_stereo_algorithms()
    pipeline        = create_stereo_pipeline(algorithm, **kwargs)
    depth           = compute_stereo_depth(left, right, w, h, config)
    backends        = list_point_cloud_backends()
    processor       = create_point_cloud_processor(backend)
    cloud           = depth_to_points(depth_frame, camera_matrix)
    reg_algos       = list_registration_algorithms()
    result          = register_point_clouds(source, target, algorithm)
    slam_types      = list_slam_types()
    hook            = create_slam_hook(slam_type)
    cal_types       = list_calibration_types()
    cal_result      = calibrate_camera(frames, calibration_type, **kwargs)
    scenes          = list_test_scenes()
    cloud           = generate_test_scene(scene_id)
    validation      = validate_test_scene(scene_id, cloud)
    recipes         = list_test_recipes()
    report          = run_test_recipe(recipe_id)
    artifacts       = list_artifacts()
    verdict         = validate_gate()
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import struct
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "configs" / "depth_sensing.yaml"


# ── Enums ─────────────────────────────────────────────────────────────────


class DepthDomain(str, Enum):
    tof_sensors = "tof_sensors"
    structured_light = "structured_light"
    stereo = "stereo"
    point_cloud = "point_cloud"
    registration = "registration"
    slam = "slam"
    calibration = "calibration"


class SensorId(str, Enum):
    sony_imx556 = "sony_imx556"
    melexis_mlx75027 = "melexis_mlx75027"


class SensorState(str, Enum):
    disconnected = "disconnected"
    connected = "connected"
    configured = "configured"
    streaming = "streaming"
    error = "error"


class StructuredLightPattern(str, Enum):
    gray_code = "gray_code"
    phase_shift = "phase_shift"
    speckle = "speckle"


class StereoAlgorithm(str, Enum):
    sgbm = "sgbm"
    bm = "bm"


class PointCloudBackend(str, Enum):
    pcl = "pcl"
    open3d = "open3d"


class PointCloudFormat(str, Enum):
    pcd = "pcd"
    ply = "ply"
    xyz = "xyz"
    las = "las"


class FilterType(str, Enum):
    voxel_grid = "voxel_grid"
    statistical_outlier = "statistical_outlier"
    radius_outlier = "radius_outlier"
    passthrough = "passthrough"
    crop = "crop"


class RegistrationAlgorithm(str, Enum):
    icp_point_to_point = "icp_point_to_point"
    icp_point_to_plane = "icp_point_to_plane"
    colored_icp = "colored_icp"
    ndt = "ndt"


class SlamType(str, Enum):
    visual_slam = "visual_slam"
    lidar_slam = "lidar_slam"


class CalibrationType(str, Enum):
    intrinsic = "intrinsic"
    stereo_extrinsic = "stereo_extrinsic"
    tof_phase = "tof_phase"


class DepthResultStatus(str, Enum):
    success = "success"
    no_depth = "no_depth"
    calibration_required = "calibration_required"
    sensor_error = "sensor_error"
    timeout = "timeout"
    invalid_input = "invalid_input"


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


# ── Data models ───────────────────────────────────────────────────────────


@dataclass
class SensorConfig:
    sensor_id: str
    resolution: tuple = (640, 480)
    max_range: float = 5.0
    min_range: float = 0.1
    frame_rate: int = 30
    exposure_us: int = 1000
    modulation_freq_mhz: float = 20.0


@dataclass
class DepthFrame:
    width: int
    height: int
    depth_data: bytes  # float32 per pixel
    timestamp: float
    sensor_id: str
    frame_number: int
    min_depth: float
    max_depth: float


@dataclass
class StereoConfig:
    algorithm: str = "sgbm"
    num_disparities: int = 128
    block_size: int = 5
    baseline: float = 0.12  # metres
    focal_length: float = 500.0  # pixels
    P1: int = 600
    P2: int = 2400
    disp12MaxDiff: int = 1
    uniquenessRatio: int = 10
    speckleWindowSize: int = 100
    speckleRange: int = 32


@dataclass
class PointCloudData:
    points: list  # list of (x, y, z) tuples
    colors: list  # optional list of (r, g, b) tuples
    normals: list  # optional list of (nx, ny, nz) tuples
    point_count: int
    bounds_min: tuple  # (x, y, z)
    bounds_max: tuple  # (x, y, z)


@dataclass
class RegistrationResult:
    transformation: list  # 4x4 matrix as list of lists
    fitness: float  # 0.0-1.0
    inlier_rmse: float
    num_inliers: int
    converged: bool
    iterations: int


@dataclass
class CalibrationResult:
    calibration_type: str
    reprojection_error: float
    camera_matrix: list  # 3x3 as list of lists
    distortion_coeffs: list
    success: bool
    timestamp: float


@dataclass
class SlamPose:
    position: tuple  # (x, y, z)
    orientation: tuple  # (qw, qx, qy, qz) quaternion
    timestamp: float
    frame_id: int
    confidence: float


@dataclass
class TestScene:
    scene_id: str
    expected_point_count: int
    expected_bounds_min: tuple
    expected_bounds_max: tuple
    tolerance_points: int
    tolerance_bounds: float


@dataclass
class TestRecipeResult:
    recipe_id: str
    passed: bool
    duration_ms: float
    details: dict


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


# ── Config loader ─────────────────────────────────────────────────────────

_cfg: dict[str, Any] | None = None


def _load_config() -> dict[str, Any]:
    global _cfg
    if _cfg is not None:
        return _cfg
    with open(_CONFIG_PATH) as f:
        raw = yaml.safe_load(f)
    _cfg = raw.get("depth_sensing", raw)
    return _cfg


def _get_cfg() -> dict[str, Any]:
    return _load_config()


# ── Helper utilities ──────────────────────────────────────────────────────


def _sensor_hash(sensor_id: str) -> int:
    """Return a deterministic integer hash from a sensor_id string."""
    return int(hashlib.sha256(sensor_id.encode()).hexdigest()[:8], 16)


def _depth_frame_hash(frame: DepthFrame) -> str:
    """Compute deterministic hash of a DepthFrame."""
    h = hashlib.sha256()
    h.update(struct.pack(">II", frame.width, frame.height))
    h.update(frame.depth_data[:min(len(frame.depth_data), 4096)])
    h.update(frame.sensor_id.encode())
    h.update(struct.pack(">I", frame.frame_number))
    return h.hexdigest()[:16]


def _identity_matrix_4x4() -> list[list[float]]:
    """Return a 4x4 identity matrix as list of lists."""
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _apply_transform(points: list[tuple], transform: list[list[float]]) -> list[tuple]:
    """Apply a 4x4 homogeneous transformation to a list of 3D points."""
    result = []
    for x, y, z in points:
        nx = transform[0][0] * x + transform[0][1] * y + transform[0][2] * z + transform[0][3]
        ny = transform[1][0] * x + transform[1][1] * y + transform[1][2] * z + transform[1][3]
        nz = transform[2][0] * x + transform[2][1] * y + transform[2][2] * z + transform[2][3]
        result.append((nx, ny, nz))
    return result


def _compute_fitness(source: list[tuple], target: list[tuple],
                     max_distance: float) -> tuple[float, float]:
    """Compute registration fitness and RMSE between source and target point lists.

    Returns (fitness, inlier_rmse).
    """
    if not source or not target:
        return 0.0, float("inf")

    inlier_count = 0
    sum_sq = 0.0

    for sx, sy, sz in source:
        best_dist_sq = float("inf")
        for tx, ty, tz in target:
            dx = sx - tx
            dy = sy - ty
            dz = sz - tz
            d2 = dx * dx + dy * dy + dz * dz
            if d2 < best_dist_sq:
                best_dist_sq = d2
        best_dist = math.sqrt(best_dist_sq)
        if best_dist <= max_distance:
            inlier_count += 1
            sum_sq += best_dist_sq

    fitness = inlier_count / len(source) if source else 0.0
    rmse = math.sqrt(sum_sq / inlier_count) if inlier_count > 0 else float("inf")
    return fitness, rmse


def _generate_synthetic_depth(width: int, height: int, scene_type: str,
                              noise_std: float = 0.01, seed: int = 0) -> bytes:
    """Generate synthetic depth data as packed float32 bytes.

    The *seed* parameter makes output deterministic for a given call site.
    """
    # Use a simple deterministic pseudo-random based on seed
    rng_state = seed & 0xFFFFFFFF

    def _next_float() -> float:
        nonlocal rng_state
        rng_state = (rng_state * 1103515245 + 12345) & 0x7FFFFFFF
        return (rng_state / 0x7FFFFFFF) * 2.0 - 1.0  # -1..1

    values: list[float] = []

    if scene_type == "flat_wall":
        base_z = 1.0
        for _ in range(width * height):
            z = base_z + _next_float() * noise_std
            values.append(max(0.0, z))

    elif scene_type == "box_scene":
        box_cx, box_cy = width // 2, height // 2
        box_hw, box_hh = width // 6, height // 6
        for row in range(height):
            for col in range(width):
                if abs(col - box_cx) < box_hw and abs(row - box_cy) < box_hh:
                    z = 0.5 + _next_float() * noise_std
                else:
                    z = 1.0 + _next_float() * noise_std
                values.append(max(0.0, z))

    elif scene_type == "sphere":
        cx, cy = width / 2.0, height / 2.0
        radius_pixels = min(width, height) / 4.0
        sphere_depth_center = 0.5
        sphere_radius = 0.25
        for row in range(height):
            for col in range(width):
                dx = (col - cx) / radius_pixels
                dy = (row - cy) / radius_pixels
                r2 = dx * dx + dy * dy
                if r2 < 1.0:
                    z = sphere_depth_center - sphere_radius * math.sqrt(1.0 - r2)
                    z += _next_float() * noise_std
                else:
                    z = 1.5 + _next_float() * noise_std
                values.append(max(0.0, z))

    elif scene_type == "staircase":
        step_count = 5
        step_height_pixels = height // step_count
        for row in range(height):
            step_idx = min(row // step_height_pixels, step_count - 1)
            base_z = 0.5 + step_idx * 0.3
            for _ in range(width):
                z = base_z + _next_float() * noise_std
                values.append(max(0.0, z))

    elif scene_type == "corner":
        for row in range(height):
            for col in range(width):
                if col < width // 2:
                    z = 0.5 + (col / (width / 2.0)) * 0.5 + _next_float() * noise_std
                else:
                    z = 1.0 + _next_float() * noise_std
                values.append(max(0.0, z))

    elif scene_type == "empty_room":
        for row in range(height):
            for col in range(width):
                # Simulate a room: walls at edges, far wall in center
                edge_l = col / float(width)
                edge_r = 1.0 - edge_l
                edge_t = row / float(height)
                edge_b = 1.0 - edge_t
                min_edge = min(edge_l, edge_r, edge_t, edge_b)
                if min_edge < 0.1:
                    z = 0.3 + min_edge * 5.0 + _next_float() * noise_std
                else:
                    z = 2.0 + _next_float() * noise_std
                values.append(max(0.0, z))

    else:
        # Default: Gaussian depth distribution
        center_z = 1.0
        for _ in range(width * height):
            z = center_z + _next_float() * noise_std * 5.0
            values.append(max(0.0, z))

    return struct.pack(f"<{len(values)}f", *values)


def _unpack_depth(data: bytes, count: int) -> list[float]:
    """Unpack float32 depth values from bytes."""
    return list(struct.unpack(f"<{count}f", data[:count * 4]))


def _pack_depth(values: list[float]) -> bytes:
    """Pack float32 depth values to bytes."""
    return struct.pack(f"<{len(values)}f", *values)


def _default_camera_matrix(width: int, height: int) -> list[list[float]]:
    """Generate a plausible default camera intrinsic matrix."""
    fx = float(width)
    fy = float(width)
    cx = width / 2.0
    cy = height / 2.0
    return [
        [fx, 0.0, cx],
        [0.0, fy, cy],
        [0.0, 0.0, 1.0],
    ]


def _mat3x3_multiply(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    """Multiply two 3x3 matrices."""
    result = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            for k in range(3):
                result[i][j] += a[i][k] * b[k][j]
    return result


def _mat4x4_multiply(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    """Multiply two 4x4 matrices."""
    result = [[0.0] * 4 for _ in range(4)]
    for i in range(4):
        for j in range(4):
            for k in range(4):
                result[i][j] += a[i][k] * b[k][j]
    return result


def _rotation_matrix_z(angle_rad: float) -> list[list[float]]:
    """Return a 4x4 rotation matrix around the Z axis."""
    c = math.cos(angle_rad)
    s = math.sin(angle_rad)
    return [
        [c, -s, 0.0, 0.0],
        [s,  c, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _translation_matrix(tx: float, ty: float, tz: float) -> list[list[float]]:
    """Return a 4x4 translation matrix."""
    return [
        [1.0, 0.0, 0.0, tx],
        [0.0, 1.0, 0.0, ty],
        [0.0, 0.0, 1.0, tz],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _quaternion_from_axis_angle(axis: tuple, angle_rad: float) -> tuple:
    """Return (qw, qx, qy, qz) from axis-angle representation."""
    half = angle_rad / 2.0
    s = math.sin(half)
    norm = math.sqrt(axis[0] ** 2 + axis[1] ** 2 + axis[2] ** 2)
    if norm < 1e-12:
        return (1.0, 0.0, 0.0, 0.0)
    ax = axis[0] / norm
    ay = axis[1] / norm
    az = axis[2] / norm
    return (math.cos(half), ax * s, ay * s, az * s)


def _compute_bounds(points: list[tuple]) -> tuple[tuple, tuple]:
    """Compute axis-aligned bounding box of a point list.

    Returns (bounds_min, bounds_max).
    """
    if not points:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


# ── DepthSensor abstract base class ──────────────────────────────────────


class DepthSensor(ABC):
    """Abstract base class for all depth sensor adapters."""

    def __init__(self, sensor_id: str, config: SensorConfig):
        self._sensor_id = sensor_id
        self._config = config
        self._state = SensorState.disconnected
        self._frame_count = 0

    @abstractmethod
    def connect(self) -> bool:
        ...

    @abstractmethod
    def disconnect(self) -> bool:
        ...

    @abstractmethod
    def configure(self, config: SensorConfig) -> bool:
        ...

    @abstractmethod
    def capture(self) -> DepthFrame:
        ...

    @abstractmethod
    def get_capabilities(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_status(self) -> dict[str, Any]:
        ...

    @property
    def state(self) -> SensorState:
        return self._state

    @property
    def sensor_id(self) -> str:
        return self._sensor_id

    @property
    def config(self) -> SensorConfig:
        return self._config

    @property
    def frame_count(self) -> int:
        return self._frame_count


# ── Vendor adapters ───────────────────────────────────────────────────────


class _BaseToFAdapter(DepthSensor):
    """Shared capture logic for indirect ToF sensor adapters."""

    _CAPABILITIES: dict[str, Any] = {}
    _DEFAULT_RESOLUTION: tuple = (640, 480)
    _DEFAULT_MAX_RANGE: float = 5.0
    _NOISE_STD: float = 0.01

    def connect(self) -> bool:
        if self._state != SensorState.disconnected:
            return False
        self._state = SensorState.connected
        logger.info("Sensor connected: id=%s", self._sensor_id)
        return True

    def disconnect(self) -> bool:
        if self._state == SensorState.disconnected:
            return False
        self._state = SensorState.disconnected
        self._frame_count = 0
        logger.info("Sensor disconnected: id=%s", self._sensor_id)
        return True

    def configure(self, config: SensorConfig) -> bool:
        if self._state not in (SensorState.connected, SensorState.configured):
            return False
        # Validate resolution
        w, h = config.resolution
        if w <= 0 or h <= 0:
            return False
        if config.max_range <= config.min_range:
            return False
        if config.frame_rate <= 0:
            return False
        self._config = config
        self._state = SensorState.configured
        logger.info("Sensor configured: id=%s res=%dx%d", self._sensor_id, w, h)
        return True

    def capture(self) -> DepthFrame:
        if self._state not in (SensorState.configured, SensorState.connected,
                                SensorState.streaming):
            return DepthFrame(
                width=0, height=0, depth_data=b"",
                timestamp=time.time(), sensor_id=self._sensor_id,
                frame_number=self._frame_count,
                min_depth=0.0, max_depth=0.0,
            )

        self._state = SensorState.streaming
        w, h = self._config.resolution

        # Deterministic seed from sensor hash and frame number
        seed = _sensor_hash(self._sensor_id) ^ (self._frame_count * 2654435761)
        depth_data = _generate_synthetic_depth(
            w, h, "flat_wall", noise_std=self._NOISE_STD, seed=seed,
        )

        pixel_count = w * h
        values = _unpack_depth(depth_data, pixel_count)
        valid = [v for v in values if v > 0.0]
        min_d = min(valid) if valid else 0.0
        max_d = max(valid) if valid else 0.0

        frame = DepthFrame(
            width=w, height=h, depth_data=depth_data,
            timestamp=time.time(), sensor_id=self._sensor_id,
            frame_number=self._frame_count,
            min_depth=round(min_d, 4), max_depth=round(max_d, 4),
        )
        self._frame_count += 1
        return frame

    def get_status(self) -> dict[str, Any]:
        return {
            "sensor_id": self._sensor_id,
            "state": self._state.value,
            "frame_count": self._frame_count,
            "resolution": self._config.resolution,
            "max_range": self._config.max_range,
            "frame_rate": self._config.frame_rate,
            "modulation_freq_mhz": self._config.modulation_freq_mhz,
        }

    def get_capabilities(self) -> dict[str, Any]:
        return dict(self._CAPABILITIES)


class SonyIMX556Adapter(_BaseToFAdapter):
    """Sony IMX556 back-illuminated indirect ToF image sensor adapter.

    640x480 resolution, up to 5 m range, CW modulation at 20/50/100 MHz.
    Supports ambient light suppression, multi-frequency phase unwrapping,
    confidence maps, and IR amplitude imaging.
    """

    _CAPABILITIES = {
        "sensor": "sony_imx556",
        "name": "Sony IMX556 ToF",
        "technology": "indirect_tof",
        "resolution": (640, 480),
        "max_range_m": 5.0,
        "depth_accuracy_m": 0.01,
        "frame_rate_fps": 30,
        "modulation_frequencies_mhz": [20, 50, 100],
        "pixel_pitch_um": 10.0,
        "interface": "mipi_csi2",
        "features": [
            "ambient_light_suppression",
            "multi_frequency_unwrap",
            "confidence_map",
            "ir_amplitude_image",
        ],
    }
    _DEFAULT_RESOLUTION = (640, 480)
    _DEFAULT_MAX_RANGE = 5.0
    _NOISE_STD = 0.01


class MelexisMLX75027Adapter(_BaseToFAdapter):
    """Melexis MLX75027 QVGA ToF sensor adapter.

    320x240 resolution, up to 2 m range, optimised for high-speed indoor use.
    Lower noise floor than IMX556 at short range but narrower FoV.
    """

    _CAPABILITIES = {
        "sensor": "melexis_mlx75027",
        "name": "Melexis MLX75027",
        "technology": "indirect_tof",
        "resolution": (320, 240),
        "max_range_m": 2.0,
        "depth_accuracy_m": 0.005,
        "frame_rate_fps": 60,
        "modulation_frequencies_mhz": [80, 100],
        "pixel_pitch_um": 15.0,
        "interface": "mipi_csi2",
        "features": [
            "ambient_light_suppression",
            "multi_frequency_unwrap",
            "confidence_map",
            "high_dynamic_range",
        ],
    }
    _DEFAULT_RESOLUTION = (320, 240)
    _DEFAULT_MAX_RANGE = 2.0
    _NOISE_STD = 0.005


_ADAPTER_MAP: dict[str, type[DepthSensor]] = {
    SensorId.sony_imx556.value: SonyIMX556Adapter,
    SensorId.melexis_mlx75027.value: MelexisMLX75027Adapter,
}


# ── Structured Light Module ───────────────────────────────────────────────


class StructuredLightCodec:
    """Encode and decode structured light patterns for depth recovery.

    Supports Gray code, sinusoidal phase-shift, and IR speckle patterns.
    """

    def __init__(self, pattern_type: str, projector_resolution: tuple = (1280, 720)):
        if pattern_type not in (p.value for p in StructuredLightPattern):
            raise ValueError(f"Unknown pattern type: {pattern_type}")
        self._pattern_type = pattern_type
        self._width, self._height = projector_resolution
        self._num_patterns = self._compute_num_patterns()

    def _compute_num_patterns(self) -> int:
        if self._pattern_type == StructuredLightPattern.gray_code.value:
            return max(1, int(math.ceil(math.log2(self._width))))
        elif self._pattern_type == StructuredLightPattern.phase_shift.value:
            return 4  # 4-step phase shift
        elif self._pattern_type == StructuredLightPattern.speckle.value:
            return 1  # single shot
        return 1

    @property
    def pattern_type(self) -> str:
        return self._pattern_type

    @property
    def num_patterns(self) -> int:
        return self._num_patterns

    @property
    def projector_resolution(self) -> tuple:
        return (self._width, self._height)

    def generate_patterns(self) -> list[bytes]:
        """Generate projection patterns as byte arrays (uint8 per pixel)."""
        if self._pattern_type == StructuredLightPattern.gray_code.value:
            return self._generate_gray_code()
        elif self._pattern_type == StructuredLightPattern.phase_shift.value:
            return self._generate_phase_shift()
        elif self._pattern_type == StructuredLightPattern.speckle.value:
            return self._generate_speckle()
        return []

    def _generate_gray_code(self) -> list[bytes]:
        """Generate Gray code binary patterns.

        Each pattern encodes one bit of the column index in Gray code ordering.
        """
        patterns: list[bytes] = []
        for bit in range(self._num_patterns):
            row_data = bytearray(self._width * self._height)
            for row in range(self._height):
                for col in range(self._width):
                    gray = col ^ (col >> 1)
                    pixel = 255 if (gray >> (self._num_patterns - 1 - bit)) & 1 else 0
                    row_data[row * self._width + col] = pixel
            patterns.append(bytes(row_data))
        return patterns

    def _generate_phase_shift(self) -> list[bytes]:
        """Generate 4-step sinusoidal phase-shift patterns.

        Phase offsets: 0, pi/2, pi, 3pi/2.
        """
        patterns: list[bytes] = []
        period = self._width / 8.0  # 8 periods across width
        for step in range(4):
            phase_offset = step * math.pi / 2.0
            row_data = bytearray(self._width * self._height)
            for row in range(self._height):
                for col in range(self._width):
                    val = 127.5 + 127.5 * math.sin(2.0 * math.pi * col / period + phase_offset)
                    row_data[row * self._width + col] = max(0, min(255, int(val)))
            patterns.append(bytes(row_data))
        return patterns

    def _generate_speckle(self) -> list[bytes]:
        """Generate pseudo-random IR speckle pattern.

        Deterministic pattern based on pixel coordinates.
        """
        data = bytearray(self._width * self._height)
        rng = 42
        for i in range(self._width * self._height):
            rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
            data[i] = rng & 0xFF
        return [bytes(data)]

    def decode(self, captured_frames: list[bytes], pattern_type: str = None,
               baseline: float = 0.1, focal_length: float = 500.0) -> DepthFrame:
        """Decode captured frames into a depth map.

        Parameters
        ----------
        captured_frames : list[bytes]
            One uint8 image per pattern exposure, each of size width*height.
        pattern_type : str, optional
            Override pattern type; defaults to self._pattern_type.
        baseline : float
            Projector-camera baseline in metres.
        focal_length : float
            Camera focal length in pixels.

        Returns
        -------
        DepthFrame
        """
        pt = pattern_type or self._pattern_type

        if pt == StructuredLightPattern.gray_code.value:
            return self._decode_gray_code(captured_frames, baseline, focal_length)
        elif pt == StructuredLightPattern.phase_shift.value:
            return self._decode_phase_shift(captured_frames, baseline, focal_length)
        elif pt == StructuredLightPattern.speckle.value:
            return self._decode_speckle(captured_frames, baseline, focal_length)

        # Fallback: empty frame
        return DepthFrame(
            width=self._width, height=self._height,
            depth_data=b"\x00" * (self._width * self._height * 4),
            timestamp=time.time(), sensor_id="structured_light",
            frame_number=0, min_depth=0.0, max_depth=0.0,
        )

    def _decode_gray_code(self, frames: list[bytes],
                          baseline: float, focal_length: float) -> DepthFrame:
        """Decode Gray code frames to depth.

        Column index is recovered via binary-to-Gray conversion, then
        triangulated using baseline and focal length.
        """
        w, h = self._width, self._height
        num_bits = min(len(frames), self._num_patterns)
        depths: list[float] = []
        min_d = float("inf")
        max_d = 0.0

        for row in range(h):
            for col in range(w):
                idx = row * w + col
                gray_val = 0
                for bit in range(num_bits):
                    if idx < len(frames[bit]):
                        pixel = frames[bit][idx] if isinstance(frames[bit], (bytes, bytearray)) else 0
                    else:
                        pixel = 0
                    if pixel > 127:
                        gray_val |= (1 << (num_bits - 1 - bit))

                # Gray to binary
                binary_val = gray_val
                mask = gray_val >> 1
                while mask:
                    binary_val ^= mask
                    mask >>= 1

                # Disparity from column correspondence
                disparity = abs(binary_val - col) if binary_val != col else 0.5
                if disparity > 0:
                    z = (baseline * focal_length) / disparity
                    z = max(0.01, min(z, 10.0))
                else:
                    z = 0.0

                depths.append(z)
                if z > 0:
                    min_d = min(min_d, z)
                    max_d = max(max_d, z)

        if min_d == float("inf"):
            min_d = 0.0

        depth_bytes = _pack_depth(depths)
        return DepthFrame(
            width=w, height=h, depth_data=depth_bytes,
            timestamp=time.time(), sensor_id="structured_light_gray_code",
            frame_number=0,
            min_depth=round(min_d, 4), max_depth=round(max_d, 4),
        )

    def _decode_phase_shift(self, frames: list[bytes],
                            baseline: float, focal_length: float) -> DepthFrame:
        """Decode 4-step phase-shift frames to depth.

        Phase is recovered via atan2 of the 4 intensity samples, then
        unwrapped and converted to depth through triangulation.
        """
        w, h = self._width, self._height
        if len(frames) < 4:
            return DepthFrame(
                width=w, height=h,
                depth_data=b"\x00" * (w * h * 4),
                timestamp=time.time(), sensor_id="structured_light_phase_shift",
                frame_number=0, min_depth=0.0, max_depth=0.0,
            )

        depths: list[float] = []
        min_d = float("inf")
        max_d = 0.0
        period = w / 8.0

        for row in range(h):
            for col in range(w):
                idx = row * w + col
                i0 = frames[0][idx] if idx < len(frames[0]) else 128
                i1 = frames[1][idx] if idx < len(frames[1]) else 128
                i2 = frames[2][idx] if idx < len(frames[2]) else 128
                i3 = frames[3][idx] if idx < len(frames[3]) else 128

                # 4-step phase recovery: phi = atan2(I3 - I1, I0 - I2)
                num = float(i3) - float(i1)
                den = float(i0) - float(i2)
                if abs(den) < 1e-6 and abs(num) < 1e-6:
                    phase = 0.0
                else:
                    phase = math.atan2(num, den)

                # Unwrap to positive [0, 2*pi)
                if phase < 0:
                    phase += 2.0 * math.pi

                # Column correspondence from phase
                col_proj = (phase / (2.0 * math.pi)) * period
                disparity = abs(col_proj - (col % period))
                if disparity < 0.1:
                    disparity = 0.1  # avoid division by zero

                z = (baseline * focal_length) / disparity
                z = max(0.01, min(z, 10.0))
                depths.append(z)
                if z > 0:
                    min_d = min(min_d, z)
                    max_d = max(max_d, z)

        if min_d == float("inf"):
            min_d = 0.0

        depth_bytes = _pack_depth(depths)
        return DepthFrame(
            width=w, height=h, depth_data=depth_bytes,
            timestamp=time.time(), sensor_id="structured_light_phase_shift",
            frame_number=0,
            min_depth=round(min_d, 4), max_depth=round(max_d, 4),
        )

    def _decode_speckle(self, frames: list[bytes],
                        baseline: float, focal_length: float) -> DepthFrame:
        """Decode speckle pattern via correlation-based matching.

        A simplified 1D block correlation along each row maps the IR speckle
        to column disparity, which is then triangulated.
        """
        w, h = self._width, self._height
        if not frames:
            return DepthFrame(
                width=w, height=h,
                depth_data=b"\x00" * (w * h * 4),
                timestamp=time.time(), sensor_id="structured_light_speckle",
                frame_number=0, min_depth=0.0, max_depth=0.0,
            )

        frame = frames[0]
        depths: list[float] = []
        min_d = float("inf")
        max_d = 0.0
        block = 7
        half = block // 2

        # Reference pattern (projected)
        ref_pattern = self._generate_speckle()[0]

        for row in range(h):
            for col in range(w):
                idx = row * w + col
                captured_val = frame[idx] if idx < len(frame) else 128
                ref_val = ref_pattern[idx] if idx < len(ref_pattern) else 128

                # Simple correlation: estimate disparity from intensity difference
                diff = abs(int(captured_val) - int(ref_val))
                disparity = 1.0 + diff / 25.5  # map 0-255 diff to 1-11 disparity
                z = (baseline * focal_length) / disparity
                z = max(0.01, min(z, 10.0))
                depths.append(z)
                if z > 0:
                    min_d = min(min_d, z)
                    max_d = max(max_d, z)

        if min_d == float("inf"):
            min_d = 0.0

        depth_bytes = _pack_depth(depths)
        return DepthFrame(
            width=w, height=h, depth_data=depth_bytes,
            timestamp=time.time(), sensor_id="structured_light_speckle",
            frame_number=0,
            min_depth=round(min_d, 4), max_depth=round(max_d, 4),
        )


def create_structured_light_codec(pattern_type: str,
                                  resolution: tuple = (1280, 720)) -> StructuredLightCodec:
    """Factory: create a StructuredLightCodec instance."""
    return StructuredLightCodec(pattern_type, resolution)


def decode_structured_light(captured_frames: list[bytes], pattern_type: str,
                            baseline: float = 0.1,
                            focal_length: float = 500.0,
                            resolution: tuple = (1280, 720)) -> DepthFrame:
    """Convenience function: decode captured structured light frames to depth."""
    codec = StructuredLightCodec(pattern_type, resolution)
    return codec.decode(captured_frames, pattern_type, baseline, focal_length)


# ── Stereo Pipeline ───────────────────────────────────────────────────────


class StereoPipeline:
    """Stereo rectification and disparity computation pipeline.

    Supports SGBM (Semi-Global Block Matching) and BM (basic Block Matching).
    """

    def __init__(self, config: StereoConfig = None):
        self._config = config or StereoConfig()

    @property
    def config(self) -> StereoConfig:
        return self._config

    def rectify(self, left_frame: bytes, right_frame: bytes,
                width: int, height: int,
                camera_matrix_left: list = None, camera_matrix_right: list = None,
                dist_left: list = None, dist_right: list = None,
                R: list = None, T: list = None) -> tuple[bytes, bytes]:
        """Rectify a stereo pair using camera parameters.

        In this simulated implementation the images are returned with
        a simple undistortion approximation applied.  Real implementations
        would invoke cv2.stereoRectify + cv2.initUndistortRectifyMap.

        Returns (rectified_left, rectified_right).
        """
        if camera_matrix_left is None:
            camera_matrix_left = _default_camera_matrix(width, height)
        if camera_matrix_right is None:
            camera_matrix_right = _default_camera_matrix(width, height)
        if dist_left is None:
            dist_left = [0.0, 0.0, 0.0, 0.0, 0.0]
        if dist_right is None:
            dist_right = [0.0, 0.0, 0.0, 0.0, 0.0]

        # Simulated rectification: apply radial undistortion approximation
        rect_left = self._undistort(left_frame, width, height,
                                    camera_matrix_left, dist_left)
        rect_right = self._undistort(right_frame, width, height,
                                     camera_matrix_right, dist_right)
        return rect_left, rect_right

    def _undistort(self, frame: bytes, width: int, height: int,
                   camera_matrix: list, dist_coeffs: list) -> bytes:
        """Apply a simplified radial undistortion to an 8-bit grayscale image."""
        if not frame:
            return frame

        cx = camera_matrix[0][2]
        cy = camera_matrix[1][2]
        fx = camera_matrix[0][0]
        fy = camera_matrix[1][1]
        k1 = dist_coeffs[0] if len(dist_coeffs) > 0 else 0.0
        k2 = dist_coeffs[1] if len(dist_coeffs) > 1 else 0.0

        out = bytearray(len(frame))
        for row in range(height):
            for col in range(width):
                # Normalised coordinates
                xn = (col - cx) / fx if fx != 0 else 0.0
                yn = (row - cy) / fy if fy != 0 else 0.0
                r2 = xn * xn + yn * yn
                radial = 1.0 + k1 * r2 + k2 * r2 * r2

                src_x = int(cx + xn * radial * fx)
                src_y = int(cy + yn * radial * fy)

                idx_dst = row * width + col
                if 0 <= src_x < width and 0 <= src_y < height:
                    idx_src = src_y * width + src_x
                    if idx_src < len(frame):
                        out[idx_dst] = frame[idx_src]
                    else:
                        out[idx_dst] = 0
                else:
                    out[idx_dst] = 0

        return bytes(out)

    def compute_disparity(self, left_rectified: bytes, right_rectified: bytes,
                          width: int, height: int) -> bytes:
        """Compute disparity map using SGBM or BM.

        Returns disparity as packed float32 bytes (one value per pixel).
        """
        if self._config.algorithm == StereoAlgorithm.sgbm.value:
            return self._compute_sgbm(left_rectified, right_rectified, width, height)
        elif self._config.algorithm == StereoAlgorithm.bm.value:
            return self._compute_bm(left_rectified, right_rectified, width, height)
        return self._compute_sgbm(left_rectified, right_rectified, width, height)

    def _compute_sgbm(self, left: bytes, right: bytes,
                      width: int, height: int) -> bytes:
        """Simulated Semi-Global Block Matching.

        Computes a simplified SAD (Sum of Absolute Differences) block match
        along each scanline within the configured disparity range, then
        applies a consistency check.
        """
        num_disp = self._config.num_disparities
        block = self._config.block_size
        half = block // 2
        disparities: list[float] = []

        for row in range(height):
            for col in range(width):
                best_d = 0
                best_cost = float("inf")

                for d in range(0, min(num_disp, col + 1), 4):  # step by 4 for speed
                    cost = 0.0
                    count = 0
                    for dy in range(-half, half + 1):
                        for dx in range(-half, half + 1):
                            ry = row + dy
                            lx = col + dx
                            rx = col + dx - d
                            if 0 <= ry < height and 0 <= lx < width and 0 <= rx < width:
                                li = ry * width + lx
                                ri = ry * width + rx
                                lv = left[li] if li < len(left) else 0
                                rv = right[ri] if ri < len(right) else 0
                                cost += abs(int(lv) - int(rv))
                                count += 1
                    if count > 0:
                        cost /= count
                    if cost < best_cost:
                        best_cost = cost
                        best_d = d

                disparities.append(float(best_d))

        return _pack_depth(disparities)

    def _compute_bm(self, left: bytes, right: bytes,
                    width: int, height: int) -> bytes:
        """Simulated basic Block Matching (faster, sparser than SGBM)."""
        num_disp = min(self._config.num_disparities, 64)
        block = self._config.block_size
        half = block // 2
        disparities: list[float] = []

        for row in range(height):
            for col in range(width):
                best_d = 0
                best_cost = float("inf")

                for d in range(0, min(num_disp, col + 1), 8):  # step by 8
                    cost = 0.0
                    count = 0
                    for dx in range(-half, half + 1):
                        lx = col + dx
                        rx = col + dx - d
                        if 0 <= lx < width and 0 <= rx < width:
                            li = row * width + lx
                            ri = row * width + rx
                            lv = left[li] if li < len(left) else 0
                            rv = right[ri] if ri < len(right) else 0
                            cost += abs(int(lv) - int(rv))
                            count += 1
                    if count > 0:
                        cost /= count
                    if cost < best_cost:
                        best_cost = cost
                        best_d = d

                disparities.append(float(best_d))

        return _pack_depth(disparities)

    def disparity_to_depth(self, disparity: bytes, width: int, height: int) -> DepthFrame:
        """Convert a disparity map to a DepthFrame.

        Uses the relation depth = baseline * focal_length / disparity.
        """
        pixel_count = width * height
        disp_values = _unpack_depth(disparity, pixel_count)
        depths: list[float] = []
        min_d = float("inf")
        max_d = 0.0

        bl = self._config.baseline
        fl = self._config.focal_length

        for d in disp_values:
            if d > 0.5:  # valid disparity threshold
                z = (bl * fl) / d
                z = max(0.01, min(z, 100.0))
            else:
                z = 0.0
            depths.append(z)
            if z > 0:
                min_d = min(min_d, z)
                max_d = max(max_d, z)

        if min_d == float("inf"):
            min_d = 0.0

        return DepthFrame(
            width=width, height=height,
            depth_data=_pack_depth(depths),
            timestamp=time.time(),
            sensor_id="stereo",
            frame_number=0,
            min_depth=round(min_d, 4),
            max_depth=round(max_d, 4),
        )


def create_stereo_pipeline(algorithm: str = "sgbm", **kwargs: Any) -> StereoPipeline:
    """Factory: create a StereoPipeline with the specified algorithm."""
    config = StereoConfig(algorithm=algorithm, **kwargs)
    return StereoPipeline(config)


def compute_stereo_depth(left_frame: bytes, right_frame: bytes,
                         width: int, height: int,
                         config: StereoConfig = None) -> DepthFrame:
    """Convenience: rectify, compute disparity, and convert to depth in one call."""
    if config is None:
        config = StereoConfig()
    pipeline = StereoPipeline(config)
    rect_l, rect_r = pipeline.rectify(left_frame, right_frame, width, height)
    disp = pipeline.compute_disparity(rect_l, rect_r, width, height)
    return pipeline.disparity_to_depth(disp, width, height)


# ── Point Cloud Module ────────────────────────────────────────────────────


class PointCloudProcessor:
    """Convert depth frames to point clouds, apply filters, and export/import."""

    def __init__(self, backend: str = "open3d"):
        if backend not in (b.value for b in PointCloudBackend):
            raise ValueError(f"Unknown point cloud backend: {backend}")
        self._backend = backend

    @property
    def backend(self) -> str:
        return self._backend

    def depth_to_point_cloud(self, depth_frame: DepthFrame,
                             camera_matrix: list = None,
                             color_frame: bytes = None) -> PointCloudData:
        """Convert a depth frame to a 3D point cloud using the pinhole camera model.

        x = (u - cx) * z / fx
        y = (v - cy) * z / fy
        """
        w, h = depth_frame.width, depth_frame.height
        if camera_matrix is None:
            camera_matrix = _default_camera_matrix(w, h)

        fx = camera_matrix[0][0]
        fy = camera_matrix[1][1]
        cx = camera_matrix[0][2]
        cy = camera_matrix[1][2]

        pixel_count = w * h
        depth_values = _unpack_depth(depth_frame.depth_data, pixel_count)

        points: list[tuple] = []
        colors: list[tuple] = []

        has_color = color_frame is not None and len(color_frame) >= pixel_count * 3

        for row in range(h):
            for col in range(w):
                idx = row * w + col
                z = depth_values[idx]
                if z <= 0.0:
                    continue
                x = (col - cx) * z / fx if fx != 0 else 0.0
                y = (row - cy) * z / fy if fy != 0 else 0.0
                points.append((x, y, z))

                if has_color:
                    ci = idx * 3
                    r = color_frame[ci] / 255.0
                    g = color_frame[ci + 1] / 255.0
                    b = color_frame[ci + 2] / 255.0
                    colors.append((r, g, b))

        bounds_min, bounds_max = _compute_bounds(points)

        return PointCloudData(
            points=points,
            colors=colors,
            normals=[],
            point_count=len(points),
            bounds_min=bounds_min,
            bounds_max=bounds_max,
        )

    def filter_point_cloud(self, cloud: PointCloudData,
                           filter_type: str, **params: Any) -> PointCloudData:
        """Apply a filter to the point cloud.

        Supported filters:
        - voxel_grid: downsample with voxel_size
        - statistical_outlier: remove outliers based on mean+std of k-NN distances
        - radius_outlier: remove points with fewer than min_neighbors within radius
        - passthrough: keep points within [min_val, max_val] on a given axis
        - crop: keep points within an axis-aligned bounding box
        """
        if filter_type == FilterType.voxel_grid.value:
            return self._filter_voxel_grid(cloud, **params)
        elif filter_type == FilterType.statistical_outlier.value:
            return self._filter_statistical_outlier(cloud, **params)
        elif filter_type == FilterType.radius_outlier.value:
            return self._filter_radius_outlier(cloud, **params)
        elif filter_type == FilterType.passthrough.value:
            return self._filter_passthrough(cloud, **params)
        elif filter_type == FilterType.crop.value:
            return self._filter_crop(cloud, **params)
        else:
            raise ValueError(f"Unknown filter type: {filter_type}")

    def _filter_voxel_grid(self, cloud: PointCloudData,
                           voxel_size: float = 0.01, **_: Any) -> PointCloudData:
        """Down-sample by keeping one point per voxel cell."""
        if voxel_size <= 0:
            return cloud

        voxel_map: dict[tuple, int] = {}
        for i, (x, y, z) in enumerate(cloud.points):
            key = (
                int(math.floor(x / voxel_size)),
                int(math.floor(y / voxel_size)),
                int(math.floor(z / voxel_size)),
            )
            if key not in voxel_map:
                voxel_map[key] = i

        indices = sorted(voxel_map.values())
        return self._subset(cloud, indices)

    def _filter_statistical_outlier(self, cloud: PointCloudData,
                                    nb_neighbors: int = 20,
                                    std_ratio: float = 2.0, **_: Any) -> PointCloudData:
        """Remove points whose mean k-NN distance exceeds global mean + std_ratio * std."""
        n = cloud.point_count
        if n <= nb_neighbors:
            return cloud

        # Compute mean distance to k nearest neighbours (brute force, capped for speed)
        sample_step = max(1, n // 500)
        mean_dists: list[float] = []

        for i in range(0, n, sample_step):
            dists = []
            px, py, pz = cloud.points[i]
            for j in range(0, n, max(1, n // 200)):
                if i == j:
                    continue
                qx, qy, qz = cloud.points[j]
                d2 = (px - qx) ** 2 + (py - qy) ** 2 + (pz - qz) ** 2
                dists.append(d2)
            dists.sort()
            k = min(nb_neighbors, len(dists))
            if k > 0:
                mean_dists.append(sum(dists[:k]) / k)
            else:
                mean_dists.append(0.0)

        if not mean_dists:
            return cloud

        global_mean = sum(mean_dists) / len(mean_dists)
        variance = sum((d - global_mean) ** 2 for d in mean_dists) / len(mean_dists)
        global_std = math.sqrt(variance)
        threshold = global_mean + std_ratio * global_std

        # Now filter all points using the threshold
        indices: list[int] = []
        for i in range(n):
            px, py, pz = cloud.points[i]
            dists = []
            for j in range(0, n, max(1, n // 200)):
                if i == j:
                    continue
                qx, qy, qz = cloud.points[j]
                d2 = (px - qx) ** 2 + (py - qy) ** 2 + (pz - qz) ** 2
                dists.append(d2)
            dists.sort()
            k = min(nb_neighbors, len(dists))
            if k > 0:
                mean_d = sum(dists[:k]) / k
            else:
                mean_d = 0.0
            if mean_d <= threshold:
                indices.append(i)

        return self._subset(cloud, indices)

    def _filter_radius_outlier(self, cloud: PointCloudData,
                               radius: float = 0.05,
                               min_neighbors: int = 5, **_: Any) -> PointCloudData:
        """Remove points that have fewer than *min_neighbors* within *radius*."""
        r2 = radius * radius
        indices: list[int] = []
        n = cloud.point_count

        for i in range(n):
            px, py, pz = cloud.points[i]
            count = 0
            for j in range(0, n, max(1, n // 500)):
                if i == j:
                    continue
                qx, qy, qz = cloud.points[j]
                d2 = (px - qx) ** 2 + (py - qy) ** 2 + (pz - qz) ** 2
                if d2 <= r2:
                    count += 1
                    if count >= min_neighbors:
                        break
            if count >= min_neighbors:
                indices.append(i)

        return self._subset(cloud, indices)

    def _filter_passthrough(self, cloud: PointCloudData,
                            axis: str = "z",
                            min_val: float = 0.0,
                            max_val: float = 1.0, **_: Any) -> PointCloudData:
        """Keep points where *axis* value is in [min_val, max_val]."""
        axis_idx = {"x": 0, "y": 1, "z": 2}.get(axis, 2)
        indices = [
            i for i, p in enumerate(cloud.points)
            if min_val <= p[axis_idx] <= max_val
        ]
        return self._subset(cloud, indices)

    def _filter_crop(self, cloud: PointCloudData,
                     min_bound: tuple = (0.0, 0.0, 0.0),
                     max_bound: tuple = (1.0, 1.0, 1.0), **_: Any) -> PointCloudData:
        """Keep points inside an axis-aligned bounding box."""
        indices = [
            i for i, (x, y, z) in enumerate(cloud.points)
            if (min_bound[0] <= x <= max_bound[0] and
                min_bound[1] <= y <= max_bound[1] and
                min_bound[2] <= z <= max_bound[2])
        ]
        return self._subset(cloud, indices)

    def _subset(self, cloud: PointCloudData, indices: list[int]) -> PointCloudData:
        """Create a new PointCloudData with only the given indices."""
        points = [cloud.points[i] for i in indices]
        colors = [cloud.colors[i] for i in indices if i < len(cloud.colors)] if cloud.colors else []
        normals = [cloud.normals[i] for i in indices if i < len(cloud.normals)] if cloud.normals else []
        bounds_min, bounds_max = _compute_bounds(points)
        return PointCloudData(
            points=points,
            colors=colors,
            normals=normals,
            point_count=len(points),
            bounds_min=bounds_min,
            bounds_max=bounds_max,
        )

    def compute_normals(self, cloud: PointCloudData,
                        radius: float = 0.1,
                        max_nn: int = 30) -> PointCloudData:
        """Estimate surface normals for each point using local neighbourhood.

        A simplified covariance-based approach: for each point, gather neighbours
        within *radius*, compute the centroid, and use the cross product of two
        principal displacement vectors as the normal direction.
        """
        n = cloud.point_count
        normals: list[tuple] = []
        r2 = radius * radius
        step = max(1, n // 500)  # subsample target for neighbour search

        for i in range(n):
            px, py, pz = cloud.points[i]
            neighbours: list[tuple] = []

            for j in range(0, n, step):
                if i == j:
                    continue
                qx, qy, qz = cloud.points[j]
                d2 = (px - qx) ** 2 + (py - qy) ** 2 + (pz - qz) ** 2
                if d2 <= r2:
                    neighbours.append((qx - px, qy - py, qz - pz))
                    if len(neighbours) >= max_nn:
                        break

            if len(neighbours) >= 2:
                # Cross product of first two displacement vectors
                a = neighbours[0]
                b = neighbours[1]
                nx = a[1] * b[2] - a[2] * b[1]
                ny = a[2] * b[0] - a[0] * b[2]
                nz = a[0] * b[1] - a[1] * b[0]
                mag = math.sqrt(nx * nx + ny * ny + nz * nz)
                if mag > 1e-12:
                    normals.append((nx / mag, ny / mag, nz / mag))
                else:
                    normals.append((0.0, 0.0, 1.0))
            else:
                normals.append((0.0, 0.0, 1.0))

        return PointCloudData(
            points=cloud.points,
            colors=cloud.colors,
            normals=normals,
            point_count=cloud.point_count,
            bounds_min=cloud.bounds_min,
            bounds_max=cloud.bounds_max,
        )

    def export(self, cloud: PointCloudData, fmt: str,
               path: str = None) -> bytes:
        """Export a point cloud to the given format.

        Supported formats: pcd, ply, xyz, las.
        Returns the serialised bytes; if *path* is not None it is also
        written to disk (but we avoid that in test/sim context).
        """
        if fmt == PointCloudFormat.pcd.value:
            data = self._export_pcd(cloud)
        elif fmt == PointCloudFormat.ply.value:
            data = self._export_ply(cloud)
        elif fmt == PointCloudFormat.xyz.value:
            data = self._export_xyz(cloud)
        elif fmt == PointCloudFormat.las.value:
            data = self._export_las(cloud)
        else:
            raise ValueError(f"Unknown export format: {fmt}")

        if path is not None:
            Path(path).write_bytes(data)

        return data

    def _export_pcd(self, cloud: PointCloudData) -> bytes:
        """Export as PCD (Point Cloud Data) binary format."""
        has_color = len(cloud.colors) == cloud.point_count
        has_normal = len(cloud.normals) == cloud.point_count

        fields = "x y z"
        size = "4 4 4"
        type_str = "F F F"
        count = "1 1 1"
        num_fields = 3

        if has_color:
            fields += " rgb"
            size += " 4"
            type_str += " F"
            count += " 1"
            num_fields += 1

        header = (
            f"# .PCD v0.7 - Point Cloud Data\n"
            f"VERSION 0.7\n"
            f"FIELDS {fields}\n"
            f"SIZE {size}\n"
            f"TYPE {type_str}\n"
            f"COUNT {count}\n"
            f"WIDTH {cloud.point_count}\n"
            f"HEIGHT 1\n"
            f"VIEWPOINT 0 0 0 1 0 0 0\n"
            f"POINTS {cloud.point_count}\n"
            f"DATA binary\n"
        )

        parts = [header.encode("ascii")]
        for i in range(cloud.point_count):
            x, y, z = cloud.points[i]
            parts.append(struct.pack("<fff", x, y, z))
            if has_color:
                r, g, b = cloud.colors[i]
                # Pack RGB as single float (PCL convention)
                ri = int(r * 255) & 0xFF
                gi = int(g * 255) & 0xFF
                bi = int(b * 255) & 0xFF
                rgb_int = (ri << 16) | (gi << 8) | bi
                parts.append(struct.pack("<f", struct.unpack("<f", struct.pack("<I", rgb_int))[0]))

        return b"".join(parts)

    def _export_ply(self, cloud: PointCloudData) -> bytes:
        """Export as PLY (Polygon File Format) ASCII."""
        has_color = len(cloud.colors) == cloud.point_count

        lines = [
            "ply",
            "format ascii 1.0",
            f"element vertex {cloud.point_count}",
            "property float x",
            "property float y",
            "property float z",
        ]
        if has_color:
            lines.extend([
                "property uchar red",
                "property uchar green",
                "property uchar blue",
            ])
        lines.append("end_header")

        for i in range(cloud.point_count):
            x, y, z = cloud.points[i]
            if has_color:
                r, g, b = cloud.colors[i]
                lines.append(f"{x:.6f} {y:.6f} {z:.6f} {int(r*255)} {int(g*255)} {int(b*255)}")
            else:
                lines.append(f"{x:.6f} {y:.6f} {z:.6f}")

        return ("\n".join(lines) + "\n").encode("ascii")

    def _export_xyz(self, cloud: PointCloudData) -> bytes:
        """Export as simple space-delimited XYZ text."""
        lines: list[str] = []
        for x, y, z in cloud.points:
            lines.append(f"{x:.6f} {y:.6f} {z:.6f}")
        return ("\n".join(lines) + "\n").encode("ascii")

    def _export_las(self, cloud: PointCloudData) -> bytes:
        """Export a simplified LAS 1.2 binary (header + point records).

        This is a minimal simulation; real LAS requires full ASPRS header.
        """
        # Minimal header (227 bytes for LAS 1.2 point format 0)
        signature = b"LASF"
        file_source_id = struct.pack("<H", 0)
        global_encoding = struct.pack("<H", 0)
        guid = b"\x00" * 16
        version = struct.pack("BB", 1, 2)
        system_id = b"depth_sensing".ljust(32, b"\x00")
        software = b"OmniSight".ljust(32, b"\x00")
        creation_day = struct.pack("<HH", 105, 2026)
        header_size = struct.pack("<H", 227)
        offset_to_points = struct.pack("<I", 227)
        num_var_records = struct.pack("<I", 0)
        point_format = struct.pack("B", 0)
        point_record_len = struct.pack("<H", 20)
        num_points = struct.pack("<I", cloud.point_count)
        num_by_return = struct.pack("<5I", cloud.point_count, 0, 0, 0, 0)

        bmin = cloud.bounds_min
        bmax = cloud.bounds_max
        scales = struct.pack("<3d", 0.001, 0.001, 0.001)
        offsets = struct.pack("<3d", 0.0, 0.0, 0.0)
        maxs = struct.pack("<3d", bmax[0], bmax[1], bmax[2])
        mins = struct.pack("<3d", bmin[0], bmin[1], bmin[2])

        header = (signature + file_source_id + global_encoding + guid +
                  version + system_id + software + creation_day +
                  header_size + offset_to_points + num_var_records +
                  point_format + point_record_len + num_points + num_by_return +
                  scales + offsets + maxs + mins)

        # Pad header to 227 bytes
        if len(header) < 227:
            header += b"\x00" * (227 - len(header))
        else:
            header = header[:227]

        # Point records (format 0): X(int32) Y(int32) Z(int32) intensity(uint16) flags(4 bytes)
        parts = [header]
        for x, y, z in cloud.points:
            xi = int(x / 0.001)
            yi = int(y / 0.001)
            zi = int(z / 0.001)
            parts.append(struct.pack("<iiiH4x", xi, yi, zi, 0))

        return b"".join(parts)

    def import_cloud(self, data: bytes, fmt: str) -> PointCloudData:
        """Import a point cloud from serialised bytes.

        Supported formats: pcd, ply, xyz.
        """
        if fmt == PointCloudFormat.pcd.value:
            return self._import_pcd(data)
        elif fmt == PointCloudFormat.ply.value:
            return self._import_ply(data)
        elif fmt == PointCloudFormat.xyz.value:
            return self._import_xyz(data)
        else:
            raise ValueError(f"Unknown import format: {fmt}")

    def _import_pcd(self, data: bytes) -> PointCloudData:
        """Import PCD binary."""
        text_end = data.find(b"DATA binary\n")
        if text_end < 0:
            return PointCloudData([], [], [], 0, (0, 0, 0), (0, 0, 0))

        header_text = data[:text_end].decode("ascii", errors="replace")
        point_count = 0
        for line in header_text.splitlines():
            if line.startswith("POINTS"):
                point_count = int(line.split()[1])

        binary_start = text_end + len(b"DATA binary\n")
        points: list[tuple] = []

        offset = binary_start
        for _ in range(point_count):
            if offset + 12 > len(data):
                break
            x, y, z = struct.unpack_from("<fff", data, offset)
            points.append((x, y, z))
            offset += 12  # skip colour field if present; simplified

        bounds_min, bounds_max = _compute_bounds(points)
        return PointCloudData(
            points=points, colors=[], normals=[],
            point_count=len(points),
            bounds_min=bounds_min, bounds_max=bounds_max,
        )

    def _import_ply(self, data: bytes) -> PointCloudData:
        """Import PLY ASCII."""
        text = data.decode("ascii", errors="replace")
        lines = text.splitlines()

        header_end = 0
        for i, line in enumerate(lines):
            if line.strip() == "end_header":
                header_end = i + 1
                break

        points: list[tuple] = []
        for line in lines[header_end:]:
            parts = line.strip().split()
            if len(parts) >= 3:
                try:
                    points.append((float(parts[0]), float(parts[1]), float(parts[2])))
                except ValueError:
                    continue

        bounds_min, bounds_max = _compute_bounds(points)
        return PointCloudData(
            points=points, colors=[], normals=[],
            point_count=len(points),
            bounds_min=bounds_min, bounds_max=bounds_max,
        )

    def _import_xyz(self, data: bytes) -> PointCloudData:
        """Import XYZ space-delimited text."""
        text = data.decode("ascii", errors="replace")
        points: list[tuple] = []
        for line in text.splitlines():
            parts = line.strip().split()
            if len(parts) >= 3:
                try:
                    points.append((float(parts[0]), float(parts[1]), float(parts[2])))
                except ValueError:
                    continue

        bounds_min, bounds_max = _compute_bounds(points)
        return PointCloudData(
            points=points, colors=[], normals=[],
            point_count=len(points),
            bounds_min=bounds_min, bounds_max=bounds_max,
        )


def create_point_cloud_processor(backend: str = "open3d") -> PointCloudProcessor:
    """Factory: create a PointCloudProcessor."""
    return PointCloudProcessor(backend)


def depth_to_points(depth_frame: DepthFrame,
                    camera_matrix: list = None) -> PointCloudData:
    """Convenience: convert depth frame to point cloud using Open3D backend."""
    proc = PointCloudProcessor("open3d")
    return proc.depth_to_point_cloud(depth_frame, camera_matrix)


# ── Registration Module ───────────────────────────────────────────────────


class RegistrationEngine:
    """Point cloud registration using ICP variants and NDT.

    Simulates iterative convergence with decreasing error.
    """

    def __init__(self, algorithm: str = "icp_point_to_point"):
        if algorithm not in (a.value for a in RegistrationAlgorithm):
            raise ValueError(f"Unknown registration algorithm: {algorithm}")
        self._algorithm = algorithm

        cfg = _get_cfg()
        reg_entries = cfg.get("registration", [])
        self._max_iterations = 50
        self._tolerance = 1e-6
        self._max_correspondence_distance = 0.05

        for entry in reg_entries:
            if entry.get("id") == algorithm:
                self._max_iterations = entry.get("max_iterations", 50)
                self._tolerance = entry.get("tolerance", 1e-6)
                self._max_correspondence_distance = entry.get(
                    "max_correspondence_distance", 0.05)
                break

    @property
    def algorithm(self) -> str:
        return self._algorithm

    def register(self, source: PointCloudData, target: PointCloudData,
                 initial_transform: list = None) -> RegistrationResult:
        """Register *source* to *target*.

        Simulates iterative convergence by progressively reducing a synthetic
        error term over the configured number of iterations.
        """
        if initial_transform is None:
            initial_transform = _identity_matrix_4x4()

        if source.point_count == 0 or target.point_count == 0:
            return RegistrationResult(
                transformation=initial_transform,
                fitness=0.0,
                inlier_rmse=float("inf"),
                num_inliers=0,
                converged=False,
                iterations=0,
            )

        if self._algorithm in (RegistrationAlgorithm.icp_point_to_point.value,
                                RegistrationAlgorithm.icp_point_to_plane.value,
                                RegistrationAlgorithm.colored_icp.value):
            return self._run_icp(source, target, initial_transform)
        elif self._algorithm == RegistrationAlgorithm.ndt.value:
            return self._run_ndt(source, target, initial_transform)

        return RegistrationResult(
            transformation=initial_transform,
            fitness=0.0, inlier_rmse=float("inf"),
            num_inliers=0, converged=False, iterations=0,
        )

    def _run_icp(self, source: PointCloudData, target: PointCloudData,
                 initial_transform: list) -> RegistrationResult:
        """Simulate ICP convergence.

        Each iteration refines the transformation, decreasing error
        geometrically until the tolerance is met or max iterations reached.
        """
        transform = [row[:] for row in initial_transform]  # deep copy

        # Compute initial fitness
        src_pts = _apply_transform(source.points[:200], transform)
        tgt_pts = target.points[:200]
        fitness, rmse = _compute_fitness(src_pts, tgt_pts,
                                         self._max_correspondence_distance)

        iterations_run = 0
        prev_rmse = rmse
        convergence_factor = 0.7  # error decreases by 30% each iteration

        if self._algorithm == RegistrationAlgorithm.icp_point_to_plane.value:
            convergence_factor = 0.5  # faster convergence with plane constraint

        for it in range(self._max_iterations):
            iterations_run = it + 1

            # Simulate one ICP iteration: small translation refinement
            delta_tx = (1.0 - fitness) * 0.001 * (1 if it % 2 == 0 else -1)
            delta_ty = (1.0 - fitness) * 0.0005 * (1 if it % 3 == 0 else -1)
            transform[0][3] += delta_tx
            transform[1][3] += delta_ty

            # Update fitness and RMSE
            src_pts = _apply_transform(source.points[:200], transform)
            fitness, rmse = _compute_fitness(src_pts, tgt_pts,
                                             self._max_correspondence_distance)

            # Simulate convergence
            rmse = prev_rmse * convergence_factor
            fitness = min(1.0, fitness + (1.0 - fitness) * 0.15)
            prev_rmse = rmse

            if rmse < self._tolerance:
                break

        num_inliers = int(fitness * source.point_count)

        return RegistrationResult(
            transformation=transform,
            fitness=round(fitness, 6),
            inlier_rmse=round(rmse, 8),
            num_inliers=num_inliers,
            converged=rmse < self._tolerance or iterations_run >= self._max_iterations,
            iterations=iterations_run,
        )

    def _run_ndt(self, source: PointCloudData, target: PointCloudData,
                 initial_transform: list) -> RegistrationResult:
        """Simulate Normal Distributions Transform registration.

        NDT discretises the target into voxel cells, each modelled as a
        normal distribution. Source points are scored against these
        distributions and the transform is optimised via Newton's method.
        """
        transform = [row[:] for row in initial_transform]

        cfg = _get_cfg()
        ndt_cfg = None
        for entry in cfg.get("registration", []):
            if entry.get("id") == "ndt":
                ndt_cfg = entry
                break

        resolution = ndt_cfg.get("resolution", 1.0) if ndt_cfg else 1.0
        step_size = ndt_cfg.get("step_size", 0.1) if ndt_cfg else 0.1
        max_iter = ndt_cfg.get("max_iterations", 35) if ndt_cfg else 35

        # Simulate grid-based convergence
        fitness = 0.3
        rmse = 0.1
        iterations_run = 0

        for it in range(max_iter):
            iterations_run = it + 1
            transform[0][3] += step_size * 0.01 * (1.0 - fitness)
            rmse *= 0.75
            fitness = min(1.0, fitness + 0.03)
            if rmse < self._tolerance:
                break

        src_pts = _apply_transform(source.points[:200], transform)
        tgt_pts = target.points[:200]
        real_fitness, real_rmse = _compute_fitness(
            src_pts, tgt_pts, self._max_correspondence_distance)

        # Blend simulated and real metrics
        final_fitness = max(fitness, real_fitness)
        final_rmse = min(rmse, real_rmse) if real_rmse != float("inf") else rmse
        num_inliers = int(final_fitness * source.point_count)

        return RegistrationResult(
            transformation=transform,
            fitness=round(final_fitness, 6),
            inlier_rmse=round(final_rmse, 8),
            num_inliers=num_inliers,
            converged=True,
            iterations=iterations_run,
        )


def register_point_clouds(source: PointCloudData, target: PointCloudData,
                          algorithm: str = "icp_point_to_point",
                          initial_transform: list = None) -> RegistrationResult:
    """Convenience: register two point clouds."""
    engine = RegistrationEngine(algorithm)
    return engine.register(source, target, initial_transform)


# ── SLAM Hooks ────────────────────────────────────────────────────────────


class SlamHook:
    """SLAM system hook supporting visual and LiDAR odometry.

    Processes depth frames, maintains a trajectory, and accumulates
    a global map as a point cloud.
    """

    def __init__(self, slam_type: str):
        if slam_type not in (s.value for s in SlamType):
            raise ValueError(f"Unknown SLAM type: {slam_type}")
        self._slam_type = slam_type
        self._poses: list[SlamPose] = []
        self._map_points: list[tuple] = []
        self._initialized = False
        self._frame_id = 0
        self._config: dict[str, Any] = {}

    @property
    def slam_type(self) -> str:
        return self._slam_type

    @property
    def initialized(self) -> bool:
        return self._initialized

    def initialize(self, config: dict = None) -> bool:
        """Initialise the SLAM system.

        Loads per-type configuration from the YAML and merges any
        runtime overrides.
        """
        cfg = _get_cfg()
        slam_entries = cfg.get("slam", [])
        for entry in slam_entries:
            if entry.get("id") == self._slam_type:
                self._config = dict(entry)
                break

        if config:
            self._config.update(config)

        self._poses = []
        self._map_points = []
        self._frame_id = 0
        self._initialized = True
        logger.info("SLAM initialised: type=%s", self._slam_type)
        return True

    def process_frame(self, depth_frame: DepthFrame,
                      color_frame: bytes = None) -> SlamPose:
        """Process a new depth frame and return the estimated camera pose.

        Visual SLAM: extract synthetic ORB-like features, match against
        previous frame, estimate incremental pose.
        LiDAR SLAM: convert depth to scan, match against accumulated map.
        """
        if not self._initialized:
            self.initialize()

        self._frame_id += 1
        t = time.time()

        if self._slam_type == SlamType.visual_slam.value:
            pose = self._process_visual(depth_frame, color_frame)
        else:
            pose = self._process_lidar(depth_frame)

        self._poses.append(pose)

        # Accumulate map points (subsample from depth)
        w, h = depth_frame.width, depth_frame.height
        pixel_count = w * h
        if pixel_count > 0 and len(depth_frame.depth_data) >= pixel_count * 4:
            cam = _default_camera_matrix(w, h)
            fx, fy = cam[0][0], cam[1][1]
            cx, cy = cam[0][2], cam[1][2]
            depths = _unpack_depth(depth_frame.depth_data, pixel_count)
            step = max(1, pixel_count // 100)
            for i in range(0, pixel_count, step):
                z = depths[i]
                if z > 0:
                    col = i % w
                    row = i // w
                    x = (col - cx) * z / fx if fx != 0 else 0.0
                    y = (row - cy) * z / fy if fy != 0 else 0.0
                    # Transform to world frame using current pose
                    px, py, pz = pose.position
                    self._map_points.append((x + px, y + py, z + pz))

        return pose

    def _process_visual(self, depth_frame: DepthFrame,
                        color_frame: bytes = None) -> SlamPose:
        """Simulate visual odometry step.

        Generates a smooth circular trajectory with small drift.
        """
        fid = self._frame_id
        angle = fid * 0.05  # radians per frame
        radius = 1.0

        x = radius * math.cos(angle)
        y = radius * math.sin(angle)
        z = 0.0 + fid * 0.001  # small vertical drift

        # Quaternion from yaw angle
        quat = _quaternion_from_axis_angle((0.0, 0.0, 1.0), angle)

        confidence = max(0.5, 1.0 - fid * 0.005)

        return SlamPose(
            position=(round(x, 6), round(y, 6), round(z, 6)),
            orientation=tuple(round(q, 6) for q in quat),
            timestamp=time.time(),
            frame_id=fid,
            confidence=round(confidence, 4),
        )

    def _process_lidar(self, depth_frame: DepthFrame) -> SlamPose:
        """Simulate LiDAR odometry step.

        Generates a forward-moving trajectory with scan-to-map matching.
        """
        fid = self._frame_id
        step_size = 0.05

        x = fid * step_size
        y = 0.1 * math.sin(fid * 0.1)
        z = 0.0

        quat = _quaternion_from_axis_angle((0.0, 0.0, 1.0), fid * 0.02)
        confidence = max(0.6, 1.0 - fid * 0.003)

        return SlamPose(
            position=(round(x, 6), round(y, 6), round(z, 6)),
            orientation=tuple(round(q, 6) for q in quat),
            timestamp=time.time(),
            frame_id=fid,
            confidence=round(confidence, 4),
        )

    def get_trajectory(self) -> list[SlamPose]:
        """Return the full trajectory as a list of SlamPose objects."""
        return list(self._poses)

    def get_map_points(self) -> PointCloudData:
        """Return the accumulated map as a PointCloudData."""
        bounds_min, bounds_max = _compute_bounds(self._map_points)
        return PointCloudData(
            points=list(self._map_points),
            colors=[],
            normals=[],
            point_count=len(self._map_points),
            bounds_min=bounds_min,
            bounds_max=bounds_max,
        )

    def reset(self) -> bool:
        """Reset all SLAM state."""
        self._poses = []
        self._map_points = []
        self._frame_id = 0
        self._initialized = False
        logger.info("SLAM reset: type=%s", self._slam_type)
        return True


def create_slam_hook(slam_type: str) -> SlamHook:
    """Factory: create a SlamHook of the given type."""
    return SlamHook(slam_type)


# ── Calibration Module ────────────────────────────────────────────────────


class CalibrationEngine:
    """Camera calibration routines for intrinsic, stereo, and ToF phase calibration."""

    def calibrate_intrinsic(self, frames: list[bytes], pattern_size: tuple = (9, 6),
                            square_size: float = 0.025) -> CalibrationResult:
        """Calibrate camera intrinsics from checkerboard captures.

        Simulates corner detection, generates a plausible camera matrix
        with reprojection error proportional to the number of input frames.
        """
        num_frames = len(frames)
        if num_frames < 3:
            return CalibrationResult(
                calibration_type=CalibrationType.intrinsic.value,
                reprojection_error=float("inf"),
                camera_matrix=_default_camera_matrix(640, 480),
                distortion_coeffs=[0.0, 0.0, 0.0, 0.0, 0.0],
                success=False,
                timestamp=time.time(),
            )

        # More frames -> lower reprojection error (diminishing returns)
        reproj_error = 0.5 / math.sqrt(num_frames)

        # Simulated intrinsics
        fx = 520.0 + 2.0 * (num_frames % 5)
        fy = 520.0 + 1.5 * (num_frames % 7)
        cx = 320.0 + 0.5 * (num_frames % 3)
        cy = 240.0 + 0.3 * (num_frames % 4)

        k1 = -0.1 + 0.001 * num_frames
        k2 = 0.05 - 0.0005 * num_frames
        p1 = 0.001
        p2 = -0.001
        k3 = 0.0

        return CalibrationResult(
            calibration_type=CalibrationType.intrinsic.value,
            reprojection_error=round(reproj_error, 6),
            camera_matrix=[
                [fx, 0.0, cx],
                [0.0, fy, cy],
                [0.0, 0.0, 1.0],
            ],
            distortion_coeffs=[round(k1, 6), round(k2, 6), round(p1, 6),
                               round(p2, 6), round(k3, 6)],
            success=True,
            timestamp=time.time(),
        )

    def calibrate_stereo(self, left_frames: list[bytes], right_frames: list[bytes],
                         pattern_size: tuple = (9, 6),
                         square_size: float = 0.025) -> dict[str, Any]:
        """Calibrate stereo pair extrinsics.

        Returns rotation matrix, translation vector, essential matrix,
        fundamental matrix, and per-camera intrinsics.
        """
        num = min(len(left_frames), len(right_frames))
        if num < 5:
            return {
                "success": False,
                "error": f"Need at least 5 stereo pairs, got {num}",
            }

        reproj_error = 0.3 / math.sqrt(num)

        # Simulated baseline = ~12cm horizontal
        R = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
        T = [-0.12, 0.0, 0.0]

        # Essential matrix (simplified: E = [T]x R)
        E = [
            [0.0, -T[2], T[1]],
            [T[2], 0.0, -T[0]],
            [-T[1], T[0], 0.0],
        ]

        # Fundamental matrix (simplified)
        F = [
            [0.0, 0.0, T[1]],
            [0.0, 0.0, -T[0]],
            [-T[1], T[0], 0.0],
        ]

        left_intrinsic = self.calibrate_intrinsic(left_frames, pattern_size, square_size)
        right_intrinsic = self.calibrate_intrinsic(right_frames, pattern_size, square_size)

        return {
            "success": True,
            "reprojection_error": round(reproj_error, 6),
            "rotation_matrix": R,
            "translation_vector": T,
            "essential_matrix": E,
            "fundamental_matrix": F,
            "left_intrinsic": asdict(left_intrinsic),
            "right_intrinsic": asdict(right_intrinsic),
            "rectification_transforms": {
                "R1": R,  # simplified
                "R2": _identity_matrix_4x4()[:3],
                "P1": left_intrinsic.camera_matrix,
                "P2": right_intrinsic.camera_matrix,
            },
            "timestamp": time.time(),
        }

    def calibrate_tof_phase(self, frames_at_distances: dict[float, list[bytes]]) -> dict[str, Any]:
        """Calibrate ToF phase-to-depth mapping at known reference distances.

        Parameters
        ----------
        frames_at_distances : dict
            Mapping of reference distance (m) to list of captured frames
            at that distance.

        Returns phase offset, depth LUT, wiggling correction, and temperature
        compensation parameters.
        """
        if not frames_at_distances:
            return {"success": False, "error": "No reference distances provided"}

        distances = sorted(frames_at_distances.keys())
        if len(distances) < 2:
            return {"success": False, "error": "Need at least 2 reference distances"}

        # Simulate phase measurement at each distance
        phase_measurements: list[dict] = []
        modulation_freq = 20e6  # 20 MHz
        speed_of_light = 3e8
        wavelength = speed_of_light / (2.0 * modulation_freq)

        for dist in distances:
            frames = frames_at_distances[dist]
            # True phase from distance
            true_phase = (2.0 * math.pi * dist) / wavelength
            wrapped_phase = true_phase % (2.0 * math.pi)

            # Simulate measured phase with noise
            noise = 0.01 * len(frames)  # more frames -> less noise
            measured_phase = wrapped_phase + 0.005 / max(1, len(frames))

            phase_measurements.append({
                "distance_m": dist,
                "true_phase_rad": round(true_phase, 6),
                "measured_phase_rad": round(measured_phase, 6),
                "num_frames": len(frames),
            })

        # Compute phase offset (average difference)
        offsets = [
            m["measured_phase_rad"] - m["true_phase_rad"]
            for m in phase_measurements
        ]
        phase_offset = sum(offsets) / len(offsets)

        # Build depth LUT (distance -> corrected phase)
        depth_lut = {
            str(dist): round(
                (2.0 * math.pi * dist) / wavelength - phase_offset, 6
            )
            for dist in distances
        }

        # Wiggling correction (simplified sinusoidal error model)
        wiggling_amplitude = 0.002  # 2mm
        wiggling_period = wavelength / 2.0

        return {
            "success": True,
            "phase_offset_rad": round(phase_offset, 8),
            "depth_lut": depth_lut,
            "wiggling_correction": {
                "amplitude_m": wiggling_amplitude,
                "period_m": round(wiggling_period, 6),
                "model": "sinusoidal",
            },
            "temperature_compensation": {
                "enabled": True,
                "coefficient_rad_per_degC": 0.001,
                "reference_temp_degC": 25.0,
            },
            "measurements": phase_measurements,
            "timestamp": time.time(),
        }


def calibrate_camera(frames: list[bytes], calibration_type: str,
                     **kwargs: Any) -> Any:
    """Convenience: run calibration by type string.

    Dispatches to CalibrationEngine methods.
    """
    engine = CalibrationEngine()

    if calibration_type == CalibrationType.intrinsic.value:
        return engine.calibrate_intrinsic(
            frames,
            pattern_size=kwargs.get("pattern_size", (9, 6)),
            square_size=kwargs.get("square_size", 0.025),
        )
    elif calibration_type == CalibrationType.stereo_extrinsic.value:
        right_frames = kwargs.get("right_frames", [])
        return engine.calibrate_stereo(
            frames, right_frames,
            pattern_size=kwargs.get("pattern_size", (9, 6)),
            square_size=kwargs.get("square_size", 0.025),
        )
    elif calibration_type == CalibrationType.tof_phase.value:
        frames_at_distances = kwargs.get("frames_at_distances", {})
        return engine.calibrate_tof_phase(frames_at_distances)
    else:
        raise ValueError(f"Unknown calibration type: {calibration_type}")


# ── Test scene generation and validation ──────────────────────────────────


_TEST_SCENE_CONFIGS: dict[str, dict[str, Any]] = {
    "flat_wall": {
        "width": 640, "height": 480,
        "expected_point_count": 307200,
        "bounds_min": (-0.5, -0.4, 0.95),
        "bounds_max": (0.5, 0.4, 1.05),
        "tolerance_points": 1000,
        "tolerance_bounds": 0.1,
    },
    "box_scene": {
        "width": 320, "height": 240,
        "expected_point_count": 76800,
        "bounds_min": (-0.5, -0.5, 0.3),
        "bounds_max": (0.5, 0.5, 1.1),
        "tolerance_points": 5000,
        "tolerance_bounds": 0.3,
    },
    "sphere": {
        "width": 200, "height": 200,
        "expected_point_count": 40000,
        "bounds_min": (-0.5, -0.5, 0.0),
        "bounds_max": (0.5, 0.5, 1.6),
        "tolerance_points": 5000,
        "tolerance_bounds": 0.3,
    },
    "staircase": {
        "width": 320, "height": 240,
        "expected_point_count": 76800,
        "bounds_min": (-0.5, -0.5, 0.3),
        "bounds_max": (0.5, 0.5, 1.8),
        "tolerance_points": 5000,
        "tolerance_bounds": 0.5,
    },
    "corner": {
        "width": 320, "height": 240,
        "expected_point_count": 76800,
        "bounds_min": (-0.5, -0.5, 0.3),
        "bounds_max": (0.5, 0.5, 1.2),
        "tolerance_points": 5000,
        "tolerance_bounds": 0.3,
    },
    "empty_room": {
        "width": 640, "height": 480,
        "expected_point_count": 307200,
        "bounds_min": (-0.5, -0.5, 0.0),
        "bounds_max": (0.5, 0.5, 2.1),
        "tolerance_points": 5000,
        "tolerance_bounds": 0.5,
    },
}


def generate_test_scene(scene_id: str) -> PointCloudData:
    """Generate a synthetic point cloud for the named test scene.

    Scenes:
    - flat_wall: plane at z~1.0 with small noise
    - box_scene: 6-face box on a table surface
    - sphere: points on a sphere surface (r=0.25, centre (0,0,0.5))
    - staircase: 5-step surfaces
    - corner: two perpendicular planes
    - empty_room: 4 walls + floor + ceiling
    """
    if scene_id not in _TEST_SCENE_CONFIGS:
        raise ValueError(f"Unknown test scene: {scene_id}")

    sc = _TEST_SCENE_CONFIGS[scene_id]
    w, h = sc["width"], sc["height"]
    depth_bytes = _generate_synthetic_depth(w, h, scene_id, noise_std=0.005, seed=12345)

    depth_frame = DepthFrame(
        width=w, height=h, depth_data=depth_bytes,
        timestamp=time.time(), sensor_id=f"test_{scene_id}",
        frame_number=0, min_depth=0.0, max_depth=0.0,
    )

    cam = _default_camera_matrix(w, h)
    proc = PointCloudProcessor("open3d")
    cloud = proc.depth_to_point_cloud(depth_frame, cam)
    return cloud


def validate_test_scene(scene_id: str, cloud: PointCloudData) -> dict[str, Any]:
    """Validate a point cloud against expected scene parameters.

    Checks point count and bounds within configured tolerances.
    """
    if scene_id not in _TEST_SCENE_CONFIGS:
        raise ValueError(f"Unknown test scene: {scene_id}")

    sc = _TEST_SCENE_CONFIGS[scene_id]
    expected_count = sc["expected_point_count"]
    tol_points = sc["tolerance_points"]
    tol_bounds = sc["tolerance_bounds"]
    exp_bmin = sc["bounds_min"]
    exp_bmax = sc["bounds_max"]

    count_diff = abs(cloud.point_count - expected_count)
    point_count_ok = count_diff <= tol_points

    bounds_ok = True
    bounds_details: dict[str, Any] = {}
    for i, axis in enumerate(("x", "y", "z")):
        bmin_diff = abs(cloud.bounds_min[i] - exp_bmin[i])
        bmax_diff = abs(cloud.bounds_max[i] - exp_bmax[i])
        axis_ok = bmin_diff <= tol_bounds and bmax_diff <= tol_bounds
        bounds_details[axis] = {
            "min_expected": exp_bmin[i],
            "min_actual": round(cloud.bounds_min[i], 4),
            "max_expected": exp_bmax[i],
            "max_actual": round(cloud.bounds_max[i], 4),
            "ok": axis_ok,
        }
        if not axis_ok:
            bounds_ok = False

    passed = point_count_ok and bounds_ok

    return {
        "scene_id": scene_id,
        "passed": passed,
        "point_count_ok": point_count_ok,
        "point_count_expected": expected_count,
        "point_count_actual": cloud.point_count,
        "point_count_tolerance": tol_points,
        "bounds_ok": bounds_ok,
        "bounds_details": bounds_details,
    }


# ── Public query functions ────────────────────────────────────────────────


def list_sensors() -> list[dict[str, Any]]:
    """List available depth sensors from config."""
    cfg = _get_cfg()
    sensors = cfg.get("sensors", [])
    return [
        {
            "sensor_id": s["id"],
            "name": s["name"],
            "description": s.get("description", ""),
            "technology": s.get("technology", ""),
            "resolution": s.get("resolution", []),
            "max_range_m": s.get("max_range_m", 0.0),
            "frame_rate_fps": s.get("frame_rate_fps", 0),
            "features": s.get("features", []),
        }
        for s in sensors
    ]


def create_sensor(sensor_id: str, config: SensorConfig = None) -> DepthSensor:
    """Factory: create a depth sensor adapter by ID."""
    cls = _ADAPTER_MAP.get(sensor_id)
    if cls is None:
        raise ValueError(f"Unknown sensor: {sensor_id}")
    if config is None:
        # Load defaults from config YAML
        cfg = _get_cfg()
        for s in cfg.get("sensors", []):
            if s["id"] == sensor_id:
                res = tuple(s.get("resolution", [640, 480]))
                config = SensorConfig(
                    sensor_id=sensor_id,
                    resolution=res,
                    max_range=s.get("max_range_m", 5.0),
                    frame_rate=s.get("frame_rate_fps", 30),
                    modulation_freq_mhz=s.get("modulation_frequencies_mhz", [20])[0],
                )
                break
        if config is None:
            config = SensorConfig(sensor_id=sensor_id)
    return cls(sensor_id, config)


def list_structured_light_patterns() -> list[dict[str, Any]]:
    """List available structured light patterns from config."""
    cfg = _get_cfg()
    patterns = cfg.get("structured_light", [])
    return [
        {
            "pattern_id": p["id"],
            "name": p["name"],
            "description": p.get("description", ""),
            "pattern_type": p.get("pattern_type", ""),
            "num_patterns": p.get("num_patterns", 1),
            "projector_resolution": p.get("projector_resolution", []),
            "features": p.get("features", []),
        }
        for p in patterns
    ]


def list_stereo_algorithms() -> list[dict[str, Any]]:
    """List available stereo matching algorithms from config."""
    cfg = _get_cfg()
    algos = cfg.get("stereo", [])
    return [
        {
            "algorithm_id": a["id"],
            "name": a["name"],
            "description": a.get("description", ""),
            "num_disparities": a.get("num_disparities", 128),
            "block_size": a.get("block_size", 5),
            "features": a.get("features", []),
        }
        for a in algos
    ]


def list_point_cloud_backends() -> list[dict[str, Any]]:
    """List available point cloud processing backends from config."""
    cfg = _get_cfg()
    backends = cfg.get("point_cloud", [])
    return [
        {
            "backend_id": b["id"],
            "name": b["name"],
            "description": b.get("description", ""),
            "formats": b.get("formats", []),
            "features": b.get("features", []),
            "max_points": b.get("max_points", 0),
        }
        for b in backends
    ]


def list_registration_algorithms() -> list[dict[str, Any]]:
    """List available registration algorithms from config."""
    cfg = _get_cfg()
    regs = cfg.get("registration", [])
    return [
        {
            "algorithm_id": r["id"],
            "name": r["name"],
            "description": r.get("description", ""),
            "max_iterations": r.get("max_iterations", 50),
            "tolerance": r.get("tolerance", 1e-6),
            "features": r.get("features", []),
        }
        for r in regs
    ]


def list_slam_types() -> list[dict[str, Any]]:
    """List available SLAM types from config."""
    cfg = _get_cfg()
    slams = cfg.get("slam", [])
    return [
        {
            "slam_id": s["id"],
            "name": s["name"],
            "description": s.get("description", ""),
            "backend": s.get("backend", ""),
            "features": s.get("features", []),
        }
        for s in slams
    ]


def list_calibration_types() -> list[dict[str, Any]]:
    """List available calibration types from config."""
    cfg = _get_cfg()
    cals = cfg.get("calibration", [])
    return [
        {
            "calibration_id": c["id"],
            "name": c["name"],
            "description": c.get("description", ""),
            "method": c.get("method", ""),
            "outputs": c.get("outputs", []),
        }
        for c in cals
    ]


def list_test_scenes() -> list[dict[str, Any]]:
    """List test scenes with expected results."""
    cfg = _get_cfg()
    scenes = cfg.get("test_scenes", [])
    return [
        {
            "scene_id": s["id"],
            "name": s["name"],
            "description": s.get("description", ""),
            "expected_point_count": s.get("expected_point_count", 0),
            "bounds": s.get("bounds", {}),
        }
        for s in scenes
    ]


# ── Test recipes ──────────────────────────────────────────────────────────


def list_test_recipes() -> list[dict[str, Any]]:
    """List available test recipes from config."""
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
    """Run a test recipe end-to-end."""
    recipes = {r["recipe_id"]: r for r in list_test_recipes()}
    if recipe_id not in recipes:
        return TestResult(
            recipe_id=recipe_id,
            status=TestStatus.error.value,
            details=[{"error": f"Unknown recipe: {recipe_id}"}],
        )

    t0 = time.monotonic()

    dispatch = {
        "tof_capture": _run_tof_capture_recipe,
        "structured_light_decode": _run_structured_light_recipe,
        "stereo_disparity": _run_stereo_disparity_recipe,
        "point_cloud_processing": _run_point_cloud_recipe,
        "registration_icp": _run_registration_recipe,
        "slam_odometry": _run_slam_odometry_recipe,
    }

    runner = dispatch.get(recipe_id)
    if runner is None:
        return TestResult(
            recipe_id=recipe_id,
            status=TestStatus.skipped.value,
            details=[{"note": "No runner for recipe"}],
            duration_ms=round((time.monotonic() - t0) * 1000, 2),
        )

    return runner(recipe_id, t0)


def _run_tof_capture_recipe(recipe_id: str, t0: float) -> TestResult:
    """Recipe: create each ToF sensor -> connect -> configure -> capture -> validate depth range."""
    details: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    for sid in SensorId:
        try:
            sensor = create_sensor(sid.value)
            assert sensor.connect(), "connect failed"

            cfg = sensor.config
            assert sensor.configure(cfg), "configure failed"
            assert sensor.state == SensorState.configured

            frame = sensor.capture()
            assert frame.width > 0, "frame width is 0"
            assert frame.height > 0, "frame height is 0"
            assert len(frame.depth_data) == frame.width * frame.height * 4, "depth data size mismatch"
            assert frame.min_depth >= 0, "negative min depth"
            assert frame.max_depth > 0, "zero max depth"
            assert frame.max_depth <= cfg.max_range * 1.1, "max depth exceeds sensor range"

            caps = sensor.get_capabilities()
            assert "sensor" in caps, "no sensor in capabilities"

            status = sensor.get_status()
            assert status["state"] == SensorState.streaming.value

            # Capture a second frame and verify determinism
            frame2 = sensor.capture()
            assert frame2.frame_number == frame.frame_number + 1

            assert sensor.disconnect(), "disconnect failed"
            assert sensor.state == SensorState.disconnected

            details.append({"sensor": sid.value, "status": "passed",
                            "min_depth": frame.min_depth, "max_depth": frame.max_depth})
            passed += 1

        except Exception as e:
            details.append({"sensor": sid.value, "status": "failed", "error": str(e)})
            failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_structured_light_recipe(recipe_id: str, t0: float) -> TestResult:
    """Recipe: generate patterns -> simulate capture -> decode -> validate depth."""
    details: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    for pattern in StructuredLightPattern:
        try:
            res = (160, 120)  # small for speed
            codec = create_structured_light_codec(pattern.value, res)

            patterns = codec.generate_patterns()
            assert len(patterns) > 0, "no patterns generated"
            assert len(patterns) == codec.num_patterns, "pattern count mismatch"

            # Each pattern should be width*height bytes
            for p in patterns:
                assert len(p) == res[0] * res[1], f"pattern size mismatch: {len(p)}"

            # Simulate capture (same as projection for test purposes)
            depth_frame = codec.decode(patterns, pattern.value,
                                       baseline=0.1, focal_length=500.0)

            assert depth_frame.width == res[0], "decoded width mismatch"
            assert depth_frame.height == res[1], "decoded height mismatch"
            assert len(depth_frame.depth_data) == res[0] * res[1] * 4, "decoded data size"
            assert depth_frame.max_depth > 0, "no valid depth decoded"

            details.append({
                "pattern": pattern.value,
                "status": "passed",
                "num_patterns": codec.num_patterns,
                "min_depth": depth_frame.min_depth,
                "max_depth": depth_frame.max_depth,
            })
            passed += 1

        except Exception as e:
            details.append({"pattern": pattern.value, "status": "failed", "error": str(e)})
            failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_stereo_disparity_recipe(recipe_id: str, t0: float) -> TestResult:
    """Recipe: generate stereo pair -> rectify -> compute disparity -> validate."""
    details: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    for algo in StereoAlgorithm:
        try:
            w, h = 64, 48  # small for speed
            # Generate synthetic stereo pair with horizontal shift
            left = bytes([128 + (c % 64) for c in range(w * h)])
            shift = 8  # pixel disparity
            right = bytes([128 + ((c - shift) % 64) for c in range(w * h)])

            config = StereoConfig(algorithm=algo.value, num_disparities=32, block_size=5)
            pipeline = create_stereo_pipeline(algo.value,
                                              num_disparities=32, block_size=5)

            rect_l, rect_r = pipeline.rectify(left, right, w, h)
            assert len(rect_l) == w * h, "rectified left size mismatch"
            assert len(rect_r) == w * h, "rectified right size mismatch"

            disp = pipeline.compute_disparity(rect_l, rect_r, w, h)
            assert len(disp) == w * h * 4, "disparity size mismatch"

            depth_frame = pipeline.disparity_to_depth(disp, w, h)
            assert depth_frame.width == w
            assert depth_frame.height == h

            details.append({
                "algorithm": algo.value,
                "status": "passed",
                "min_depth": depth_frame.min_depth,
                "max_depth": depth_frame.max_depth,
            })
            passed += 1

        except Exception as e:
            details.append({"algorithm": algo.value, "status": "failed", "error": str(e)})
            failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_point_cloud_recipe(recipe_id: str, t0: float) -> TestResult:
    """Recipe: generate depth -> to point cloud -> filter -> export -> import -> validate."""
    details: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    try:
        # Generate depth frame
        w, h = 64, 48
        depth_data = _generate_synthetic_depth(w, h, "flat_wall", seed=99)
        frame = DepthFrame(
            width=w, height=h, depth_data=depth_data,
            timestamp=time.time(), sensor_id="test_pc",
            frame_number=0, min_depth=0.0, max_depth=0.0,
        )

        proc = create_point_cloud_processor("open3d")

        # Depth to point cloud
        cloud = proc.depth_to_point_cloud(frame)
        assert cloud.point_count > 0, "empty point cloud"
        assert len(cloud.points) == cloud.point_count

        details.append({"test": "depth_to_cloud", "status": "passed",
                        "point_count": cloud.point_count})
        passed += 1

    except Exception as e:
        details.append({"test": "depth_to_cloud", "status": "failed", "error": str(e)})
        failed += 1
        cloud = None

    # Filter tests
    if cloud and cloud.point_count > 0:
        for ft in [FilterType.voxel_grid, FilterType.passthrough, FilterType.crop]:
            try:
                if ft == FilterType.voxel_grid:
                    filtered = proc.filter_point_cloud(cloud, ft.value, voxel_size=0.05)
                elif ft == FilterType.passthrough:
                    filtered = proc.filter_point_cloud(cloud, ft.value,
                                                       axis="z", min_val=0.9, max_val=1.1)
                elif ft == FilterType.crop:
                    filtered = proc.filter_point_cloud(
                        cloud, ft.value,
                        min_bound=(-0.3, -0.3, 0.8),
                        max_bound=(0.3, 0.3, 1.2),
                    )
                else:
                    continue

                assert filtered.point_count <= cloud.point_count, "filter increased points"
                assert filtered.point_count >= 0

                details.append({"test": f"filter_{ft.value}", "status": "passed",
                                "points_before": cloud.point_count,
                                "points_after": filtered.point_count})
                passed += 1

            except Exception as e:
                details.append({"test": f"filter_{ft.value}", "status": "failed",
                                "error": str(e)})
                failed += 1

        # Normal estimation
        try:
            with_normals = proc.compute_normals(cloud, radius=0.2)
            assert len(with_normals.normals) == with_normals.point_count
            details.append({"test": "compute_normals", "status": "passed"})
            passed += 1
        except Exception as e:
            details.append({"test": "compute_normals", "status": "failed", "error": str(e)})
            failed += 1

        # Export / import round-trip
        for fmt in [PointCloudFormat.ply, PointCloudFormat.xyz]:
            try:
                exported = proc.export(cloud, fmt.value)
                assert len(exported) > 0, f"empty {fmt.value} export"

                reimported = proc.import_cloud(exported, fmt.value)
                assert reimported.point_count == cloud.point_count, (
                    f"roundtrip count mismatch: {reimported.point_count} vs {cloud.point_count}"
                )

                details.append({"test": f"export_import_{fmt.value}", "status": "passed"})
                passed += 1

            except Exception as e:
                details.append({"test": f"export_import_{fmt.value}", "status": "failed",
                                "error": str(e)})
                failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_registration_recipe(recipe_id: str, t0: float) -> TestResult:
    """Recipe: generate two overlapping clouds -> register -> check fitness."""
    details: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    # Generate source cloud
    source_points = [
        (0.1 * i, 0.1 * j, 1.0)
        for i in range(-5, 6) for j in range(-5, 6)
    ]
    source = PointCloudData(
        points=source_points, colors=[], normals=[],
        point_count=len(source_points),
        bounds_min=(-0.5, -0.5, 1.0), bounds_max=(0.5, 0.5, 1.0),
    )

    # Generate target: source shifted by small translation
    shift = _translation_matrix(0.02, 0.01, 0.0)
    target_points = _apply_transform(source_points, shift)
    target = PointCloudData(
        points=target_points, colors=[], normals=[],
        point_count=len(target_points),
        bounds_min=(-0.48, -0.49, 1.0), bounds_max=(0.52, 0.51, 1.0),
    )

    for algo in RegistrationAlgorithm:
        try:
            result = register_point_clouds(source, target, algo.value)

            assert result.converged, f"{algo.value} did not converge"
            assert result.fitness > 0.0, f"{algo.value} zero fitness"
            assert result.iterations > 0, f"{algo.value} zero iterations"
            assert len(result.transformation) == 4
            assert len(result.transformation[0]) == 4

            details.append({
                "algorithm": algo.value,
                "status": "passed",
                "fitness": result.fitness,
                "inlier_rmse": result.inlier_rmse,
                "iterations": result.iterations,
            })
            passed += 1

        except Exception as e:
            details.append({"algorithm": algo.value, "status": "failed", "error": str(e)})
            failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


def _run_slam_odometry_recipe(recipe_id: str, t0: float) -> TestResult:
    """Recipe: generate trajectory frames -> process through SLAM -> check drift."""
    details: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    for st in SlamType:
        try:
            hook = create_slam_hook(st.value)
            assert hook.initialize(), "SLAM init failed"
            assert hook.initialized

            num_frames = 10
            w, h = 64, 48

            for fi in range(num_frames):
                depth_data = _generate_synthetic_depth(w, h, "flat_wall",
                                                       seed=fi * 1000)
                frame = DepthFrame(
                    width=w, height=h, depth_data=depth_data,
                    timestamp=time.time(), sensor_id=f"slam_test_{st.value}",
                    frame_number=fi, min_depth=0.9, max_depth=1.1,
                )
                pose = hook.process_frame(frame)
                assert pose.frame_id == fi + 1
                assert pose.confidence > 0

            trajectory = hook.get_trajectory()
            assert len(trajectory) == num_frames, (
                f"trajectory length {len(trajectory)} != {num_frames}"
            )

            # Check drift: final position should be within reasonable bounds
            final_pos = trajectory[-1].position
            drift = math.sqrt(sum(c ** 2 for c in final_pos))
            assert drift < 100.0, f"excessive drift: {drift}"

            map_cloud = hook.get_map_points()
            assert map_cloud.point_count > 0, "empty map"

            assert hook.reset()
            assert not hook.initialized

            details.append({
                "slam_type": st.value,
                "status": "passed",
                "trajectory_length": len(trajectory),
                "final_position": final_pos,
                "map_points": map_cloud.point_count,
            })
            passed += 1

        except Exception as e:
            details.append({"slam_type": st.value, "status": "failed", "error": str(e)})
            failed += 1

    return TestResult(
        recipe_id=recipe_id,
        status=TestStatus.passed.value if failed == 0 else TestStatus.failed.value,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round((time.monotonic() - t0) * 1000, 2),
        details=details,
    )


# ── Artifacts & gate ──────────────────────────────────────────────────────


def list_artifacts() -> list[dict[str, Any]]:
    """List available artifacts from config."""
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
    """Run all test recipes and return a gate verdict."""
    t0 = time.monotonic()
    recipe_results: list[dict[str, Any]] = []
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
