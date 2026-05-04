from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

from backend.routers.workflow import router


def _workflow_routes() -> list[APIRoute]:
    return [route for route in router.routes if isinstance(route, APIRoute)]


def test_workflow_stateful_routes_have_response_models() -> None:
    routes = _workflow_routes()

    assert len(routes) == 7
    missing = [
        f"{sorted(route.methods)} {route.path}"
        for route in routes
        if route.response_model is None
    ]
    assert missing == []


def test_workflow_openapi_schema_renders() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    schema = app.openapi()

    assert "/api/v1/workflow/runs" in schema["paths"]
    assert "WorkflowRunsResponse" in schema["components"]["schemas"]
