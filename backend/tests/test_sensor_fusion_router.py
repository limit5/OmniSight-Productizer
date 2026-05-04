from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

from backend.routers.sensor_fusion import router


def _sensor_fusion_routes() -> list[APIRoute]:
    return [route for route in router.routes if isinstance(route, APIRoute)]


def test_sensor_fusion_routes_have_response_models() -> None:
    routes = _sensor_fusion_routes()

    assert routes
    missing = [
        f"{sorted(route.methods)} {route.path}"
        for route in routes
        if route.response_model is None
    ]
    assert missing == []


def test_sensor_fusion_response_model_contracts() -> None:
    routes = _sensor_fusion_routes()

    route_models = {
        route.path: route.response_model.__name__
        for route in routes
        if route.response_model is not None
    }

    assert route_models == {
        "/sensor-fusion/imu/drivers": "IMUDriversResponse",
        "/sensor-fusion/imu/drivers/{driver_id}": "IMUDriverResponse",
        "/sensor-fusion/gps/protocols": "GPSProtocolsResponse",
        "/sensor-fusion/gps/protocols/{protocol_id}": "GPSProtocolResponse",
        "/sensor-fusion/gps/nmea/parse": "NMEAParseResponse",
        "/sensor-fusion/gps/ubx/parse": "UBXParseResponse",
        "/sensor-fusion/barometer/drivers": "BarometerDriversResponse",
        "/sensor-fusion/barometer/drivers/{driver_id}": "BarometerDriverResponse",
        "/sensor-fusion/barometer/altitude": "AltitudeResponse",
        "/sensor-fusion/ekf/profiles": "EKFProfilesResponse",
        "/sensor-fusion/ekf/profiles/{profile_id}": "EKFProfileResponse",
        "/sensor-fusion/ekf/run": "EKFRunResponse",
        "/sensor-fusion/calibration/profiles": "CalibrationProfilesResponse",
        "/sensor-fusion/calibration/profiles/{profile_id}": "CalibrationProfileResponse",
        "/sensor-fusion/calibration/run": "CalibrationRunResponse",
        "/sensor-fusion/test/recipes": "SensorTestRecipesResponse",
        "/sensor-fusion/test/recipes/{recipe_id}": "SensorTestRecipeResponse",
        "/sensor-fusion/test/run": "SensorTestRunResponse",
        "/sensor-fusion/trajectory/fixtures": "TrajectoryFixturesResponse",
        "/sensor-fusion/trajectory/fixtures/{fixture_id}": "TrajectoryFixtureResponse",
        "/sensor-fusion/trajectory/evaluate": "TrajectoryEvaluationResponse",
        "/sensor-fusion/soc-compat": "SocCompatResponse",
        "/sensor-fusion/artifacts": "ArtifactDefinitionsResponse",
        "/sensor-fusion/artifacts/generate": "ArtifactGenerationResponse",
    }


def test_sensor_fusion_openapi_schema_renders() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    schema = app.openapi()

    assert "/api/v1/sensor-fusion/imu/drivers" in schema["paths"]
    assert "IMUDriversResponse" in schema["components"]["schemas"]
