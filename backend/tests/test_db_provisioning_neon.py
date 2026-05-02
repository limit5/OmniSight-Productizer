"""FS.1.1 — Neon DB provisioning adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.db_provisioning.base import (
    DBProvisionConflictError,
    DBProvisionRateLimitError,
    InvalidDBProvisionTokenError,
    MissingDBProvisionScopeError,
)
from backend.db_provisioning.neon import NEON_API_BASE, NeonDBProvisionAdapter

N = NEON_API_BASE


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


def _err(status, msg="err"):
    return httpx.Response(status, json={"message": msg})


def _mk_adapter(**kw):
    return NeonDBProvisionAdapter(
        token="napi_ABCDEF0123456789",
        database_name="tenant-demo",
        **kw,
    )


class TestProvision:

    @respx.mock
    async def test_creates_project_when_absent(self):
        respx.get(f"{N}/projects").mock(return_value=_ok({"projects": []}))
        route = respx.post(f"{N}/projects").mock(
            return_value=_ok({
                "project": {
                    "id": "prj_123",
                    "name": "tenant-demo",
                    "region_id": "aws-us-east-1",
                },
                "connection_uris": [
                    {"connection_uri": "postgresql://user:pass@ep.example/neondb"},
                ],
            }, status=201),
        )
        result = await _mk_adapter().provision_database(pg_version=16)
        assert result.created is True
        assert result.database_id == "prj_123"
        assert result.connection_url == "postgresql://user:pass@ep.example/neondb"
        body = route.calls.last.request.read()
        assert b'"name":"tenant-demo"' in body
        assert b'"pg_version":16' in body

    @respx.mock
    async def test_reuses_existing_project_by_name(self):
        respx.get(f"{N}/projects").mock(
            return_value=_ok({"projects": [
                {"id": "other", "name": "other"},
                {"id": "prj_123", "name": "tenant-demo", "region_id": "aws-us-east-1"},
            ]}),
        )
        result = await _mk_adapter().provision_database()
        assert result.created is False
        assert result.database_id == "prj_123"
        assert result.connection_url is None

    @respx.mock
    async def test_401_and_403_map_correctly(self):
        respx.get(f"{N}/projects").mock(return_value=_err(401, "bad"))
        with pytest.raises(InvalidDBProvisionTokenError):
            await _mk_adapter().provision_database()
        respx.get(f"{N}/projects").mock(return_value=_err(403, "scope"))
        with pytest.raises(MissingDBProvisionScopeError):
            await _mk_adapter().provision_database()

    @respx.mock
    async def test_409_maps_to_conflict(self):
        respx.get(f"{N}/projects").mock(return_value=_ok({"projects": []}))
        respx.post(f"{N}/projects").mock(return_value=_err(409, "taken"))
        with pytest.raises(DBProvisionConflictError):
            await _mk_adapter().provision_database()

    @respx.mock
    async def test_429_is_rate_limit(self):
        respx.get(f"{N}/projects").mock(
            return_value=httpx.Response(
                429, headers={"Retry-After": "11"}, json={"message": "slow"},
            ),
        )
        with pytest.raises(DBProvisionRateLimitError) as excinfo:
            await _mk_adapter().provision_database()
        assert excinfo.value.retry_after == 11


class TestGetConnectionUrl:

    @respx.mock
    async def test_url_cached_after_create(self):
        respx.get(f"{N}/projects").mock(return_value=_ok({"projects": []}))
        respx.post(f"{N}/projects").mock(
            return_value=_ok({
                "project": {"id": "prj_123", "name": "tenant-demo"},
                "connection_uri": "postgresql://user:pass@ep.example/neondb",
            }, status=201),
        )
        adapter = _mk_adapter()
        await adapter.provision_database()
        assert adapter.get_connection_url() == "postgresql://user:pass@ep.example/neondb"
