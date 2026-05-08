"""Unit tests for the backend.api_versioning module.

Complements ``test_api_versioning_op774.py`` (which pins the contract
shape between v1 and v2 via the actual mounted app). This file exercises
the module's public helpers in isolation so refactors don't require
spinning up the full FastAPI app.
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from backend import api_versioning as av


def test_api_relative_path_strips_v1() -> None:
    assert av.api_relative_path("/api/v1/health", "/api") == "/health"


def test_api_relative_path_strips_v2() -> None:
    assert av.api_relative_path("/api/v2/agents/123", "/api") == "/agents/123"


def test_api_relative_path_strips_fallback_prefix() -> None:
    assert av.api_relative_path("/api/foo", "/api") == "/foo"


def test_api_relative_path_returns_root_for_bare_prefix() -> None:
    assert av.api_relative_path("/api/v1", "/api") == "/"


def test_api_relative_path_passthrough_for_unprefixed_path() -> None:
    assert av.api_relative_path("/healthz", "/api") == "/healthz"


def test_api_relative_path_does_not_match_partial_v1_prefix() -> None:
    # /api/v1foo must NOT be treated as /api/v1 — strict prefix-with-slash.
    # It IS still under the /api fallback prefix, so it strips to /v1foo
    # (i.e. the v1/v2 specific match returns nothing, falls through to /api).
    assert av.api_relative_path("/api/v1foo", "/api") == "/v1foo"


def test_register_versioned_api_mounts_routers_under_both_prefixes() -> None:
    app = FastAPI()
    sample = APIRouter()

    @sample.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    av.register_versioned_api(app, [sample])

    client = TestClient(app)
    assert client.get("/api/v1/ping").status_code == 200
    assert client.get("/api/v2/ping").status_code == 200


def test_install_deprecation_headers_only_marks_v1() -> None:
    app = FastAPI()
    router = APIRouter()

    @router.get("/probe")
    async def probe() -> dict:
        return {"ok": True}

    av.register_versioned_api(app, [router])
    av.install_deprecation_headers_middleware(app)

    client = TestClient(app)
    v1 = client.get("/api/v1/probe")
    v2 = client.get("/api/v2/probe")

    assert v1.headers.get("Deprecation") == "true"
    assert v1.headers.get("Sunset") == av.V1_SUNSET_HEADER
    assert "Deprecation" not in v2.headers
    assert "Sunset" not in v2.headers


def test_install_version_metadata_endpoint_returns_supported_versions() -> None:
    app = FastAPI()
    av.install_version_metadata_endpoint(app)

    client = TestClient(app)
    resp = client.get("/api/version")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["supported_versions"] == list(av.SUPPORTED_API_VERSIONS)
    assert payload["default_version"] == av.DEFAULT_API_VERSION
    assert payload["min_frontend_api_version"] == av.MIN_FRONTEND_API_VERSION
    assert "v1" in payload["deprecated_versions"]
