"""FS.3.1 -- Cloudflare R2 storage provisioning adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import respx
from urllib.parse import parse_qs, urlparse

from backend.storage_provisioning.r2 import R2StorageProvisionAdapter

R2 = "https://acct_123.r2.cloudflarestorage.com"


def _mk_adapter(**kw):
    return R2StorageProvisionAdapter(
        token="r2_secret_ABCDEF0123456789",
        access_key_id="r2_access_123",
        account_id="acct_123",
        bucket_name="tenant-demo",
        **kw,
    )


class TestProvision:

    @respx.mock
    async def test_creates_bucket_with_default_r2_endpoint(self):
        respx.head(f"{R2}/tenant-demo").mock(return_value=httpx.Response(404))
        route = respx.put(f"{R2}/tenant-demo").mock(return_value=httpx.Response(200))

        result = await _mk_adapter().provision_bucket()

        assert result.provider == "r2"
        assert result.created is True
        assert result.endpoint_url == R2
        assert result.region == "auto"
        assert "Credential=r2_access_123/" in route.calls.last.request.headers["authorization"]

    @respx.mock
    async def test_reuses_existing_bucket_with_custom_endpoint(self):
        endpoint = "https://custom-r2.example.com"
        respx.head(f"{endpoint}/tenant-demo").mock(return_value=httpx.Response(200))

        result = await _mk_adapter(endpoint_url=endpoint).provision_bucket()

        assert result.created is False
        assert result.endpoint_url == endpoint

    @respx.mock
    async def test_configures_cors_with_r2_endpoint(self):
        respx.head(f"{R2}/tenant-demo").mock(return_value=httpx.Response(200))
        route = respx.put(f"{R2}/tenant-demo?cors").mock(return_value=httpx.Response(200))

        await _mk_adapter(
            cors_allowed_origins=["https://app.example.com"],
        ).provision_bucket()

        assert b"<AllowedOrigin>https://app.example.com</AllowedOrigin>" in (
            route.calls.last.request.read()
        )
        assert "/auto/s3/aws4_request" in route.calls.last.request.headers["authorization"]


class TestPresignedUrl:

    async def test_generates_r2_signed_url_with_auto_region(self):
        result = await _mk_adapter().generate_presigned_url("images/logo.png")

        parsed = urlparse(result.url)
        query = parse_qs(parsed.query)
        assert result.provider == "r2"
        assert parsed.netloc == "acct_123.r2.cloudflarestorage.com"
        assert parsed.path == "/tenant-demo/images/logo.png"
        assert "/auto/s3/aws4_request" in query["X-Amz-Credential"][0]
        assert query["X-Amz-Signature"][0]
