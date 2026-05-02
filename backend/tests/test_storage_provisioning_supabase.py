"""FS.3.1 -- Supabase Storage provisioning adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.storage_provisioning.base import (
    InvalidStorageProvisionTokenError,
    MissingStorageProvisionScopeError,
    StorageProvisionConflictError,
    StorageProvisionRateLimitError,
)
from backend.storage_provisioning.supabase import (
    SUPABASE_STORAGE_API_BASE,
    SupabaseStorageProvisionAdapter,
)

S = f"{SUPABASE_STORAGE_API_BASE}/v1/projects/prj_123/storage"


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


def _err(status, msg="err"):
    return httpx.Response(status, json={"message": msg})


def _mk_adapter(**kw):
    return SupabaseStorageProvisionAdapter(
        token="sbp_ABCDEF0123456789",
        bucket_name="tenant-demo",
        project_ref="prj_123",
        **kw,
    )


class TestProvision:

    @respx.mock
    async def test_creates_bucket_when_absent(self):
        respx.get(f"{S}/buckets/tenant-demo").mock(return_value=_err(404, "missing"))
        route = respx.post(f"{S}/buckets").mock(
            return_value=_ok({
                "id": "tenant-demo",
                "name": "tenant-demo",
                "public": True,
                "status": "ready",
                "region": "us-east-1",
            }, status=201),
        )

        result = await _mk_adapter(
            public=True,
            file_size_limit=1048576,
            allowed_mime_types=["image/png"],
        ).provision_bucket()

        assert result.created is True
        assert result.bucket_id == "tenant-demo"
        assert result.region == "us-east-1"
        assert result.public_url == (
            "https://prj_123.supabase.co/storage/v1/object/public/tenant-demo"
        )
        body = route.calls.last.request.read()
        assert b'"id":"tenant-demo"' in body
        assert b'"public":true' in body
        assert b'"file_size_limit":1048576' in body
        assert b'"allowed_mime_types":["image/png"]' in body

    @respx.mock
    async def test_reuses_existing_bucket(self):
        respx.get(f"{S}/buckets/tenant-demo").mock(
            return_value=_ok({
                "id": "tenant-demo",
                "name": "tenant-demo",
                "public": False,
                "status": "ready",
            }),
        )

        result = await _mk_adapter().provision_bucket()

        assert result.created is False
        assert result.bucket_name == "tenant-demo"
        assert result.public_url is None

    @respx.mock
    async def test_401_and_403_map_correctly(self):
        respx.get(f"{S}/buckets/tenant-demo").mock(return_value=_err(401, "bad"))
        with pytest.raises(InvalidStorageProvisionTokenError):
            await _mk_adapter().provision_bucket()

        respx.get(f"{S}/buckets/tenant-demo").mock(return_value=_err(403, "scope"))
        with pytest.raises(MissingStorageProvisionScopeError):
            await _mk_adapter().provision_bucket()

    @respx.mock
    async def test_422_maps_to_conflict(self):
        respx.get(f"{S}/buckets/tenant-demo").mock(return_value=_err(404, "missing"))
        respx.post(f"{S}/buckets").mock(return_value=_err(422, "taken"))

        with pytest.raises(StorageProvisionConflictError):
            await _mk_adapter().provision_bucket()

    @respx.mock
    async def test_429_is_rate_limit(self):
        respx.get(f"{S}/buckets/tenant-demo").mock(
            return_value=httpx.Response(
                429,
                headers={"Retry-After": "7"},
                json={"message": "slow"},
            ),
        )

        with pytest.raises(StorageProvisionRateLimitError) as excinfo:
            await _mk_adapter().provision_bucket()
        assert excinfo.value.retry_after == 7


class TestGetBucketConfig:

    @respx.mock
    async def test_config_cached_after_provision(self):
        respx.get(f"{S}/buckets/tenant-demo").mock(
            return_value=_ok({"id": "tenant-demo", "name": "tenant-demo"}),
        )

        adapter = _mk_adapter()
        await adapter.provision_bucket()

        assert adapter.get_bucket_config() == {
            "provider": "supabase-storage",
            "bucket_name": "tenant-demo",
            "bucket_id": "tenant-demo",
            "endpoint_url": S,
            "public_url": None,
            "status": "ready",
            "created": False,
            "region": None,
        }
