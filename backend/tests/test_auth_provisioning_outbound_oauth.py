"""FS.2b.1 -- Outbound OAuth flow scaffold tests."""

from __future__ import annotations

import pytest

from backend.auth_provisioning import (
    OutboundOAuthFlowScaffoldOptions,
    VendorOAuthAppConfigOptions,
    list_outbound_oauth_flow_providers,
    render_outbound_oauth_flow_scaffold,
    render_vendor_oauth_app_config_plan,
)
from backend.security.oauth_vendors import ALL_VENDOR_IDS


def _plan(provider: str, **overrides):
    kwargs = dict(
        provider=provider,
        app_name="tenant-demo",
        app_base_url="https://app.example.com",
        callback_path="/api/integrations/{provider}/callback",
    )
    kwargs.update(overrides)
    return render_vendor_oauth_app_config_plan(VendorOAuthAppConfigOptions(**kwargs))


def _render(*providers: str, **overrides):
    kwargs = dict(provider_plans=tuple(_plan(provider) for provider in providers))
    kwargs.update(overrides)
    return render_outbound_oauth_flow_scaffold(
        OutboundOAuthFlowScaffoldOptions(**kwargs)
    )


class TestOutboundOAuthRegistry:

    def test_list_outbound_oauth_flow_providers_matches_as1_catalog(self):
        assert list_outbound_oauth_flow_providers() == list(ALL_VENDOR_IDS)


class TestOutboundOAuthScaffold:

    def test_renders_flow_helper_and_provider_routes(self):
        result = _render("github", "slack")
        assert [p.provider for p in result.providers] == ["github", "slack"]
        assert [f.path for f in result.files] == [
            "auth/outbound-oauth-flow.ts",
            "app/api/integrations/github/authorize/route.ts",
            "app/api/integrations/github/callback/route.ts",
            "app/api/integrations/slack/authorize/route.ts",
            "app/api/integrations/slack/callback/route.ts",
        ]

    def test_flow_helper_reuses_as1_authorize_callback_and_token_parser(self):
        result = _render("github", oauth_client_import="../oauth-client")
        helper = result.files[0].content
        assert 'from "../oauth-client"' in helper
        assert "beginAuthorization" in helper
        assert "verifyStateAndConsume" in helper
        assert "parseTokenResponse" in helper
        assert "exchangeOutboundCode" in helper
        assert "code_verifier: flow.codeVerifier" in helper

    def test_provider_metadata_comes_from_vendor_plan_and_as1_catalog(self):
        result = _render("google")
        item = result.providers[0]
        assert item.provider == "google"
        assert item.callback_url == "https://app.example.com/api/integrations/google/callback"
        assert item.scope == ("openid", "email", "profile")
        assert item.authorize_endpoint == "https://accounts.google.com/o/oauth2/v2/auth"
        assert item.token_endpoint == "https://oauth2.googleapis.com/token"
        assert item.extra_authorize_params == (
            ("access_type", "offline"),
            ("prompt", "consent"),
        )
        assert item.is_oidc is True

    def test_routes_render_authorize_cookie_and_callback_token_exchange(self):
        result = _render("notion")
        authorize = result.files[1].content
        callback = result.files[2].content
        assert 'beginOutboundAuthorization("notion")' in authorize
        assert "outbound_oauth_flow_notion=" in authorize
        assert 'outboundProviderById("notion")' in callback
        assert "verifyOutboundCallback(flow, state)" in callback
        assert "exchangeOutboundCode(provider, flow, code)" in callback
        assert "token" in callback

    def test_env_declares_per_provider_secrets_without_values(self):
        result = _render("github")
        env = {item.name: item for item in result.env}
        assert env["OAUTH_GITHUB_CLIENT_ID"].required is True
        assert env["OAUTH_GITHUB_CLIENT_ID"].source == "fs.2b.1"
        assert env["OAUTH_GITHUB_CLIENT_SECRET"].sensitive is True
        text = "\n".join(f.content for f in result.files)
        assert "client-secret-value" not in text

    def test_to_dict_is_json_ready_shape(self):
        data = _render("discord").to_dict()
        assert data["providers"][0]["provider"] == "discord"
        assert data["providers"][0]["scope"] == ["identify", "email"]
        assert data["files"][0]["path"] == "auth/outbound-oauth-flow.ts"
        assert data["env"][1]["sensitive"] is True


class TestOutboundOAuthValidation:

    @pytest.mark.parametrize(
        "field",
        ["provider_plans", "flow_path", "route_prefix", "oauth_client_import"],
    )
    def test_required_fields(self, field):
        kwargs = dict(
            provider_plans=(_plan("github"),),
            flow_path="auth/outbound-oauth-flow.ts",
            route_prefix="app/api/integrations",
            oauth_client_import="@/shared/oauth-client",
        )
        kwargs[field] = () if field == "provider_plans" else ""
        opts = OutboundOAuthFlowScaffoldOptions(**kwargs)
        with pytest.raises(ValueError, match=field):
            render_outbound_oauth_flow_scaffold(opts)

    def test_duplicate_provider_is_rejected(self):
        opts = OutboundOAuthFlowScaffoldOptions(
            provider_plans=(_plan("github"), _plan("github"))
        )
        with pytest.raises(ValueError, match="duplicate provider"):
            render_outbound_oauth_flow_scaffold(opts)
