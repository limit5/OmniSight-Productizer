"""FS.3.1 -- AWS S3 storage provisioning adapter tests (respx-mocked)."""

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
from backend.storage_provisioning.s3 import S3_API_BASE, S3StorageProvisionAdapter

S = S3_API_BASE


def _mk_adapter(**kw):
    return S3StorageProvisionAdapter(
        token="aws_secret_ABCDEF0123456789",
        access_key_id="AKIA0123456789",
        bucket_name="tenant-demo",
        **kw,
    )


class TestProvision:

    @respx.mock
    async def test_creates_bucket_when_absent(self):
        respx.head(f"{S}/tenant-demo").mock(return_value=httpx.Response(404))
        route = respx.put(f"{S}/tenant-demo").mock(return_value=httpx.Response(200))

        result = await _mk_adapter(region="us-west-2").provision_bucket()

        assert result.created is True
        assert result.bucket_id == "tenant-demo"
        assert result.endpoint_url == S
        assert result.region == "us-west-2"
        assert result.status == "ready"
        req = route.calls.last.request
        assert req.headers["authorization"].startswith("AWS4-HMAC-SHA256")
        assert "Credential=AKIA0123456789/" in req.headers["authorization"]
        assert b"<LocationConstraint>us-west-2</LocationConstraint>" in req.read()

    @respx.mock
    async def test_reuses_existing_bucket(self):
        respx.head(f"{S}/tenant-demo").mock(return_value=httpx.Response(200))

        result = await _mk_adapter().provision_bucket()

        assert result.created is False
        assert result.bucket_name == "tenant-demo"
        assert result.region == "us-east-1"

    @respx.mock
    async def test_401_and_403_map_correctly(self):
        respx.head(f"{S}/tenant-demo").mock(return_value=httpx.Response(401, text="bad"))
        with pytest.raises(InvalidStorageProvisionTokenError):
            await _mk_adapter().provision_bucket()

        respx.head(f"{S}/tenant-demo").mock(return_value=httpx.Response(403, text="scope"))
        with pytest.raises(MissingStorageProvisionScopeError):
            await _mk_adapter().provision_bucket()

    @respx.mock
    async def test_409_maps_to_conflict(self):
        respx.head(f"{S}/tenant-demo").mock(return_value=httpx.Response(404))
        respx.put(f"{S}/tenant-demo").mock(return_value=httpx.Response(409, text="taken"))

        with pytest.raises(StorageProvisionConflictError):
            await _mk_adapter().provision_bucket()

    @respx.mock
    async def test_429_is_rate_limit(self):
        respx.head(f"{S}/tenant-demo").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "11"}, text="slow"),
        )

        with pytest.raises(StorageProvisionRateLimitError) as excinfo:
            await _mk_adapter().provision_bucket()
        assert excinfo.value.retry_after == 11


class TestGetBucketConfig:

    @respx.mock
    async def test_config_cached_after_provision(self):
        respx.head(f"{S}/tenant-demo").mock(return_value=httpx.Response(200))

        adapter = _mk_adapter(public_url="https://cdn.example.com/tenant-demo")
        await adapter.provision_bucket()

        assert adapter.get_bucket_config() == {
            "provider": "s3",
            "bucket_name": "tenant-demo",
            "bucket_id": "tenant-demo",
            "endpoint_url": S,
            "public_url": "https://cdn.example.com/tenant-demo",
            "status": "ready",
            "created": False,
            "region": "us-east-1",
        }
