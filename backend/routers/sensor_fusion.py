"""C14 — L4-CORE-14 Sensor fusion library endpoints (#228).

REST endpoints for IMU driver lookup, GPS protocol queries, barometer
driver queries, EKF profile management, calibration profiles, test
recipe execution, trajectory fixtures, and SoC compatibility checks.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import sensor_fusion as sf

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sensor-fusion", tags=["sensor-fusion"])


class SensorTestRequest(BaseModel):
    recipe_id: str = Field(..., description="Test recipe ID")
    target_device: str = Field(..., description="Target device identifier")
    timeout_s: int = Field(default=600, description="Timeout in seconds")


class EKFRunRequest(BaseModel):
    profile_id: str = Field(default="orientation_9dof", description="EKF profile ID")
    samples: list[dict[str, float]] = Field(..., description="IMU samples [{timestamp, accel_x/y/z, gyro_x/y/z}]")
    initial_orientation: dict[str, float] | None = Field(default=None, description="Initial roll/pitch/yaw in degrees")


class CalibrationRequest(BaseModel):
    profile_id: str = Field(default="imu_6axis", description="Calibration profile ID")
    static_data: dict[str, list[dict[str, float]]] = Field(
        ..., description="Position label → list of IMU samples"
    )


class SocCompatRequest(BaseModel):
    soc_id: str = Field(..., description="SoC identifier")
    sensor_ids: list[str] = Field(default_factory=list, description="Sensor IDs to check (empty = all)")


class NMEAParseRequest(BaseModel):
    sentence: str = Field(..., description="NMEA sentence (starting with $)")


class UBXParseRequest(BaseModel):
    data_hex: str = Field(..., description="UBX message as hex string")


class AltitudeRequest(BaseModel):
    pressure_pa: float = Field(..., description="Pressure in Pascals")
    sea_level_pressure_pa: float = Field(default=101325.0, description="Sea-level pressure reference")


class TrajectoryEvalRequest(BaseModel):
    fixture_id: str = Field(..., description="Trajectory fixture ID")
    euler_deg: dict[str, float] = Field(..., description="EKF result euler angles {roll, pitch, yaw}")
    profile_id: str = Field(default="orientation_9dof")


class ArtifactGenRequest(BaseModel):
    sensor_type: str = Field(..., description="Sensor type (imu, gps, barometer, fusion)")
    provided_artifacts: list[str] = Field(default_factory=list)


class IMUDriverResponse(BaseModel):
    driver_id: str
    name: str
    vendor: str
    bus: str
    i2c_addr_default: str
    i2c_addr_alt: str
    accel_range_g: list[int]
    gyro_range_dps: list[int]
    sample_rate_hz: int
    fifo_depth: int
    axes: int
    temperature: bool
    interrupt_pin: bool
    wake_on_motion: bool
    compatible_socs: list[str]


class IMUDriversResponse(BaseModel):
    count: int
    drivers: list[IMUDriverResponse]


class GPSProtocolResponse(BaseModel):
    protocol_id: str
    name: str
    standard: str
    baud_default: int
    supported_sentences: list[dict[str, str]]
    message_classes: list[dict[str, str]]
    key_messages: list[dict[str, str]]
    talker_ids: list[dict[str, str]]
    checksum: str


class GPSProtocolsResponse(BaseModel):
    count: int
    protocols: list[GPSProtocolResponse]


class NMEAParseResponse(BaseModel):
    sentence_type: str
    talker_id: str
    valid: bool
    fields: dict[str, Any]
    raw: str
    checksum_ok: bool
    error: str


class UBXParseResponse(BaseModel):
    msg_class: str
    msg_id: str
    payload_length: int
    valid: bool
    class_name: str
    msg_name: str
    parsed_fields: dict[str, Any]
    error: str


class BarometerDriverResponse(BaseModel):
    driver_id: str
    name: str
    vendor: str
    bus: str
    i2c_addr_default: str
    pressure_range_hpa: list[float]
    pressure_resolution_pa: float
    temperature_range_c: list[float]
    sample_rate_hz: int
    modes: list[str]
    compatible_socs: list[str]


class BarometerDriversResponse(BaseModel):
    count: int
    drivers: list[BarometerDriverResponse]


class AltitudeResponse(BaseModel):
    altitude_m: float
    pressure_pa: float
    sea_level_pressure_pa: float


class EKFProfileResponse(BaseModel):
    profile_id: str
    name: str
    description: str
    state_dim: int
    measurement_dim: int
    state_vector: list[dict[str, str]]
    process_noise: dict[str, float]
    measurement_noise: dict[str, float]
    initial_covariance: float
    prediction_model: str
    update_model: str


class EKFProfilesResponse(BaseModel):
    count: int
    profiles: list[EKFProfileResponse]


class EKFRunResponse(BaseModel):
    profile_id: str
    state: str
    quaternion: list[float]
    euler_deg: dict[str, float]
    gyro_bias: list[float]
    covariance_trace: float
    iterations: int
    rms_error_deg: float
    timestamp: float
    message: str


class CalibrationProfileResponse(BaseModel):
    profile_id: str
    name: str
    description: str
    parameters: dict[str, dict[str, Any]]
    procedure: list[dict[str, Any]]


class CalibrationProfilesResponse(BaseModel):
    count: int
    profiles: list[CalibrationProfileResponse]


class CalibrationRunResponse(BaseModel):
    profile_id: str
    status: str
    accel_bias: list[float]
    accel_scale: list[float]
    gyro_bias: list[float]
    gyro_scale: list[float]
    misalignment_matrix: list[list[float]]
    residual_g: float
    samples_used: int
    timestamp: float
    message: str


class SensorTestRecipeResponse(BaseModel):
    recipe_id: str
    name: str
    category: str
    description: str
    sensor_type: str
    tools: list[str]
    timeout_s: int


class SensorTestRecipesResponse(BaseModel):
    count: int
    recipes: list[SensorTestRecipeResponse]


class SensorTestRunResponse(BaseModel):
    recipe_id: str
    sensor_type: str
    status: str
    target_device: str
    timestamp: float
    measurements: dict[str, Any]
    raw_log_path: str
    message: str


class TrajectoryFixtureResponse(BaseModel):
    fixture_id: str
    name: str
    duration_s: float
    sample_rate_hz: int
    tolerance_deg: float
    description: str
    expected_orientation: dict[str, float] | None = None
    expected_final_orientation: dict[str, float] | None = None
    angular_rate_dps: float | None = None
    return_to_origin: bool | None = None


class TrajectoryFixturesResponse(BaseModel):
    count: int
    fixtures: list[TrajectoryFixtureResponse]


class TrajectoryEvaluationResponse(BaseModel):
    passed: bool
    error: str | None = None
    fixture_id: str | None = None
    tolerance_deg: float | None = None
    max_error_deg: float | None = None
    rms_error_deg: float | None = None
    per_axis_error: dict[str, float] | None = None
    expected: dict[str, float] | None = None
    actual: dict[str, float] | None = None


class SocCompatResponse(BaseModel):
    soc_id: str
    compatibility: dict[str, bool]


class ArtifactDefinitionResponse(BaseModel):
    artifact_id: str
    name: str
    description: str
    file_pattern: str


class ArtifactDefinitionsResponse(BaseModel):
    count: int
    artifacts: list[ArtifactDefinitionResponse]


class CertArtifactResponse(BaseModel):
    artifact_id: str
    name: str
    sensor_type: str
    status: str
    file_path: str
    description: str


class ArtifactGenerationResponse(BaseModel):
    sensor_type: str
    count: int
    artifacts: list[CertArtifactResponse]


# -- IMU driver endpoints --

@router.get("/imu/drivers", response_model=IMUDriversResponse)
async def list_imu_drivers() -> dict[str, Any]:
    drivers = sf.list_imu_drivers()
    return {
        "count": len(drivers),
        "drivers": [d.to_dict() for d in drivers],
    }


@router.get("/imu/drivers/{driver_id}", response_model=IMUDriverResponse)
async def get_imu_driver(driver_id: str) -> dict[str, Any]:
    driver = sf.get_imu_driver(driver_id)
    if driver is None:
        raise HTTPException(status_code=404, detail=f"IMU driver not found: {driver_id}")
    return driver.to_dict()


# -- GPS protocol endpoints --

@router.get("/gps/protocols", response_model=GPSProtocolsResponse)
async def list_gps_protocols() -> dict[str, Any]:
    protocols = sf.list_gps_protocols()
    return {
        "count": len(protocols),
        "protocols": [p.to_dict() for p in protocols],
    }


@router.get("/gps/protocols/{protocol_id}", response_model=GPSProtocolResponse)
async def get_gps_protocol(protocol_id: str) -> dict[str, Any]:
    proto = sf.get_gps_protocol(protocol_id)
    if proto is None:
        raise HTTPException(status_code=404, detail=f"GPS protocol not found: {protocol_id}")
    return proto.to_dict()


@router.post("/gps/nmea/parse", response_model=NMEAParseResponse)
async def parse_nmea(request: NMEAParseRequest) -> dict[str, Any]:
    result = sf.parse_nmea_sentence(request.sentence)
    return result.to_dict()


@router.post("/gps/ubx/parse", response_model=UBXParseResponse)
async def parse_ubx(request: UBXParseRequest) -> dict[str, Any]:
    try:
        data = bytes.fromhex(request.data_hex)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid hex string")
    result = sf.parse_ubx_message(data)
    return result.to_dict()


# -- Barometer endpoints --

@router.get("/barometer/drivers", response_model=BarometerDriversResponse)
async def list_barometer_drivers() -> dict[str, Any]:
    drivers = sf.list_barometer_drivers()
    return {
        "count": len(drivers),
        "drivers": [d.to_dict() for d in drivers],
    }


@router.get("/barometer/drivers/{driver_id}", response_model=BarometerDriverResponse)
async def get_barometer_driver(driver_id: str) -> dict[str, Any]:
    driver = sf.get_barometer_driver(driver_id)
    if driver is None:
        raise HTTPException(status_code=404, detail=f"Barometer driver not found: {driver_id}")
    return driver.to_dict()


@router.post("/barometer/altitude", response_model=AltitudeResponse)
async def calculate_altitude(request: AltitudeRequest) -> dict[str, Any]:
    alt = sf.pressure_to_altitude(request.pressure_pa, request.sea_level_pressure_pa)
    return {
        "altitude_m": alt,
        "pressure_pa": request.pressure_pa,
        "sea_level_pressure_pa": request.sea_level_pressure_pa,
    }


# -- EKF endpoints --

@router.get("/ekf/profiles", response_model=EKFProfilesResponse)
async def list_ekf_profiles() -> dict[str, Any]:
    profiles = sf.list_ekf_profiles()
    return {
        "count": len(profiles),
        "profiles": [p.to_dict() for p in profiles],
    }


@router.get("/ekf/profiles/{profile_id}", response_model=EKFProfileResponse)
async def get_ekf_profile(profile_id: str) -> dict[str, Any]:
    profile = sf.get_ekf_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"EKF profile not found: {profile_id}")
    return profile.to_dict()


@router.post("/ekf/run", response_model=EKFRunResponse)
async def run_ekf(request: EKFRunRequest) -> dict[str, Any]:
    samples = [
        sf.IMUSample(
            timestamp=s.get("timestamp", 0.0),
            accel_x=s.get("accel_x", 0.0),
            accel_y=s.get("accel_y", 0.0),
            accel_z=s.get("accel_z", 0.0),
            gyro_x=s.get("gyro_x", 0.0),
            gyro_y=s.get("gyro_y", 0.0),
            gyro_z=s.get("gyro_z", 0.0),
            mag_x=s.get("mag_x", 0.0),
            mag_y=s.get("mag_y", 0.0),
            mag_z=s.get("mag_z", 0.0),
        )
        for s in request.samples
    ]
    result = sf.run_ekf_orientation(
        samples,
        profile_id=request.profile_id,
        initial_orientation=request.initial_orientation,
    )
    return result.to_dict()


# -- Calibration endpoints --

@router.get("/calibration/profiles", response_model=CalibrationProfilesResponse)
async def list_calibration_profiles() -> dict[str, Any]:
    profiles = sf.list_calibration_profiles()
    return {
        "count": len(profiles),
        "profiles": [p.to_dict() for p in profiles],
    }


@router.get("/calibration/profiles/{profile_id}", response_model=CalibrationProfileResponse)
async def get_calibration_profile(profile_id: str) -> dict[str, Any]:
    profile = sf.get_calibration_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Calibration profile not found: {profile_id}")
    return profile.to_dict()


@router.post("/calibration/run", response_model=CalibrationRunResponse)
async def run_calibration(request: CalibrationRequest) -> dict[str, Any]:
    static_data: dict[str, list[sf.IMUSample]] = {}
    for position, samples_raw in request.static_data.items():
        static_data[position] = [
            sf.IMUSample(
                timestamp=s.get("timestamp", 0.0),
                accel_x=s.get("accel_x", 0.0),
                accel_y=s.get("accel_y", 0.0),
                accel_z=s.get("accel_z", 0.0),
                gyro_x=s.get("gyro_x", 0.0),
                gyro_y=s.get("gyro_y", 0.0),
                gyro_z=s.get("gyro_z", 0.0),
            )
            for s in samples_raw
        ]
    result = sf.run_imu_calibration(static_data, profile_id=request.profile_id)
    return result.to_dict()


# -- Test recipe endpoints --

@router.get("/test/recipes", response_model=SensorTestRecipesResponse)
async def list_test_recipes(
    sensor_type: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    if sensor_type:
        recipes = sf.get_recipes_by_sensor_type(sensor_type)
    elif category:
        recipes = sf.get_recipes_by_category(category)
    else:
        recipes = sf.list_test_recipes()
    return {
        "count": len(recipes),
        "recipes": [r.to_dict() for r in recipes],
    }


@router.get("/test/recipes/{recipe_id}", response_model=SensorTestRecipeResponse)
async def get_test_recipe(recipe_id: str) -> dict[str, Any]:
    recipe = sf.get_test_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"Test recipe not found: {recipe_id}")
    return recipe.to_dict()


@router.post("/test/run", response_model=SensorTestRunResponse)
async def run_sensor_test(request: SensorTestRequest) -> dict[str, Any]:
    result = sf.run_sensor_test(
        request.recipe_id,
        request.target_device,
        timeout_s=request.timeout_s,
    )
    return result.to_dict()


# -- Trajectory fixture endpoints --

@router.get("/trajectory/fixtures", response_model=TrajectoryFixturesResponse)
async def list_trajectory_fixtures() -> dict[str, Any]:
    fixtures = sf.list_trajectory_fixtures()
    return {
        "count": len(fixtures),
        "fixtures": [f.to_dict() for f in fixtures],
    }


@router.get("/trajectory/fixtures/{fixture_id}", response_model=TrajectoryFixtureResponse)
async def get_trajectory_fixture(fixture_id: str) -> dict[str, Any]:
    fixture = sf.get_trajectory_fixture(fixture_id)
    if fixture is None:
        raise HTTPException(status_code=404, detail=f"Trajectory fixture not found: {fixture_id}")
    return fixture.to_dict()


@router.post("/trajectory/evaluate", response_model=TrajectoryEvaluationResponse)
async def evaluate_trajectory(request: TrajectoryEvalRequest) -> dict[str, Any]:
    ekf_result = sf.EKFResult(
        profile_id=request.profile_id,
        state=sf.EKFState.converged,
        euler_deg=request.euler_deg,
    )
    return sf.evaluate_ekf_against_fixture(ekf_result, request.fixture_id)


# -- SoC compatibility --

@router.post("/soc-compat", response_model=SocCompatResponse)
async def check_soc_compat(request: SocCompatRequest) -> dict[str, Any]:
    compat = sf.check_soc_compatibility(request.soc_id, request.sensor_ids or None)
    return {
        "soc_id": request.soc_id,
        "compatibility": compat,
    }


# -- Artifact endpoints --

@router.get("/artifacts", response_model=ArtifactDefinitionsResponse)
async def list_artifact_definitions() -> dict[str, Any]:
    defs = sf.list_artifact_definitions()
    return {
        "count": len(defs),
        "artifacts": defs,
    }


@router.post("/artifacts/generate", response_model=ArtifactGenerationResponse)
async def generate_artifacts(request: ArtifactGenRequest) -> dict[str, Any]:
    artifacts = sf.generate_cert_artifacts(
        request.sensor_type,
        spec={"provided_artifacts": request.provided_artifacts},
    )
    return {
        "sensor_type": request.sensor_type,
        "count": len(artifacts),
        "artifacts": [a.to_dict() for a in artifacts],
    }
