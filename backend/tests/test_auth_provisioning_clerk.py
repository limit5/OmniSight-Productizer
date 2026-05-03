"""FS.2.1 -- Clerk auth provisioning adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.auth_provisioning.base import (
    AuthProvisionConflictError,
    AuthProvisionRateLimitError,
    InvalidAuthProvisionTokenError,
    MissingAuthProvisionScopeError,
)
from backend.auth_provisioning.clerk import CLERK_API_BASE, ClerkAuthProvisionAdapter

C = CLERK_API_BASE


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


def _err(status, msg="err"):
    return httpx.Response(status, json={"errors": [{"message": msg}]})


def _mk_adapter(**kw):
    return ClerkAuthProvisionAdapter(
        token="sk_test_ABCDEF0123456789",
        application_name="tenant-demo",
        created_by="user_123",
        publishable_key="pk_test_123",
        issuer_url="https://settled-moth-12.clerk.accounts.dev",
        **kw,
    )


class TestSetupApplication:

    @respx.mock
    async def test_creates_organization_when_absent(self):
        respx.get(f"{C}/organizations").mock(return_value=_ok({"data": []}))
        route = respx.post(f"{C}/organizations").mock(
            return_value=_ok({
                "id": "org_123",
                "name": "tenant-demo",
                "slug": "tenant-demo",
            }, status=201),
        )
        result = await _mk_adapter().setup_application(
            slug="tenant-demo",
            redirect_uris=("https://app.example.com/sso-callback",),
            allowed_origins=("https://app.example.com",),
        )
        assert result.created is True
        assert result.application_id == "org_123"
        assert result.client_id == "pk_test_123"
        assert result.issuer_url == "https://settled-moth-12.clerk.accounts.dev"
        assert result.redirect_uris == ("https://app.example.com/sso-callback",)
        body = route.calls.last.request.read()
        assert b'"created_by":"user_123"' in body
        assert b'"slug":"tenant-demo"' in body
        assert b'"redirect_uris":["https://app.example.com/sso-callback"]' in body

    @respx.mock
    async def test_creates_organization_with_require_mfa_metadata_when_enabled(self):
        respx.get(f"{C}/organizations").mock(return_value=_ok({"data": []}))
        route = respx.post(f"{C}/organizations").mock(
            return_value=_ok({
                "id": "org_123",
                "name": "tenant-demo",
                "slug": "tenant-demo",
                "private_metadata": {"require_mfa": True},
            }, status=201),
        )
        result = await _mk_adapter().setup_application(
            slug="tenant-demo",
            require_mfa=True,
        )
        assert result.require_mfa is True
        assert result.to_dict()["require_mfa"] is True
        body = route.calls.last.request.read()
        assert b'"private_metadata":{"redirect_uris":[],"allowed_origins":[],"require_mfa":true}' in body

    @respx.mock
    async def test_reuses_existing_organization_by_slug(self):
        respx.get(f"{C}/organizations").mock(
            return_value=_ok({"data": [
                {"id": "org_other", "name": "other", "slug": "other"},
                {"id": "org_123", "name": "tenant-demo", "slug": "tenant-demo"},
            ]}),
        )
        result = await _mk_adapter().setup_application(slug="tenant-demo")
        assert result.created is False
        assert result.application_id == "org_123"
        assert result.status == "ready"

    @respx.mock
    async def test_reuses_existing_organization_and_updates_require_mfa_metadata(self):
        respx.get(f"{C}/organizations").mock(
            return_value=_ok({"data": [
                {
                    "id": "org_123",
                    "name": "tenant-demo",
                    "slug": "tenant-demo",
                    "private_metadata": {"keep": "yes"},
                },
            ]}),
        )
        route = respx.patch(f"{C}/organizations/org_123").mock(
            return_value=_ok({
                "id": "org_123",
                "name": "tenant-demo",
                "slug": "tenant-demo",
                "private_metadata": {"keep": "yes", "require_mfa": True},
            }),
        )
        result = await _mk_adapter().setup_application(
            slug="tenant-demo",
            require_mfa=True,
        )
        assert result.created is False
        assert result.require_mfa is True
        body = route.calls.last.request.read()
        assert b'"private_metadata":{"keep":"yes","require_mfa":true}' in body

    @respx.mock
    async def test_401_and_403_map_correctly(self):
        respx.get(f"{C}/organizations").mock(return_value=_err(401, "bad"))
        with pytest.raises(InvalidAuthProvisionTokenError):
            await _mk_adapter().setup_application()
        respx.get(f"{C}/organizations").mock(return_value=_err(403, "scope"))
        with pytest.raises(MissingAuthProvisionScopeError):
            await _mk_adapter().setup_application()

    @respx.mock
    async def test_422_maps_to_conflict(self):
        respx.get(f"{C}/organizations").mock(return_value=_ok({"data": []}))
        respx.post(f"{C}/organizations").mock(return_value=_err(422, "taken"))
        with pytest.raises(AuthProvisionConflictError):
            await _mk_adapter().setup_application()

    @respx.mock
    async def test_429_is_rate_limit(self):
        respx.get(f"{C}/organizations").mock(
            return_value=httpx.Response(
                429, headers={"Retry-After": "7"}, json={"errors": [{"message": "slow"}]},
            ),
        )
        with pytest.raises(AuthProvisionRateLimitError) as excinfo:
            await _mk_adapter().setup_application()
        assert excinfo.value.retry_after == 7


class TestGetClientConfig:

    @respx.mock
    async def test_config_cached_after_setup(self):
        respx.get(f"{C}/organizations").mock(
            return_value=_ok({"data": [
                {"id": "org_123", "name": "tenant-demo", "slug": "tenant-demo"},
            ]}),
        )
        adapter = _mk_adapter()
        await adapter.setup_application(slug="tenant-demo")
        config = adapter.get_client_config()
        assert config is not None
        assert config["provider"] == "clerk"
        assert config["application_id"] == "org_123"
        assert config["require_mfa"] is False
