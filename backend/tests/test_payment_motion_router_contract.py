from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

from backend.routers.motion_control import router as motion_router
from backend.routers.payment import router as payment_router


def _routes(router) -> list[APIRoute]:
    return [route for route in router.routes if isinstance(route, APIRoute)]


def test_payment_routes_have_response_models() -> None:
    routes = _routes(payment_router)

    assert routes
    missing = [
        f"{sorted(route.methods)} {route.path}"
        for route in routes
        if route.response_model is None
    ]
    assert missing == []


def test_motion_routes_have_response_models() -> None:
    routes = _routes(motion_router)

    assert routes
    missing = [
        f"{sorted(route.methods)} {route.path}"
        for route in routes
        if route.response_model is None
    ]
    assert missing == []


def test_payment_motion_openapi_schema_renders() -> None:
    app = FastAPI()
    app.include_router(payment_router, prefix="/api/v1")
    app.include_router(motion_router, prefix="/api/v1")

    schema = app.openapi()

    assert "/api/v1/payment/pci-dss/levels" in schema["paths"]
    assert "/api/v1/motion/machines" in schema["paths"]
    assert "PCIDSSLevelsResponse" in schema["components"]["schemas"]
    assert "MachineResponse" in schema["components"]["schemas"]
