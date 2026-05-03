"""FS.2.2 -- NextAuth.js / Lucia self-hosted scaffold tests."""

from __future__ import annotations

import pytest

from backend.auth_provisioning import (
    AuthProviderSetupResult,
    SelfHostedAuthScaffoldOptions,
    UnsupportedSelfHostedAuthFrameworkError,
    list_self_hosted_frameworks,
    normalize_self_hosted_framework,
    render_self_hosted_auth_scaffold,
)


def _setup_result(**overrides) -> AuthProviderSetupResult:
    kwargs = dict(
        provider="auth0",
        application_id="client_123",
        application_name="tenant-demo",
        client_id="cid_123",
        client_secret="super-secret-value",
        issuer_url="https://tenant.example.auth0.com/",
        redirect_uris=("https://app.example.com/api/auth/callback/auth0",),
        allowed_origins=("https://app.example.com",),
        scopes=("openid", "email", "profile"),
        created=True,
    )
    kwargs.update(overrides)
    return AuthProviderSetupResult(**kwargs)


def _opts(framework: str, **overrides) -> SelfHostedAuthScaffoldOptions:
    kwargs = dict(
        framework=framework,
        provider_setup=_setup_result(),
        app_base_url="https://app.example.com",
        oauth_client_import="../oauth-client",
    )
    kwargs.update(overrides)
    return SelfHostedAuthScaffoldOptions(**kwargs)


class TestFrameworkRegistry:

    def test_list_self_hosted_frameworks(self):
        assert list_self_hosted_frameworks() == ["nextauth", "lucia"]

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("nextauth", "nextauth"),
            ("next-auth", "nextauth"),
            ("authjs", "nextauth"),
            ("lucia", "lucia"),
        ],
    )
    def test_normalize_aliases(self, raw, expected):
        assert normalize_self_hosted_framework(raw) == expected

    def test_rejects_unknown_framework(self):
        with pytest.raises(UnsupportedSelfHostedAuthFrameworkError):
            normalize_self_hosted_framework("passport")


class TestNextAuthScaffold:

    def test_renders_nextauth_manifest(self):
        result = render_self_hosted_auth_scaffold(_opts("next-auth"))
        assert result.framework == "nextauth"
        assert result.provider == "auth0"
        assert result.dependencies == ("next-auth",)
        assert [f.path for f in result.files] == [
            "auth/oauth-client.ts",
            "auth/nextauth.mfa.ts",
            "auth/nextauth.config.ts",
            "app/api/auth/[...nextauth]/route.ts",
        ]

    def test_nextauth_reuses_as1_oauth_client_bridge(self):
        result = render_self_hosted_auth_scaffold(_opts("nextauth"))
        bridge = result.files[0].content
        mfa = result.files[1].content
        config = result.files[2].content
        assert 'from "../oauth-client"' in bridge
        assert "DEFAULT_STATE_TTL_SECONDS" in bridge
        assert "DEFAULT_STATE_TTL_SECONDS" in mfa
        assert 'import { DEFAULT_STATE_TTL_SECONDS } from "./oauth-client"' in config
        assert 'checks: ["pkce", "state"]' in config

    def test_nextauth_renders_mfa_enforcement_scaffold_from_setup_toggle(self):
        result = render_self_hosted_auth_scaffold(
            _opts("nextauth", provider_setup=_setup_result(require_mfa=True))
        )
        mfa = result.files[1].content
        config = result.files[2].content
        env = {item.name: item for item in result.env}
        assert "nextAuthMfaPosture" in mfa
        assert "requiresNextAuthMfaStepUp" in mfa
        assert "nextAuthMfaRedirectUrl" in mfa
        assert "/api/v1/auth/mfa/challenge" in mfa
        assert "/api/v1/auth/mfa/totp/enroll" in mfa
        assert "/api/v1/auth/mfa/webauthn/challenge/complete" in mfa
        assert "DEFAULT_STATE_TTL_SECONDS" in mfa
        assert "|| true" in mfa
        assert 'import { nextAuthMfaCallbacks } from "./nextauth.mfa"' in config
        assert "callbacks: nextAuthMfaCallbacks" in config
        assert env["AUTH_MFA_REQUIRED"].required is True
        assert env["AUTH_MFA_REQUIRED"].source == "sc.8.2"

    def test_nextauth_manifest_does_not_emit_secret_values(self):
        result = render_self_hosted_auth_scaffold(_opts("nextauth"))
        all_content = "\n".join(f.content for f in result.files)
        assert "super-secret-value" not in all_content
        env = {item.name: item for item in result.env}
        assert env["AUTH_CLIENT_SECRET"].sensitive is True
        assert env["AUTH_CLIENT_SECRET"].source == "fs.2.1"
        assert env["AUTH_MFA_REQUIRED"].sensitive is False


class TestLuciaScaffold:

    def test_renders_lucia_manifest(self):
        result = render_self_hosted_auth_scaffold(_opts("lucia"))
        assert result.framework == "lucia"
        assert result.dependencies == ("lucia",)
        assert [f.path for f in result.files] == [
            "auth/oauth-client.ts",
            "auth/lucia.ts",
            "app/api/auth/auth0/route.ts",
            "app/api/auth/auth0/callback/route.ts",
        ]

    def test_lucia_routes_reuse_as1_oauth_client_flow(self):
        result = render_self_hosted_auth_scaffold(_opts("lucia"))
        text = "\n".join(f.content for f in result.files)
        assert 'from "../oauth-client"' in text
        assert "beginAuthorization" in text
        assert "verifyStateAndConsume" in text
        assert "parseTokenResponse" in text
        assert "codeVerifier" in text

    def test_lucia_env_declares_protocol_endpoints(self):
        result = render_self_hosted_auth_scaffold(_opts("lucia"))
        env_names = [item.name for item in result.env]
        assert "AUTH_AUTHORIZE_ENDPOINT" in env_names
        assert "AUTH_TOKEN_ENDPOINT" in env_names
        assert "LUCIA_SESSION_SECRET" in env_names


class TestValidation:

    @pytest.mark.parametrize(
        "field",
        ["provider", "client_id", "issuer_url"],
    )
    def test_required_provider_setup_fields(self, field):
        setup = _setup_result(**{field: ""})
        opts = _opts("nextauth", provider_setup=setup)
        with pytest.raises(ValueError, match=field):
            render_self_hosted_auth_scaffold(opts)

    def test_to_dict_is_json_ready_shape(self):
        result = render_self_hosted_auth_scaffold(_opts("nextauth")).to_dict()
        assert result["framework"] == "nextauth"
        assert result["files"][0]["path"] == "auth/oauth-client.ts"
        assert result["env"][0]["name"] == "AUTH_PROVIDER"
