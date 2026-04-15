"""C14 — L4-CORE-14 Sensor fusion library (#228).

IMU drivers (MPU6050 / LSM6DS3 / BMI270), GPS NMEA parser + UBX protocol,
barometer drivers (BMP280 / LPS22), EKF (9-DoF orientation), calibration
routines (bias/scale/alignment), and trajectory test fixtures.

Public API:
    drivers    = list_imu_drivers()
    driver     = get_imu_driver(driver_id)
    gps        = list_gps_protocols()
    baro       = list_barometer_drivers()
    profiles   = list_ekf_profiles()
    cal        = list_calibration_profiles()
    recipes    = list_test_recipes()
    fixtures   = list_trajectory_fixtures()
    nmea       = parse_nmea_sentence(sentence)
    ubx        = parse_ubx_message(data)
    altitude   = pressure_to_altitude(pressure_pa)
    result     = run_ekf_orientation(imu_samples)
    cal_result = run_imu_calibration(static_data)
    test       = run_sensor_test(recipe_id, target, work_dir)
    certs      = get_sensor_fusion_certs()
"""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SENSOR_FUSION_PATH = _PROJECT_ROOT / "configs" / "sensor_fusion_profiles.yaml"


# -- Enums --

class SensorType(str, Enum):
    imu = "imu"
    gps = "gps"
    barometer = "barometer"
    magnetometer = "magnetometer"
    fusion = "fusion"


class SensorBus(str, Enum):
    i2c = "i2c"
    spi = "spi"
    uart = "uart"


class TestCategory(str, Enum):
    functional = "functional"
    performance = "performance"
    calibration = "calibration"


class TestStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    pending = "pending"
    skipped = "skipped"
    error = "error"


class CalibrationStatus(str, Enum):
    not_calibrated = "not_calibrated"
    in_progress = "in_progress"
    calibrated = "calibrated"
    failed = "failed"


class EKFState(str, Enum):
    uninitialized = "uninitialized"
    converging = "converging"
    converged = "converged"
    diverged = "diverged"


class NMEASentenceType(str, Enum):
    GGA = "GGA"
    RMC = "RMC"
    GSA = "GSA"
    GSV = "GSV"
    VTG = "VTG"
    GLL = "GLL"


# -- Data models --

@dataclass
class IMURegister:
    addr: str
    expected: str = ""
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"addr": self.addr}
        if self.expected:
            d["expected"] = self.expected
        if self.count:
            d["count"] = self.count
        return d


@dataclass
class IMUDriverDef:
    driver_id: str
    name: str
    vendor: str
    bus: str = "i2c"
    i2c_addr_default: str = ""
    i2c_addr_alt: str = ""
    accel_range_g: list[int] = field(default_factory=list)
    gyro_range_dps: list[int] = field(default_factory=list)
    sample_rate_hz: int = 0
    fifo_depth: int = 0
    axes: int = 6
    temperature: bool = True
    interrupt_pin: bool = True
    wake_on_motion: bool = False
    step_counter: bool = False
    gesture_recognition: bool = False
    requires_config_upload: bool = False
    compatible_socs: list[str] = field(default_factory=list)
    registers: dict[str, IMURegister] = field(default_factory=dict)
    init_sequence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "driver_id": self.driver_id,
            "name": self.name,
            "vendor": self.vendor,
            "bus": self.bus,
            "i2c_addr_default": self.i2c_addr_default,
            "i2c_addr_alt": self.i2c_addr_alt,
            "accel_range_g": self.accel_range_g,
            "gyro_range_dps": self.gyro_range_dps,
            "sample_rate_hz": self.sample_rate_hz,
            "fifo_depth": self.fifo_depth,
            "axes": self.axes,
            "temperature": self.temperature,
            "interrupt_pin": self.interrupt_pin,
            "wake_on_motion": self.wake_on_motion,
            "compatible_socs": self.compatible_socs,
        }


@dataclass
class GPSProtocolDef:
    protocol_id: str
    name: str
    standard: str = ""
    baud_default: int = 0
    supported_sentences: list[dict[str, str]] = field(default_factory=list)
    message_classes: list[dict[str, str]] = field(default_factory=list)
    key_messages: list[dict[str, str]] = field(default_factory=list)
    talker_ids: list[dict[str, str]] = field(default_factory=list)
    sync_chars: list[int] = field(default_factory=list)
    checksum: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_id": self.protocol_id,
            "name": self.name,
            "standard": self.standard,
            "baud_default": self.baud_default,
            "supported_sentences": self.supported_sentences,
            "message_classes": self.message_classes,
            "key_messages": self.key_messages,
            "talker_ids": self.talker_ids,
            "checksum": self.checksum,
        }


@dataclass
class BarometerDriverDef:
    driver_id: str
    name: str
    vendor: str
    bus: str = "i2c"
    i2c_addr_default: str = ""
    i2c_addr_alt: str = ""
    pressure_range_hpa: list[float] = field(default_factory=list)
    pressure_resolution_pa: float = 0.0
    temperature_range_c: list[float] = field(default_factory=list)
    temperature_resolution_c: float = 0.0
    sample_rate_hz: int = 0
    modes: list[str] = field(default_factory=list)
    compatible_socs: list[str] = field(default_factory=list)
    registers: dict[str, IMURegister] = field(default_factory=dict)
    init_sequence: list[dict[str, Any]] = field(default_factory=list)
    compensation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "driver_id": self.driver_id,
            "name": self.name,
            "vendor": self.vendor,
            "bus": self.bus,
            "i2c_addr_default": self.i2c_addr_default,
            "pressure_range_hpa": self.pressure_range_hpa,
            "pressure_resolution_pa": self.pressure_resolution_pa,
            "temperature_range_c": self.temperature_range_c,
            "sample_rate_hz": self.sample_rate_hz,
            "modes": self.modes,
            "compatible_socs": self.compatible_socs,
        }


@dataclass
class EKFProfileDef:
    profile_id: str
    name: str
    description: str = ""
    state_dim: int = 7
    measurement_dim: int = 6
    state_vector: list[dict[str, str]] = field(default_factory=list)
    process_noise: dict[str, float] = field(default_factory=dict)
    measurement_noise: dict[str, float] = field(default_factory=dict)
    initial_covariance: float = 0.1
    prediction_model: str = ""
    update_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "description": self.description,
            "state_dim": self.state_dim,
            "measurement_dim": self.measurement_dim,
            "state_vector": self.state_vector,
            "process_noise": dict(self.process_noise),
            "measurement_noise": dict(self.measurement_noise),
            "initial_covariance": self.initial_covariance,
            "prediction_model": self.prediction_model,
            "update_model": self.update_model,
        }


@dataclass
class CalibrationProfileDef:
    profile_id: str
    name: str
    description: str = ""
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    procedure: list[dict[str, Any]] = field(default_factory=list)
    min_samples_per_position: int = 0
    min_samples: int = 0
    static_threshold_g: float = 0.05

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "procedure": self.procedure,
        }


@dataclass
class SensorTestRecipe:
    recipe_id: str
    name: str
    category: str
    description: str = ""
    sensor_type: str = ""
    tools: list[str] = field(default_factory=list)
    timeout_s: int = 60

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "sensor_type": self.sensor_type,
            "tools": self.tools,
            "timeout_s": self.timeout_s,
        }


@dataclass
class SensorTestResult:
    recipe_id: str
    sensor_type: str
    status: TestStatus
    target_device: str = ""
    timestamp: float = field(default_factory=time.time)
    measurements: dict[str, Any] = field(default_factory=dict)
    raw_log_path: str = ""
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.status == TestStatus.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "sensor_type": self.sensor_type,
            "status": self.status.value,
            "target_device": self.target_device,
            "timestamp": self.timestamp,
            "measurements": self.measurements,
            "raw_log_path": self.raw_log_path,
            "message": self.message,
        }


@dataclass
class TrajectoryFixture:
    fixture_id: str
    name: str
    duration_s: float = 10.0
    sample_rate_hz: int = 100
    expected_orientation: dict[str, float] = field(default_factory=dict)
    expected_final_orientation: dict[str, float] = field(default_factory=dict)
    tolerance_deg: float = 5.0
    angular_rate_dps: float = 0.0
    return_to_origin: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "fixture_id": self.fixture_id,
            "name": self.name,
            "duration_s": self.duration_s,
            "sample_rate_hz": self.sample_rate_hz,
            "tolerance_deg": self.tolerance_deg,
            "description": self.description,
        }
        if self.expected_orientation:
            d["expected_orientation"] = self.expected_orientation
        if self.expected_final_orientation:
            d["expected_final_orientation"] = self.expected_final_orientation
        if self.angular_rate_dps:
            d["angular_rate_dps"] = self.angular_rate_dps
        if self.return_to_origin:
            d["return_to_origin"] = self.return_to_origin
        return d


@dataclass
class NMEAResult:
    sentence_type: str
    talker_id: str = ""
    valid: bool = False
    fields: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    checksum_ok: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sentence_type": self.sentence_type,
            "talker_id": self.talker_id,
            "valid": self.valid,
            "fields": self.fields,
            "raw": self.raw,
            "checksum_ok": self.checksum_ok,
            "error": self.error,
        }


@dataclass
class UBXMessage:
    msg_class: int
    msg_id: int
    payload: bytes = b""
    valid: bool = False
    class_name: str = ""
    msg_name: str = ""
    parsed_fields: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "msg_class": f"0x{self.msg_class:02X}",
            "msg_id": f"0x{self.msg_id:02X}",
            "payload_length": len(self.payload),
            "valid": self.valid,
            "class_name": self.class_name,
            "msg_name": self.msg_name,
            "parsed_fields": self.parsed_fields,
            "error": self.error,
        }


@dataclass
class IMUSample:
    timestamp: float
    accel_x: float = 0.0
    accel_y: float = 0.0
    accel_z: float = 0.0
    gyro_x: float = 0.0
    gyro_y: float = 0.0
    gyro_z: float = 0.0
    mag_x: float = 0.0
    mag_y: float = 0.0
    mag_z: float = 0.0
    temperature: float = 0.0


@dataclass
class EKFResult:
    profile_id: str
    state: EKFState
    quaternion: list[float] = field(default_factory=lambda: [1.0, 0.0, 0.0, 0.0])
    euler_deg: dict[str, float] = field(default_factory=lambda: {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
    gyro_bias: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    covariance_trace: float = 0.0
    iterations: int = 0
    rms_error_deg: float = 0.0
    timestamp: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "state": self.state.value,
            "quaternion": self.quaternion,
            "euler_deg": self.euler_deg,
            "gyro_bias": self.gyro_bias,
            "covariance_trace": self.covariance_trace,
            "iterations": self.iterations,
            "rms_error_deg": self.rms_error_deg,
            "timestamp": self.timestamp,
            "message": self.message,
        }


@dataclass
class CalibrationResult:
    profile_id: str
    status: CalibrationStatus
    accel_bias: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    accel_scale: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    gyro_bias: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    gyro_scale: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    misalignment_matrix: list[list[float]] = field(
        default_factory=lambda: [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    )
    residual_g: float = 0.0
    samples_used: int = 0
    timestamp: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "status": self.status.value,
            "accel_bias": self.accel_bias,
            "accel_scale": self.accel_scale,
            "gyro_bias": self.gyro_bias,
            "gyro_scale": self.gyro_scale,
            "misalignment_matrix": self.misalignment_matrix,
            "residual_g": self.residual_g,
            "samples_used": self.samples_used,
            "timestamp": self.timestamp,
            "message": self.message,
        }


@dataclass
class SensorFusionCertArtifact:
    artifact_id: str
    name: str
    sensor_type: str
    status: str = "pending"
    file_path: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "sensor_type": self.sensor_type,
            "status": self.status,
            "file_path": self.file_path,
            "description": self.description,
        }


# -- Config loading (cached) --

_SF_CACHE: dict | None = None


def _load_sensor_fusion_config() -> dict:
    global _SF_CACHE
    if _SF_CACHE is None:
        try:
            _SF_CACHE = yaml.safe_load(
                _SENSOR_FUSION_PATH.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "sensor_fusion_profiles.yaml load failed: %s — using empty config", exc
            )
            _SF_CACHE = {
                "imu_drivers": {},
                "gps_protocols": {},
                "barometer_drivers": {},
                "ekf_profiles": {},
                "calibration_profiles": {},
                "test_recipes": [],
                "trajectory_fixtures": {},
                "artifact_definitions": {},
            }
    return _SF_CACHE


def reload_sensor_fusion_config_for_tests() -> None:
    global _SF_CACHE
    _SF_CACHE = None


def _parse_registers(raw: dict) -> dict[str, IMURegister]:
    regs: dict[str, IMURegister] = {}
    for name, info in raw.items():
        if isinstance(info, dict):
            regs[name] = IMURegister(
                addr=info.get("addr", ""),
                expected=info.get("expected", ""),
                count=info.get("count", 0),
            )
    return regs


# -- IMU driver queries --

def _parse_imu_driver(driver_id: str, data: dict) -> IMUDriverDef:
    return IMUDriverDef(
        driver_id=driver_id,
        name=data.get("name", driver_id),
        vendor=data.get("vendor", ""),
        bus=data.get("bus", "i2c"),
        i2c_addr_default=data.get("i2c_addr_default", ""),
        i2c_addr_alt=data.get("i2c_addr_alt", ""),
        accel_range_g=data.get("accel_range_g", []),
        gyro_range_dps=data.get("gyro_range_dps", []),
        sample_rate_hz=data.get("sample_rate_hz", 0),
        fifo_depth=data.get("fifo_depth", 0),
        axes=data.get("axes", 6),
        temperature=data.get("temperature", True),
        interrupt_pin=data.get("interrupt_pin", True),
        wake_on_motion=data.get("wake_on_motion", False),
        step_counter=data.get("step_counter", False),
        gesture_recognition=data.get("gesture_recognition", False),
        requires_config_upload=data.get("requires_config_upload", False),
        compatible_socs=data.get("compatible_socs", []),
        registers=_parse_registers(data.get("registers", {})),
        init_sequence=data.get("init_sequence", []),
    )


def list_imu_drivers() -> list[IMUDriverDef]:
    raw = _load_sensor_fusion_config().get("imu_drivers", {})
    return [_parse_imu_driver(k, v) for k, v in raw.items()]


def get_imu_driver(driver_id: str) -> IMUDriverDef | None:
    raw = _load_sensor_fusion_config().get("imu_drivers", {})
    if driver_id not in raw:
        return None
    return _parse_imu_driver(driver_id, raw[driver_id])


# -- GPS protocol queries --

def _parse_gps_protocol(protocol_id: str, data: dict) -> GPSProtocolDef:
    return GPSProtocolDef(
        protocol_id=protocol_id,
        name=data.get("name", protocol_id),
        standard=data.get("standard", ""),
        baud_default=data.get("baud_default", 0),
        supported_sentences=data.get("supported_sentences", []),
        message_classes=data.get("message_classes", []),
        key_messages=data.get("key_messages", []),
        talker_ids=data.get("talker_ids", []),
        sync_chars=data.get("sync_chars", []),
        checksum=data.get("checksum", ""),
    )


def list_gps_protocols() -> list[GPSProtocolDef]:
    raw = _load_sensor_fusion_config().get("gps_protocols", {})
    return [_parse_gps_protocol(k, v) for k, v in raw.items()]


def get_gps_protocol(protocol_id: str) -> GPSProtocolDef | None:
    raw = _load_sensor_fusion_config().get("gps_protocols", {})
    if protocol_id not in raw:
        return None
    return _parse_gps_protocol(protocol_id, raw[protocol_id])


# -- Barometer driver queries --

def _parse_barometer_driver(driver_id: str, data: dict) -> BarometerDriverDef:
    return BarometerDriverDef(
        driver_id=driver_id,
        name=data.get("name", driver_id),
        vendor=data.get("vendor", ""),
        bus=data.get("bus", "i2c"),
        i2c_addr_default=data.get("i2c_addr_default", ""),
        i2c_addr_alt=data.get("i2c_addr_alt", ""),
        pressure_range_hpa=data.get("pressure_range_hpa", []),
        pressure_resolution_pa=data.get("pressure_resolution_pa", 0.0),
        temperature_range_c=data.get("temperature_range_c", []),
        temperature_resolution_c=data.get("temperature_resolution_c", 0.0),
        sample_rate_hz=data.get("sample_rate_hz", 0),
        modes=data.get("modes", []),
        compatible_socs=data.get("compatible_socs", []),
        registers=_parse_registers(data.get("registers", {})),
        init_sequence=data.get("init_sequence", []),
        compensation=data.get("compensation", ""),
    )


def list_barometer_drivers() -> list[BarometerDriverDef]:
    raw = _load_sensor_fusion_config().get("barometer_drivers", {})
    return [_parse_barometer_driver(k, v) for k, v in raw.items()]


def get_barometer_driver(driver_id: str) -> BarometerDriverDef | None:
    raw = _load_sensor_fusion_config().get("barometer_drivers", {})
    if driver_id not in raw:
        return None
    return _parse_barometer_driver(driver_id, raw[driver_id])


# -- EKF profile queries --

def _parse_ekf_profile(profile_id: str, data: dict) -> EKFProfileDef:
    return EKFProfileDef(
        profile_id=profile_id,
        name=data.get("name", profile_id),
        description=data.get("description", ""),
        state_dim=data.get("state_dim", 7),
        measurement_dim=data.get("measurement_dim", 6),
        state_vector=data.get("state_vector", []),
        process_noise=data.get("process_noise", {}),
        measurement_noise=data.get("measurement_noise", {}),
        initial_covariance=data.get("initial_covariance", 0.1),
        prediction_model=data.get("prediction_model", ""),
        update_model=data.get("update_model", ""),
    )


def list_ekf_profiles() -> list[EKFProfileDef]:
    raw = _load_sensor_fusion_config().get("ekf_profiles", {})
    return [_parse_ekf_profile(k, v) for k, v in raw.items()]


def get_ekf_profile(profile_id: str) -> EKFProfileDef | None:
    raw = _load_sensor_fusion_config().get("ekf_profiles", {})
    if profile_id not in raw:
        return None
    return _parse_ekf_profile(profile_id, raw[profile_id])


# -- Calibration profile queries --

def _parse_calibration_profile(profile_id: str, data: dict) -> CalibrationProfileDef:
    return CalibrationProfileDef(
        profile_id=profile_id,
        name=data.get("name", profile_id),
        description=data.get("description", ""),
        parameters=data.get("parameters", {}),
        procedure=data.get("procedure", []),
        min_samples_per_position=data.get("min_samples_per_position", 0),
        min_samples=data.get("min_samples", 0),
        static_threshold_g=data.get("static_threshold_g", 0.05),
    )


def list_calibration_profiles() -> list[CalibrationProfileDef]:
    raw = _load_sensor_fusion_config().get("calibration_profiles", {})
    return [_parse_calibration_profile(k, v) for k, v in raw.items()]


def get_calibration_profile(profile_id: str) -> CalibrationProfileDef | None:
    raw = _load_sensor_fusion_config().get("calibration_profiles", {})
    if profile_id not in raw:
        return None
    return _parse_calibration_profile(profile_id, raw[profile_id])


# -- Test recipe queries --

def _parse_test_recipe(data: dict) -> SensorTestRecipe:
    return SensorTestRecipe(
        recipe_id=data["id"],
        name=data.get("name", data["id"]),
        category=data.get("category", ""),
        description=data.get("description", ""),
        sensor_type=data.get("sensor_type", ""),
        tools=data.get("tools", []),
        timeout_s=data.get("timeout_s", 60),
    )


def list_test_recipes() -> list[SensorTestRecipe]:
    raw = _load_sensor_fusion_config().get("test_recipes", [])
    return [_parse_test_recipe(r) for r in raw]


def get_test_recipe(recipe_id: str) -> SensorTestRecipe | None:
    for r in list_test_recipes():
        if r.recipe_id == recipe_id:
            return r
    return None


def get_recipes_by_sensor_type(sensor_type: str) -> list[SensorTestRecipe]:
    return [r for r in list_test_recipes() if r.sensor_type == sensor_type]


def get_recipes_by_category(category: str) -> list[SensorTestRecipe]:
    return [r for r in list_test_recipes() if r.category == category]


# -- Trajectory fixture queries --

def _parse_trajectory_fixture(fixture_id: str, data: dict) -> TrajectoryFixture:
    return TrajectoryFixture(
        fixture_id=fixture_id,
        name=data.get("name", fixture_id),
        duration_s=data.get("duration_s", 10.0),
        sample_rate_hz=data.get("sample_rate_hz", 100),
        expected_orientation=data.get("expected_orientation", {}),
        expected_final_orientation=data.get("expected_final_orientation", {}),
        tolerance_deg=data.get("tolerance_deg", 5.0),
        angular_rate_dps=data.get("angular_rate_dps", 0.0),
        return_to_origin=data.get("return_to_origin", False),
        description=data.get("description", ""),
    )


def list_trajectory_fixtures() -> list[TrajectoryFixture]:
    raw = _load_sensor_fusion_config().get("trajectory_fixtures", {})
    return [_parse_trajectory_fixture(k, v) for k, v in raw.items()]


def get_trajectory_fixture(fixture_id: str) -> TrajectoryFixture | None:
    raw = _load_sensor_fusion_config().get("trajectory_fixtures", {})
    if fixture_id not in raw:
        return None
    return _parse_trajectory_fixture(fixture_id, raw[fixture_id])


# -- Artifact definitions --

def get_artifact_definition(artifact_id: str) -> dict[str, Any] | None:
    raw = _load_sensor_fusion_config().get("artifact_definitions", {})
    if artifact_id not in raw:
        return None
    d = raw[artifact_id]
    return {
        "artifact_id": artifact_id,
        "name": d.get("name", artifact_id),
        "description": d.get("description", ""),
        "file_pattern": d.get("file_pattern", ""),
    }


def list_artifact_definitions() -> list[dict[str, Any]]:
    raw = _load_sensor_fusion_config().get("artifact_definitions", {})
    return [
        {
            "artifact_id": k,
            "name": v.get("name", k),
            "description": v.get("description", ""),
            "file_pattern": v.get("file_pattern", ""),
        }
        for k, v in raw.items()
    ]


# -- NMEA parser --

def _nmea_checksum(sentence: str) -> str:
    start = sentence.find("$")
    end = sentence.find("*")
    if start < 0 or end < 0 or end <= start:
        return ""
    body = sentence[start + 1 : end]
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"{cs:02X}"


def parse_nmea_sentence(sentence: str) -> NMEAResult:
    sentence = sentence.strip()
    if not sentence.startswith("$"):
        return NMEAResult(
            sentence_type="",
            raw=sentence,
            error="Sentence must start with '$'",
        )

    has_checksum = "*" in sentence
    if has_checksum:
        expected_cs = sentence[sentence.index("*") + 1 :].strip().upper()
        computed_cs = _nmea_checksum(sentence)
        checksum_ok = expected_cs == computed_cs
        data_part = sentence[1 : sentence.index("*")]
    else:
        checksum_ok = False
        data_part = sentence[1:]

    parts = data_part.split(",")
    if not parts:
        return NMEAResult(sentence_type="", raw=sentence, error="Empty sentence")

    header = parts[0]
    talker_id = header[:2] if len(header) >= 5 else ""
    sentence_type = header[2:] if len(header) >= 5 else header

    fields: dict[str, Any] = {}

    if sentence_type == "GGA" and len(parts) >= 15:
        fields = _parse_gga(parts)
    elif sentence_type == "RMC" and len(parts) >= 12:
        fields = _parse_rmc(parts)
    elif sentence_type == "GSA" and len(parts) >= 18:
        fields = _parse_gsa(parts)
    elif sentence_type == "VTG" and len(parts) >= 9:
        fields = _parse_vtg(parts)
    elif sentence_type == "GLL" and len(parts) >= 7:
        fields = _parse_gll(parts)

    valid = checksum_ok and bool(fields)

    return NMEAResult(
        sentence_type=sentence_type,
        talker_id=talker_id,
        valid=valid,
        fields=fields,
        raw=sentence,
        checksum_ok=checksum_ok,
    )


def _safe_float(s: str, default: float = 0.0) -> float:
    try:
        return float(s) if s else default
    except (ValueError, TypeError):
        return default


def _parse_lat_lon(lat_str: str, lat_dir: str, lon_str: str, lon_dir: str) -> dict[str, float]:
    lat = 0.0
    lon = 0.0
    if lat_str:
        lat_deg = int(float(lat_str) / 100)
        lat_min = float(lat_str) - lat_deg * 100
        lat = lat_deg + lat_min / 60.0
        if lat_dir == "S":
            lat = -lat
    if lon_str:
        lon_deg = int(float(lon_str) / 100)
        lon_min = float(lon_str) - lon_deg * 100
        lon = lon_deg + lon_min / 60.0
        if lon_dir == "W":
            lon = -lon
    return {"latitude": lat, "longitude": lon}


def _parse_gga(parts: list[str]) -> dict[str, Any]:
    pos = _parse_lat_lon(parts[2], parts[3], parts[4], parts[5])
    return {
        "time": parts[1],
        "latitude": pos["latitude"],
        "longitude": pos["longitude"],
        "fix_quality": int(parts[6]) if parts[6] else 0,
        "num_satellites": int(parts[7]) if parts[7] else 0,
        "hdop": _safe_float(parts[8]),
        "altitude_m": _safe_float(parts[9]),
        "geoid_separation_m": _safe_float(parts[11]),
    }


def _parse_rmc(parts: list[str]) -> dict[str, Any]:
    pos = _parse_lat_lon(parts[3], parts[4], parts[5], parts[6])
    return {
        "time": parts[1],
        "status": parts[2],
        "latitude": pos["latitude"],
        "longitude": pos["longitude"],
        "speed_knots": _safe_float(parts[7]),
        "course_deg": _safe_float(parts[8]),
        "date": parts[9],
    }


def _parse_gsa(parts: list[str]) -> dict[str, Any]:
    sat_ids = [int(p) for p in parts[3:15] if p]
    return {
        "mode": parts[1],
        "fix_type": int(parts[2]) if parts[2] else 0,
        "satellite_ids": sat_ids,
        "pdop": _safe_float(parts[15]),
        "hdop": _safe_float(parts[16]),
        "vdop": _safe_float(parts[17]),
    }


def _parse_vtg(parts: list[str]) -> dict[str, Any]:
    return {
        "course_true": _safe_float(parts[1]),
        "course_magnetic": _safe_float(parts[3]),
        "speed_knots": _safe_float(parts[5]),
        "speed_kmh": _safe_float(parts[7]),
    }


def _parse_gll(parts: list[str]) -> dict[str, Any]:
    pos = _parse_lat_lon(parts[1], parts[2], parts[3], parts[4])
    return {
        "latitude": pos["latitude"],
        "longitude": pos["longitude"],
        "time": parts[5],
        "status": parts[6] if len(parts) > 6 else "",
    }


# -- UBX parser --

_UBX_CLASS_NAMES = {
    0x01: "NAV", 0x02: "RXM", 0x04: "INF", 0x05: "ACK",
    0x06: "CFG", 0x0A: "MON", 0x0D: "TIM", 0x13: "MGA",
}

_UBX_MSG_NAMES = {
    (0x01, 0x07): "NAV-PVT",
    (0x01, 0x03): "NAV-STATUS",
    (0x05, 0x01): "ACK-ACK",
    (0x05, 0x00): "ACK-NAK",
    (0x06, 0x00): "CFG-PRT",
    (0x06, 0x08): "CFG-RATE",
    (0x06, 0x24): "CFG-NAV5",
    (0x0A, 0x09): "MON-HW",
}


def _ubx_checksum(data: bytes) -> tuple[int, int]:
    ck_a = 0
    ck_b = 0
    for b in data:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return ck_a, ck_b


def parse_ubx_message(data: bytes) -> UBXMessage:
    if len(data) < 8:
        return UBXMessage(msg_class=0, msg_id=0, error="Message too short (< 8 bytes)")

    if data[0] != 0xB5 or data[1] != 0x62:
        return UBXMessage(msg_class=0, msg_id=0, error="Invalid sync chars")

    msg_class = data[2]
    msg_id = data[3]
    length = data[4] | (data[5] << 8)

    if len(data) < 6 + length + 2:
        return UBXMessage(
            msg_class=msg_class,
            msg_id=msg_id,
            error=f"Incomplete message: expected {6 + length + 2} bytes, got {len(data)}",
        )

    payload = data[6 : 6 + length]
    ck_a_expected = data[6 + length]
    ck_b_expected = data[6 + length + 1]

    ck_a, ck_b = _ubx_checksum(data[2 : 6 + length])
    valid = ck_a == ck_a_expected and ck_b == ck_b_expected

    class_name = _UBX_CLASS_NAMES.get(msg_class, f"UNK-0x{msg_class:02X}")
    msg_name = _UBX_MSG_NAMES.get((msg_class, msg_id), f"{class_name}-0x{msg_id:02X}")

    parsed_fields: dict[str, Any] = {}
    if valid and msg_class == 0x01 and msg_id == 0x07 and length >= 92:
        parsed_fields = _parse_nav_pvt(payload)

    return UBXMessage(
        msg_class=msg_class,
        msg_id=msg_id,
        payload=payload,
        valid=valid,
        class_name=class_name,
        msg_name=msg_name,
        parsed_fields=parsed_fields,
    )


def _parse_nav_pvt(payload: bytes) -> dict[str, Any]:
    def _i32(off: int) -> int:
        v = int.from_bytes(payload[off : off + 4], "little", signed=True)
        return v

    def _u32(off: int) -> int:
        return int.from_bytes(payload[off : off + 4], "little", signed=False)

    def _u16(off: int) -> int:
        return int.from_bytes(payload[off : off + 2], "little", signed=False)

    def _u8(off: int) -> int:
        return payload[off]

    return {
        "iTOW": _u32(0),
        "year": _u16(4),
        "month": _u8(6),
        "day": _u8(7),
        "hour": _u8(8),
        "min": _u8(9),
        "sec": _u8(10),
        "valid": _u8(11),
        "fixType": _u8(20),
        "flags": _u8(21),
        "numSV": _u8(23),
        "lon_deg": _i32(24) * 1e-7,
        "lat_deg": _i32(28) * 1e-7,
        "height_mm": _i32(32),
        "hMSL_mm": _i32(36),
        "hAcc_mm": _u32(40),
        "vAcc_mm": _u32(44),
        "velN_mm_s": _i32(48),
        "velE_mm_s": _i32(52),
        "velD_mm_s": _i32(56),
        "gSpeed_mm_s": _i32(60),
        "headMot_deg": _i32(64) * 1e-5,
        "pDOP": _u16(76) * 0.01,
    }


def build_ubx_message(msg_class: int, msg_id: int, payload: bytes = b"") -> bytes:
    length = len(payload)
    header = bytes([0xB5, 0x62, msg_class, msg_id, length & 0xFF, (length >> 8) & 0xFF])
    body = bytes([msg_class, msg_id, length & 0xFF, (length >> 8) & 0xFF]) + payload
    ck_a, ck_b = _ubx_checksum(body)
    return header + payload + bytes([ck_a, ck_b])


# -- Barometric altitude --

_SEA_LEVEL_PRESSURE_PA = 101325.0
_TEMPERATURE_LAPSE_RATE = 0.0065
_STD_TEMPERATURE_K = 288.15
_GRAVITY = 9.80665
_MOLAR_MASS = 0.0289644
_GAS_CONSTANT = 8.31447


def pressure_to_altitude(
    pressure_pa: float,
    sea_level_pressure_pa: float = _SEA_LEVEL_PRESSURE_PA,
) -> float:
    if pressure_pa <= 0 or sea_level_pressure_pa <= 0:
        return 0.0
    ratio = pressure_pa / sea_level_pressure_pa
    exponent = (_GAS_CONSTANT * _TEMPERATURE_LAPSE_RATE) / (_GRAVITY * _MOLAR_MASS)
    altitude = (_STD_TEMPERATURE_K / _TEMPERATURE_LAPSE_RATE) * (1.0 - ratio ** exponent)
    return altitude


def altitude_to_pressure(
    altitude_m: float,
    sea_level_pressure_pa: float = _SEA_LEVEL_PRESSURE_PA,
) -> float:
    exponent = (_GRAVITY * _MOLAR_MASS) / (_GAS_CONSTANT * _TEMPERATURE_LAPSE_RATE)
    factor = 1.0 - (_TEMPERATURE_LAPSE_RATE * altitude_m) / _STD_TEMPERATURE_K
    if factor <= 0:
        return 0.0
    return sea_level_pressure_pa * (factor ** exponent)


# -- EKF implementation (9-DoF orientation) --

def _normalize_quaternion(q: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in q))
    if norm < 1e-10:
        return [1.0, 0.0, 0.0, 0.0]
    return [x / norm for x in q]


def _quaternion_multiply(a: list[float], b: list[float]) -> list[float]:
    return [
        a[0]*b[0] - a[1]*b[1] - a[2]*b[2] - a[3]*b[3],
        a[0]*b[1] + a[1]*b[0] + a[2]*b[3] - a[3]*b[2],
        a[0]*b[2] - a[1]*b[3] + a[2]*b[0] + a[3]*b[1],
        a[0]*b[3] + a[1]*b[2] - a[2]*b[1] + a[3]*b[0],
    ]


def _quaternion_to_euler(q: list[float]) -> dict[str, float]:
    q0, q1, q2, q3 = q
    sinr_cosp = 2.0 * (q0 * q1 + q2 * q3)
    cosr_cosp = 1.0 - 2.0 * (q1 * q1 + q2 * q2)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    sinp = 2.0 * (q0 * q2 - q3 * q1)
    sinp = max(-1.0, min(1.0, sinp))
    pitch = math.asin(sinp)

    siny_cosp = 2.0 * (q0 * q3 + q1 * q2)
    cosy_cosp = 1.0 - 2.0 * (q2 * q2 + q3 * q3)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return {
        "roll": math.degrees(roll),
        "pitch": math.degrees(pitch),
        "yaw": math.degrees(yaw),
    }


def _euler_to_quaternion(roll_deg: float, pitch_deg: float, yaw_deg: float) -> list[float]:
    r = math.radians(roll_deg) / 2.0
    p = math.radians(pitch_deg) / 2.0
    y = math.radians(yaw_deg) / 2.0
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return [
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    ]


def _matrix_identity(n: int) -> list[list[float]]:
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def _matrix_scale(m: list[list[float]], s: float) -> list[list[float]]:
    return [[x * s for x in row] for row in m]


def _matrix_add(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[a[i][j] + b[i][j] for j in range(len(a[0]))] for i in range(len(a))]


def _matrix_trace(m: list[list[float]]) -> float:
    return sum(m[i][i] for i in range(min(len(m), len(m[0]))))


def run_ekf_orientation(
    imu_samples: list[IMUSample],
    profile_id: str = "orientation_9dof",
    *,
    initial_orientation: dict[str, float] | None = None,
) -> EKFResult:
    profile = get_ekf_profile(profile_id)
    if profile is None:
        return EKFResult(
            profile_id=profile_id,
            state=EKFState.diverged,
            message=f"Unknown EKF profile: {profile_id}",
        )

    if not imu_samples:
        return EKFResult(
            profile_id=profile_id,
            state=EKFState.uninitialized,
            message="No IMU samples provided",
        )

    if initial_orientation:
        q = _euler_to_quaternion(
            initial_orientation.get("roll", 0.0),
            initial_orientation.get("pitch", 0.0),
            initial_orientation.get("yaw", 0.0),
        )
    else:
        s0 = imu_samples[0]
        pitch_init = math.atan2(-s0.accel_x, math.sqrt(s0.accel_y**2 + s0.accel_z**2))
        roll_init = math.atan2(s0.accel_y, s0.accel_z)
        q = _euler_to_quaternion(math.degrees(roll_init), math.degrees(pitch_init), 0.0)

    gyro_bias = [0.0, 0.0, 0.0]
    state_dim = profile.state_dim
    P = _matrix_identity(state_dim)
    P = _matrix_scale(P, profile.initial_covariance)

    gyro_noise = profile.process_noise.get("gyro_noise", 0.001)
    gyro_bias_drift = profile.process_noise.get("gyro_bias_drift", 0.0001)
    accel_noise = profile.measurement_noise.get("accel_noise", 0.5)

    iterations = 0
    for i in range(1, len(imu_samples)):
        dt = imu_samples[i].timestamp - imu_samples[i - 1].timestamp
        if dt <= 0:
            dt = 1.0 / 100.0

        s = imu_samples[i]
        wx = s.gyro_x - gyro_bias[0]
        wy = s.gyro_y - gyro_bias[1]
        wz = s.gyro_z - gyro_bias[2]

        omega_norm = math.sqrt(wx**2 + wy**2 + wz**2)
        if omega_norm > 1e-10:
            half_angle = omega_norm * dt / 2.0
            s_ha = math.sin(half_angle) / omega_norm
            dq = [math.cos(half_angle), wx * s_ha, wy * s_ha, wz * s_ha]
        else:
            dq = [1.0, 0.0, 0.0, 0.0]

        q = _quaternion_multiply(q, dq)
        q = _normalize_quaternion(q)

        Q = _matrix_identity(state_dim)
        for j in range(4):
            Q[j][j] = gyro_noise * dt
        for j in range(4, state_dim):
            Q[j][j] = gyro_bias_drift * dt
        P = _matrix_add(P, Q)

        accel_norm = math.sqrt(s.accel_x**2 + s.accel_y**2 + s.accel_z**2)
        if 0.8 < accel_norm / 9.81 < 1.2:
            q0, q1, q2, q3 = q
            gx_pred = 2.0 * (q1 * q3 - q0 * q2) * 9.81
            gy_pred = 2.0 * (q0 * q1 + q2 * q3) * 9.81
            gz_pred = (q0**2 - q1**2 - q2**2 + q3**2) * 9.81

            ex = s.accel_x - gx_pred
            ey = s.accel_y - gy_pred
            ez = s.accel_z - gz_pred

            K_gain = accel_noise
            if K_gain > 0:
                alpha = min(0.1, dt / K_gain)
            else:
                alpha = 0.05

            correction_x = alpha * (q2 * ez - q3 * ey)
            correction_y = alpha * (q3 * ex - q1 * ez)
            correction_z = alpha * (q1 * ey - q2 * ex)

            q[1] += correction_x
            q[2] += correction_y
            q[3] += correction_z
            q = _normalize_quaternion(q)

            bias_alpha = gyro_bias_drift * dt * 10
            gyro_bias[0] -= bias_alpha * ex
            gyro_bias[1] -= bias_alpha * ey
            gyro_bias[2] -= bias_alpha * ez

            decay = max(0.0, 1.0 - alpha * 0.1)
            P = _matrix_scale(P, decay)

        iterations += 1

    cov_trace = _matrix_trace(P)
    euler = _quaternion_to_euler(q)

    if cov_trace < profile.initial_covariance * state_dim * 0.5:
        state = EKFState.converged
    elif iterations > 10:
        state = EKFState.converging
    else:
        state = EKFState.uninitialized

    return EKFResult(
        profile_id=profile_id,
        state=state,
        quaternion=q,
        euler_deg=euler,
        gyro_bias=gyro_bias,
        covariance_trace=cov_trace,
        iterations=iterations,
        message=f"EKF completed: {iterations} iterations, state={state.value}",
    )


def evaluate_ekf_against_fixture(
    ekf_result: EKFResult,
    fixture_id: str,
) -> dict[str, Any]:
    fixture = get_trajectory_fixture(fixture_id)
    if fixture is None:
        return {"passed": False, "error": f"Unknown fixture: {fixture_id}"}

    expected = fixture.expected_orientation or fixture.expected_final_orientation
    if not expected:
        return {"passed": False, "error": "No expected orientation in fixture"}

    euler = ekf_result.euler_deg
    errors = {}
    for axis in ("roll", "pitch", "yaw"):
        key = f"{axis}_deg"
        if key in expected:
            errors[axis] = abs(euler.get(axis, 0.0) - expected[key])

    if not errors:
        return {"passed": False, "error": "No comparable axes found"}

    max_error = max(errors.values())
    rms_error = math.sqrt(sum(e ** 2 for e in errors.values()) / len(errors))

    return {
        "passed": max_error <= fixture.tolerance_deg,
        "fixture_id": fixture_id,
        "tolerance_deg": fixture.tolerance_deg,
        "max_error_deg": max_error,
        "rms_error_deg": rms_error,
        "per_axis_error": errors,
        "expected": expected,
        "actual": euler,
    }


# -- Calibration routines --

def run_imu_calibration(
    static_data: dict[str, list[IMUSample]],
    profile_id: str = "imu_6axis",
) -> CalibrationResult:
    profile = get_calibration_profile(profile_id)
    if profile is None:
        return CalibrationResult(
            profile_id=profile_id,
            status=CalibrationStatus.failed,
            message=f"Unknown calibration profile: {profile_id}",
        )

    if not static_data:
        return CalibrationResult(
            profile_id=profile_id,
            status=CalibrationStatus.failed,
            message="No static data provided",
        )

    accel_means: dict[str, list[float]] = {}
    gyro_all_x: list[float] = []
    gyro_all_y: list[float] = []
    gyro_all_z: list[float] = []
    total_samples = 0

    for position, samples in static_data.items():
        if not samples:
            continue
        ax = sum(s.accel_x for s in samples) / len(samples)
        ay = sum(s.accel_y for s in samples) / len(samples)
        az = sum(s.accel_z for s in samples) / len(samples)
        accel_means[position] = [ax, ay, az]

        gyro_all_x.extend(s.gyro_x for s in samples)
        gyro_all_y.extend(s.gyro_y for s in samples)
        gyro_all_z.extend(s.gyro_z for s in samples)
        total_samples += len(samples)

    gyro_bias = [0.0, 0.0, 0.0]
    if gyro_all_x:
        gyro_bias = [
            sum(gyro_all_x) / len(gyro_all_x),
            sum(gyro_all_y) / len(gyro_all_y),
            sum(gyro_all_z) / len(gyro_all_z),
        ]

    accel_bias = [0.0, 0.0, 0.0]
    accel_scale = [1.0, 1.0, 1.0]

    if len(accel_means) >= 2:
        all_ax = [v[0] for v in accel_means.values()]
        all_ay = [v[1] for v in accel_means.values()]
        all_az = [v[2] for v in accel_means.values()]

        accel_bias = [
            sum(all_ax) / len(all_ax),
            sum(all_ay) / len(all_ay),
            sum(all_az) / len(all_az),
        ]

        range_x = max(all_ax) - min(all_ax)
        range_y = max(all_ay) - min(all_ay)
        range_z = max(all_az) - min(all_az)
        g = 9.81

        if range_x > 0.1:
            accel_scale[0] = (2 * g) / range_x
        if range_y > 0.1:
            accel_scale[1] = (2 * g) / range_y
        if range_z > 0.1:
            accel_scale[2] = (2 * g) / range_z

    residuals = []
    for _pos, mean in accel_means.items():
        corrected = [
            (mean[i] - accel_bias[i]) * accel_scale[i]
            for i in range(3)
        ]
        magnitude = math.sqrt(sum(c**2 for c in corrected))
        residuals.append(abs(magnitude - 9.81))

    residual_g = sum(residuals) / len(residuals) if residuals else 0.0

    status = CalibrationStatus.calibrated if residual_g < 0.5 else CalibrationStatus.failed

    return CalibrationResult(
        profile_id=profile_id,
        status=status,
        accel_bias=accel_bias,
        accel_scale=accel_scale,
        gyro_bias=gyro_bias,
        gyro_scale=[1.0, 1.0, 1.0],
        misalignment_matrix=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        residual_g=residual_g,
        samples_used=total_samples,
        message=f"Calibration {status.value}: {len(accel_means)} positions, "
                f"{total_samples} samples, residual={residual_g:.4f} g",
    )


# -- Sensor test stub runner --

def run_sensor_test(
    recipe_id: str,
    target_device: str,
    *,
    work_dir: str | None = None,
    timeout_s: int = 600,
    **kwargs: Any,
) -> SensorTestResult:
    recipe = get_test_recipe(recipe_id)
    if recipe is None:
        return SensorTestResult(
            recipe_id=recipe_id,
            sensor_type="unknown",
            status=TestStatus.error,
            target_device=target_device,
            message=f"Unknown recipe: {recipe_id!r}. "
                    f"Available: {[r.recipe_id for r in list_test_recipes()]}",
        )

    binary = kwargs.pop("binary", "")
    if binary and shutil.which(binary):
        return _exec_sensor_binary(
            binary, recipe, target_device,
            work_dir=work_dir, timeout_s=timeout_s, **kwargs,
        )

    return SensorTestResult(
        recipe_id=recipe_id,
        sensor_type=recipe.sensor_type,
        status=TestStatus.pending,
        target_device=target_device,
        measurements={
            "category": recipe.category,
            "tools": recipe.tools,
            "sensor_type": recipe.sensor_type,
        },
        message=f"Stub: {recipe.name} — awaiting hardware execution. "
                f"Tools needed: {recipe.tools}.",
    )


def _exec_sensor_binary(
    binary: str,
    recipe: SensorTestRecipe,
    target_device: str,
    *,
    work_dir: str | None = None,
    timeout_s: int = 600,
    **kwargs: Any,
) -> SensorTestResult:
    cmd = [
        binary,
        "--sensor-type", recipe.sensor_type,
        "--recipe", recipe.recipe_id,
        "--device", target_device,
    ]
    output_file = kwargs.get("output_file", "")
    if output_file:
        cmd += ["--output", output_file]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_s, cwd=work_dir,
        )
        passed = proc.returncode == 0
        return SensorTestResult(
            recipe_id=recipe.recipe_id,
            sensor_type=recipe.sensor_type,
            status=TestStatus.passed if passed else TestStatus.failed,
            target_device=target_device,
            raw_log_path=output_file,
            message=proc.stdout[:500] if proc.stdout else proc.stderr[:500],
        )
    except subprocess.TimeoutExpired:
        return SensorTestResult(
            recipe_id=recipe.recipe_id,
            sensor_type=recipe.sensor_type,
            status=TestStatus.error,
            target_device=target_device,
            message=f"Timeout after {timeout_s}s",
        )
    except FileNotFoundError:
        return SensorTestResult(
            recipe_id=recipe.recipe_id,
            sensor_type=recipe.sensor_type,
            status=TestStatus.error,
            target_device=target_device,
            message=f"Binary not found: {binary}",
        )


# -- SoC compatibility check --

def check_soc_compatibility(
    soc_id: str,
    sensor_ids: list[str] | None = None,
) -> dict[str, bool]:
    result: dict[str, bool] = {}

    all_imus = list_imu_drivers()
    all_baros = list_barometer_drivers()

    sensors = []
    if sensor_ids:
        for sid in sensor_ids:
            drv = get_imu_driver(sid)
            if drv:
                sensors.append(("imu", drv.driver_id, drv.compatible_socs))
                continue
            bdrv = get_barometer_driver(sid)
            if bdrv:
                sensors.append(("baro", bdrv.driver_id, bdrv.compatible_socs))
    else:
        for d in all_imus:
            sensors.append(("imu", d.driver_id, d.compatible_socs))
        for d in all_baros:
            sensors.append(("baro", d.driver_id, d.compatible_socs))

    for _stype, sid, compat in sensors:
        if not compat:
            result[sid] = True
        else:
            result[sid] = soc_id.lower() in [s.lower() for s in compat]

    return result


# -- Doc suite generator integration --

_ACTIVE_SF_CERTS: list[dict[str, Any]] = []


def register_sensor_fusion_cert(
    standard: str,
    status: str = "Pending",
    cert_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    _ACTIVE_SF_CERTS.append({
        "standard": standard,
        "status": status,
        "cert_id": cert_id,
        "details": details or {},
    })


def get_sensor_fusion_certs() -> list[dict[str, Any]]:
    return list(_ACTIVE_SF_CERTS)


def clear_sensor_fusion_certs() -> None:
    _ACTIVE_SF_CERTS.clear()


# -- Cert artifact generator --

def generate_cert_artifacts(
    sensor_type: str,
    spec: dict[str, Any] | None = None,
    test_results: list[SensorTestResult] | None = None,
) -> list[SensorFusionCertArtifact]:
    spec = spec or {}
    test_results = test_results or []
    provided = set(spec.get("provided_artifacts", []))
    result_map = {r.recipe_id: r for r in test_results}

    art_defs = list_artifact_definitions()
    artifacts: list[SensorFusionCertArtifact] = []

    for ad in art_defs:
        aid = ad["artifact_id"]
        status = "provided" if aid in provided else "pending"
        artifacts.append(SensorFusionCertArtifact(
            artifact_id=aid,
            name=ad["name"],
            sensor_type=sensor_type,
            status=status,
            description=ad.get("description", ""),
        ))

    return artifacts


# -- Synthetic trajectory generator (for testing) --

def generate_static_trajectory(
    duration_s: float = 10.0,
    sample_rate_hz: int = 100,
    roll_deg: float = 0.0,
    pitch_deg: float = 0.0,
    yaw_deg: float = 0.0,
    accel_noise: float = 0.01,
    gyro_noise: float = 0.001,
) -> list[IMUSample]:
    import random
    g = 9.81
    roll_r = math.radians(roll_deg)
    pitch_r = math.radians(pitch_deg)

    gx = -g * math.sin(pitch_r)
    gy = g * math.sin(roll_r) * math.cos(pitch_r)
    gz = g * math.cos(roll_r) * math.cos(pitch_r)

    samples: list[IMUSample] = []
    n = int(duration_s * sample_rate_hz)
    dt = 1.0 / sample_rate_hz

    for i in range(n):
        t = i * dt
        samples.append(IMUSample(
            timestamp=t,
            accel_x=gx + random.gauss(0, accel_noise),
            accel_y=gy + random.gauss(0, accel_noise),
            accel_z=gz + random.gauss(0, accel_noise),
            gyro_x=random.gauss(0, gyro_noise),
            gyro_y=random.gauss(0, gyro_noise),
            gyro_z=random.gauss(0, gyro_noise),
        ))

    return samples


def generate_rotation_trajectory(
    duration_s: float = 10.0,
    sample_rate_hz: int = 100,
    angular_rate_dps: float = 10.0,
    axis: str = "yaw",
    accel_noise: float = 0.01,
    gyro_noise: float = 0.001,
) -> list[IMUSample]:
    import random
    g = 9.81
    rate_rad = math.radians(angular_rate_dps)

    samples: list[IMUSample] = []
    n = int(duration_s * sample_rate_hz)
    dt = 1.0 / sample_rate_hz

    for i in range(n):
        t = i * dt
        wx = rate_rad if axis == "roll" else 0.0
        wy = rate_rad if axis == "pitch" else 0.0
        wz = rate_rad if axis == "yaw" else 0.0

        samples.append(IMUSample(
            timestamp=t,
            accel_x=random.gauss(0, accel_noise),
            accel_y=random.gauss(0, accel_noise),
            accel_z=g + random.gauss(0, accel_noise),
            gyro_x=wx + random.gauss(0, gyro_noise),
            gyro_y=wy + random.gauss(0, gyro_noise),
            gyro_z=wz + random.gauss(0, gyro_noise),
        ))

    return samples


# -- Audit log integration --

async def log_sensor_test_result(result: SensorTestResult) -> Optional[int]:
    try:
        from backend import audit
        entity_id = f"{result.sensor_type}:{result.recipe_id}"
        return await audit.log(
            action="sensor_test",
            entity_kind="sensor_test_result",
            entity_id=entity_id,
            detail={
                "status": result.status.value,
                "target_device": result.target_device,
                "measurements": result.measurements,
                "message": result.message,
            },
        )
    except Exception as exc:
        logger.debug("audit log failed (non-fatal): %s", exc)
        return None


async def log_ekf_result(result: EKFResult) -> Optional[int]:
    try:
        from backend import audit
        return await audit.log(
            action="ekf_run",
            entity_kind="ekf_result",
            entity_id=result.profile_id,
            detail={
                "state": result.state.value,
                "euler_deg": result.euler_deg,
                "covariance_trace": result.covariance_trace,
                "iterations": result.iterations,
            },
        )
    except Exception as exc:
        logger.debug("audit log failed (non-fatal): %s", exc)
        return None


async def log_calibration_result(result: CalibrationResult) -> Optional[int]:
    try:
        from backend import audit
        return await audit.log(
            action="sensor_calibration",
            entity_kind="calibration_result",
            entity_id=result.profile_id,
            detail={
                "status": result.status.value,
                "accel_bias": result.accel_bias,
                "gyro_bias": result.gyro_bias,
                "residual_g": result.residual_g,
                "samples_used": result.samples_used,
            },
        )
    except Exception as exc:
        logger.debug("audit log failed (non-fatal): %s", exc)
        return None
