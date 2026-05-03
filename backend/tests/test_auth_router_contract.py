from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

from backend.routers.auth import router


def _auth_routes() -> list[APIRoute]:
    return [route for route in router.routes if isinstance(route, APIRoute)]


def test_login_route_has_response_model() -> None:
    login_routes = [
        route
        for route in _auth_routes()
        if route.path == "/auth/login" and route.methods == {"POST"}
    ]

    assert len(login_routes) == 1
    assert login_routes[0].response_model is not None
    assert login_routes[0].response_model_exclude_none is True


def test_auth_login_openapi_schema_renders() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    schema = app.openapi()

    assert "/api/v1/auth/login" in schema["paths"]
    assert "LoginResponse" in schema["components"]["schemas"]
