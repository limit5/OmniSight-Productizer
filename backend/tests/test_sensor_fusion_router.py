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


def test_sensor_fusion_openapi_schema_renders() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    schema = app.openapi()

    assert "/api/v1/sensor-fusion/imu/drivers" in schema["paths"]
    assert "IMUDriversResponse" in schema["components"]["schemas"]
