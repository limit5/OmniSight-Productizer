"""FS.2.1 -- Auth0 auth provisioning adapter tests (respx-mocked)."""

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
from backend.auth_provisioning.auth0 import Auth0AuthProvisionAdapter

A = "https://tenant.us.auth0.com/api/v2"


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


def _err(status, msg="err"):
    return httpx.Response(status, json={"message": msg})


def _mk_adapter(**kw):
    return Auth0AuthProvisionAdapter(
        token="mgmt_ABCDEF0123456789",
        application_name="tenant-demo",
        tenant_domain="tenant.us.auth0.com",
        **kw,
    )


class TestSetupApplication:

    @respx.mock
    async def test_creates_client_when_absent(self):
        respx.get(f"{A}/clients").mock(return_value=_ok([]))
        route = respx.post(f"{A}/clients").mock(
            return_value=_ok({
                "client_id": "client_123",
                "client_secret": "secret_123",
                "name": "tenant-demo",
                "app_type": "regular_web",
                "callbacks": ["https://app.example.com/api/auth/callback/auth0"],
                "web_origins": ["https://app.example.com"],
            }, status=201),
        )
        result = await _mk_adapter().setup_application(
            redirect_uris=("https://app.example.com/api/auth/callback/auth0",),
            allowed_logout_urls=("https://app.example.com",),
            allowed_origins=("https://app.example.com",),
        )
        assert result.created is True
        assert result.application_id == "client_123"
        assert result.client_secret == "secret_123"
        assert result.issuer_url == "https://tenant.us.auth0.com/"
        assert result.redirect_uris == ("https://app.example.com/api/auth/callback/auth0",)
        body = route.calls.last.request.read()
        assert b'"app_type":"regular_web"' in body
        assert b'"oidc_conformant":true' in body
        assert b'"grant_types":["authorization_code","refresh_token"]' in body

    @respx.mock
    async def test_reuses_existing_client_by_name(self):
        respx.get(f"{A}/clients").mock(
            return_value=_ok([
                {"client_id": "other", "name": "other"},
                {
                    "client_id": "client_123",
                    "name": "tenant-demo",
                    "callbacks": ["https://app.example.com/callback"],
                },
            ]),
        )
        result = await _mk_adapter().setup_application(
            redirect_uris=("https://fallback.example.com/callback",),
        )
        assert result.created is False
        assert result.application_id == "client_123"
        assert result.redirect_uris == ("https://app.example.com/callback",)

    @respx.mock
    async def test_401_and_403_map_correctly(self):
        respx.get(f"{A}/clients").mock(return_value=_err(401, "bad"))
        with pytest.raises(InvalidAuthProvisionTokenError):
            await _mk_adapter().setup_application(redirect_uris=("https://x/cb",))
        respx.get(f"{A}/clients").mock(return_value=_err(403, "scope"))
        with pytest.raises(MissingAuthProvisionScopeError):
            await _mk_adapter().setup_application(redirect_uris=("https://x/cb",))

    @respx.mock
    async def test_409_maps_to_conflict(self):
        respx.get(f"{A}/clients").mock(return_value=_ok([]))
        respx.post(f"{A}/clients").mock(return_value=_err(409, "taken"))
        with pytest.raises(AuthProvisionConflictError):
            await _mk_adapter().setup_application(redirect_uris=("https://x/cb",))

    @respx.mock
    async def test_429_is_rate_limit(self):
        respx.get(f"{A}/clients").mock(
            return_value=httpx.Response(
                429, headers={"Retry-After": "9"}, json={"message": "slow"},
            ),
        )
        with pytest.raises(AuthProvisionRateLimitError) as excinfo:
            await _mk_adapter().setup_application(redirect_uris=("https://x/cb",))
        assert excinfo.value.retry_after == 9


class TestGetClientConfig:

    @respx.mock
    async def test_config_cached_after_setup(self):
        respx.get(f"{A}/clients").mock(
            return_value=_ok([{"client_id": "client_123", "name": "tenant-demo"}]),
        )
        adapter = _mk_adapter()
        await adapter.setup_application(redirect_uris=("https://app.example.com/callback",))
        config = adapter.get_client_config()
        assert config is not None
        assert config["provider"] == "auth0"
        assert config["client_id"] == "client_123"
