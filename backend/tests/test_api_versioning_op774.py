"""OP-774 API versioning and compatibility contract tests."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

import pytest
from fastapi import FastAPI

from backend.main import (
    API_V1_PREFIX,
    API_V2_PREFIX,
    DEFAULT_API_VERSION,
    MIN_FRONTEND_API_VERSION,
    SUPPORTED_API_VERSIONS,
    V1_SUNSET_HEADER,
    api_v1_router,
    api_v2_router,
)


def _schema_for(router, prefix: str) -> dict:
    app = FastAPI()
    app.include_router(router, prefix=prefix)
    return app.openapi()


def _normalise_versioned_paths(schema: Mapping, prefix: str) -> dict:
    paths: dict[str, dict] = {}
    for path, methods in schema["paths"].items():
        assert path == prefix or path.startswith(prefix + "/")
        rel = path.removeprefix(prefix) or "/"
        paths[rel] = deepcopy(methods)
    return paths


def _contract_shape(operation: Mapping) -> dict:
    def _normalise_generated_schema(value):
        if isinstance(value, dict):
            return {
                key: _normalise_generated_schema(item)
                for key, item in value.items()
                if key != "title"
            }
        if isinstance(value, list):
            return [_normalise_generated_schema(item) for item in value]
        if isinstance(value, str):
            return value.replace("_api_v1_", "_api_v_").replace("_api_v2_", "_api_v_")
        return value

    return {
        "parameters": _normalise_generated_schema(operation.get("parameters", [])),
        "requestBody": _normalise_generated_schema(operation.get("requestBody", {})),
        "responses": _normalise_generated_schema(operation.get("responses", {})),
    }


def test_versioned_routers_are_separate_instances() -> None:
    assert api_v1_router is not api_v2_router
    assert {route.path for route in api_v1_router.routes} == {
        route.path for route in api_v2_router.routes
    }
    assert "/agents" in {route.path for route in api_v1_router.routes}


@pytest.mark.asyncio
async def test_api_version_endpoint_returns_supported_versions(client) -> None:
    response = await client.get("/api/version")

    assert response.status_code == 200
    body = response.json()
    # OP-774 legacy version-routing fields.
    assert body["supported_versions"] == list(SUPPORTED_API_VERSIONS)
    assert body["default_version"] == DEFAULT_API_VERSION
    assert body["min_frontend_api_version"] == MIN_FRONTEND_API_VERSION
    assert body["deprecated_versions"] == {"v1": {"sunset": V1_SUNSET_HEADER}}
    # OP-1479 bundle-aware additions — the keys must always be present
    # even when /app/bundle.json is missing (values may be None).
    for key in ("bundle_id", "api_required", "api_supported",
                "openapi_hash", "db_migration_head"):
        assert key in body, f"/api/version missing {key}"


@pytest.mark.asyncio
async def test_v1_responses_advertise_deprecation_window(client) -> None:
    response = await client.get(f"{API_V1_PREFIX}/health")

    assert response.status_code == 200
    assert response.headers["Deprecation"] == "true"
    assert response.headers["Sunset"] == V1_SUNSET_HEADER


@pytest.mark.asyncio
async def test_v2_responses_do_not_emit_v1_deprecation_headers(client) -> None:
    response = await client.get(f"{API_V2_PREFIX}/health")

    assert response.status_code == 200
    assert "Deprecation" not in response.headers
    assert "Sunset" not in response.headers


def test_v2_schema_keeps_v1_contract_shapes() -> None:
    """CI tripwire: v2 must keep v1 operation inputs/responses intact."""
    v1_paths = _normalise_versioned_paths(
        _schema_for(api_v1_router, API_V1_PREFIX),
        API_V1_PREFIX,
    )
    v2_paths = _normalise_versioned_paths(
        _schema_for(api_v2_router, API_V2_PREFIX),
        API_V2_PREFIX,
    )

    missing_paths = sorted(set(v1_paths) - set(v2_paths))
    assert not missing_paths, f"v2 is missing v1 paths: {missing_paths[:20]}"

    mismatches: list[str] = []
    for path, v1_methods in v1_paths.items():
        v2_methods = v2_paths[path]
        for method, v1_operation in v1_methods.items():
            if method not in v2_methods:
                mismatches.append(f"{method.upper()} {path}: method missing")
                continue
            if _contract_shape(v1_operation) != _contract_shape(v2_methods[method]):
                mismatches.append(f"{method.upper()} {path}: contract shape changed")

    assert not mismatches, "\n".join(mismatches[:40])
