"""FS.2.1 -- WorkOS auth provisioning adapter tests (respx-mocked)."""

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
from backend.auth_provisioning.workos import WORKOS_API_BASE, WorkOSAuthProvisionAdapter

W = WORKOS_API_BASE


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


def _err(status, msg="err"):
    return httpx.Response(status, json={"message": msg})


def _mk_adapter(**kw):
    return WorkOSAuthProvisionAdapter(
        token="sk_test_ABCDEF0123456789",
        application_name="tenant-demo",
        organization_id="org_123",
        **kw,
    )


class TestSetupApplication:

    @respx.mock
    async def test_creates_connect_application_when_absent(self):
        respx.get(f"{W}/connect/applications").mock(return_value=_ok({"data": []}))
        route = respx.post(f"{W}/connect/applications").mock(
            return_value=_ok({
                "connect_application": {
                    "id": "app_123",
                    "client_id": "client_123",
                    "name": "tenant-demo",
                    "application_type": "oauth",
                    "redirect_uris": [
                        {
                            "uri": "https://app.example.com/api/auth/callback/workos",
                            "default": True,
                        },
                    ],
                    "scopes": ["openid", "email", "profile"],
                },
            }, status=201),
        )
        result = await _mk_adapter().setup_application(
            redirect_uris=("https://app.example.com/api/auth/callback/workos",),
            description="Customer-facing auth app",
        )
        assert result.created is True
        assert result.application_id == "app_123"
        assert result.client_id == "client_123"
        assert result.redirect_uris == ("https://app.example.com/api/auth/callback/workos",)
        body = route.calls.last.request.read()
        assert b'"application_type":"oauth"' in body
        assert b'"organization_id":"org_123"' in body
        assert b'"uses_pkce":true' in body

    @respx.mock
    async def test_reuses_existing_application_by_name(self):
        respx.get(f"{W}/connect/applications").mock(
            return_value=_ok({"data": [
                {"id": "app_other", "name": "other"},
                {
                    "id": "app_123",
                    "client_id": "client_123",
                    "name": "tenant-demo",
                    "application_type": "oauth",
                    "redirect_uris": [{"uri": "https://app.example.com/callback"}],
                },
            ]}),
        )
        result = await _mk_adapter().setup_application(
            redirect_uris=("https://fallback.example.com/callback",),
        )
        assert result.created is False
        assert result.application_id == "app_123"
        assert result.redirect_uris == ("https://app.example.com/callback",)

    @respx.mock
    async def test_401_and_403_map_correctly(self):
        respx.get(f"{W}/connect/applications").mock(return_value=_err(401, "bad"))
        with pytest.raises(InvalidAuthProvisionTokenError):
            await _mk_adapter().setup_application(redirect_uris=("https://x/cb",))
        respx.get(f"{W}/connect/applications").mock(return_value=_err(403, "scope"))
        with pytest.raises(MissingAuthProvisionScopeError):
            await _mk_adapter().setup_application(redirect_uris=("https://x/cb",))

    @respx.mock
    async def test_422_maps_to_conflict(self):
        respx.get(f"{W}/connect/applications").mock(return_value=_ok({"data": []}))
        respx.post(f"{W}/connect/applications").mock(return_value=_err(422, "taken"))
        with pytest.raises(AuthProvisionConflictError):
            await _mk_adapter().setup_application(redirect_uris=("https://x/cb",))

    @respx.mock
    async def test_429_is_rate_limit(self):
        respx.get(f"{W}/connect/applications").mock(
            return_value=httpx.Response(
                429, headers={"Retry-After": "13"}, json={"message": "slow"},
            ),
        )
        with pytest.raises(AuthProvisionRateLimitError) as excinfo:
            await _mk_adapter().setup_application(redirect_uris=("https://x/cb",))
        assert excinfo.value.retry_after == 13


class TestGetClientConfig:

    @respx.mock
    async def test_config_cached_after_setup(self):
        respx.get(f"{W}/connect/applications").mock(
            return_value=_ok({"data": [
                {"id": "app_123", "client_id": "client_123", "name": "tenant-demo"},
            ]}),
        )
        adapter = _mk_adapter()
        await adapter.setup_application(redirect_uris=("https://app.example.com/callback",))
        config = adapter.get_client_config()
        assert config is not None
        assert config["provider"] == "workos"
        assert config["client_id"] == "client_123"
