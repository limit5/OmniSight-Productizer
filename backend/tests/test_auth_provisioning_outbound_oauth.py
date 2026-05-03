"""FS.2b.1 -- Outbound OAuth flow scaffold tests."""

from __future__ import annotations

import pytest

from backend.auth_provisioning import (
    OUTBOUND_OAUTH_VENDOR_IDS,
    OutboundOAuthFlowScaffoldOptions,
    OutboundOAuthVendorCatalogOptions,
    VendorOAuthAppConfigOptions,
    get_outbound_oauth_vendor,
    list_outbound_oauth_flow_providers,
    render_outbound_oauth_flow_scaffold,
    render_outbound_oauth_vendor_catalog,
    render_vendor_oauth_app_config_plan,
)


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


def _render_catalog_subset(*providers: str, **overrides):
    catalog = render_outbound_oauth_vendor_catalog(
        OutboundOAuthVendorCatalogOptions(
            app_name="tenant-demo",
            app_base_url="https://app.example.com",
        )
    )
    selected = tuple(plan for plan in catalog if plan.provider in providers)
    kwargs = dict(provider_plans=selected)
    kwargs.update(overrides)
    return render_outbound_oauth_flow_scaffold(
        OutboundOAuthFlowScaffoldOptions(**kwargs)
    )


def _files_by_path(result):
    return {item.path: item.content for item in result.files}


class TestOutboundOAuthRegistry:

    def test_list_outbound_oauth_flow_providers_matches_fs_2b_6_catalog(self):
        assert list_outbound_oauth_flow_providers() == list(OUTBOUND_OAUTH_VENDOR_IDS)

    def test_fs_2b_6_catalog_pins_the_ten_outbound_vendors(self):
        assert OUTBOUND_OAUTH_VENDOR_IDS == (
            "github",
            "slack",
            "google_workspace",
            "microsoft_365",
            "notion",
            "salesforce",
            "hubspot",
            "zoom",
            "stripe_connect",
            "discord",
        )

    def test_get_outbound_oauth_vendor_returns_frozen_catalog_entry(self):
        item = get_outbound_oauth_vendor("zoom")
        assert item.display_name == "Zoom"
        assert item.authorize_endpoint == "https://zoom.us/oauth/authorize"
        assert item.token_endpoint == "https://zoom.us/oauth/token"
        assert item.revocation_endpoint == "https://zoom.us/oauth/revoke"
        assert item.scope == ("user:read", "meeting:read", "meeting:write")

    def test_get_outbound_oauth_vendor_rejects_unknown_vendor(self):
        with pytest.raises(KeyError, match="unknown outbound OAuth vendor"):
            get_outbound_oauth_vendor("myspace")

    def test_render_outbound_oauth_vendor_catalog_returns_plan_shape(self):
        plans = render_outbound_oauth_vendor_catalog(
            OutboundOAuthVendorCatalogOptions(
                app_name="tenant-demo",
                app_base_url="https://app.example.com",
            )
        )
        assert [p.provider for p in plans] == list(OUTBOUND_OAUTH_VENDOR_IDS)
        assert plans[2].provider == "google_workspace"
        assert plans[2].callback_url == (
            "https://app.example.com/api/integrations/google_workspace/callback"
        )
        assert plans[2].metadata["token_vault_provider"] == "google"
        assert plans[7].metadata["token_endpoint"] == "https://zoom.us/oauth/token"


class TestOutboundOAuthScaffold:

    def test_fs_2b_6_catalog_renders_all_ten_provider_routes(self):
        plans = render_outbound_oauth_vendor_catalog(
            OutboundOAuthVendorCatalogOptions(
                app_name="tenant-demo",
                app_base_url="https://app.example.com",
            )
        )
        result = render_outbound_oauth_flow_scaffold(
            OutboundOAuthFlowScaffoldOptions(provider_plans=plans)
        )

        assert [p.provider for p in result.providers] == list(OUTBOUND_OAUTH_VENDOR_IDS)
        assert len(result.providers) == 10
        assert len(result.files) == 5 + (10 * 4)
        route_paths = [f.path for f in result.files]
        assert "app/api/integrations/zoom/authorize/route.ts" in route_paths
        assert "app/api/integrations/stripe_connect/disconnect/route.ts" in route_paths

    def test_fs_2b_6_aliases_google_workspace_and_microsoft_365_to_as2_vault(self):
        plans = render_outbound_oauth_vendor_catalog(
            OutboundOAuthVendorCatalogOptions(
                app_name="tenant-demo",
                app_base_url="https://app.example.com",
            )
        )
        result = render_outbound_oauth_flow_scaffold(
            OutboundOAuthFlowScaffoldOptions(provider_plans=plans)
        )
        support = {
            p.provider: (p.token_vault_supported, p.token_vault_provider)
            for p in result.providers
        }
        assert support["github"] == (True, "github")
        assert support["google_workspace"] == (True, "google")
        assert support["microsoft_365"] == (True, "microsoft")
        assert support["zoom"] == (False, None)

        helper = result.files[0].content
        vault_helper = result.files[1].content
        assert 'provider: "google_workspace"' in helper
        assert 'tokenVaultProvider: "google"' in helper
        assert 'provider: "microsoft_365"' in helper
        assert 'tokenVaultProvider: "microsoft"' in helper
        assert "vaultProvider: string" in vault_helper
        assert "provider.tokenVaultProvider || provider.provider" in "\n".join(
            f.content for f in result.files
        )

    def test_renders_flow_helper_and_provider_routes(self):
        result = _render("github", "slack")
        assert [p.provider for p in result.providers] == ["github", "slack"]
        assert [f.path for f in result.files] == [
            "auth/outbound-oauth-flow.ts",
            "auth/outbound-token-vault.ts",
            "auth/outbound-refresh-middleware.ts",
            "auth/outbound-scope-upgrade.ts",
            "auth/outbound-disconnect.ts",
            "app/api/integrations/github/authorize/route.ts",
            "app/api/integrations/github/callback/route.ts",
            "app/api/integrations/github/scope-upgrade/route.ts",
            "app/api/integrations/github/disconnect/route.ts",
            "app/api/integrations/slack/authorize/route.ts",
            "app/api/integrations/slack/callback/route.ts",
            "app/api/integrations/slack/scope-upgrade/route.ts",
            "app/api/integrations/slack/disconnect/route.ts",
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
        assert item.revocation_endpoint == "https://oauth2.googleapis.com/revoke"
        assert item.extra_authorize_params == (
            ("access_type", "offline"),
            ("prompt", "consent"),
        )
        assert item.is_oidc is True
        assert item.token_vault_supported is True

    def test_routes_render_authorize_cookie_and_callback_token_exchange(self):
        result = _render("notion")
        authorize = result.files[5].content
        callback = result.files[6].content
        assert 'beginOutboundAuthorization("notion")' in authorize
        assert "outbound_oauth_flow_notion=" in authorize
        assert 'outboundProviderById("notion")' in callback
        assert "verifyOutboundCallback(flow, state)" in callback
        assert "exchangeOutboundCode(provider, flow, code)" in callback
        assert "encryptOutboundTokenSet" in callback
        assert "unsupported_token_vault_provider" in callback
        assert "vaultRecord" in callback

    def test_token_vault_helper_reuses_as2_twin_and_encrypts_token_set(self):
        result = _render("github", token_vault_import="../token-vault")
        helper = result.files[1].content
        assert 'from "../token-vault"' in helper
        assert "TokenVault" in helper
        assert "importMasterKey" in helper
        assert "encryptOutboundTokenSet" in helper
        assert "vault.encryptForUser(userId, vaultProvider, token.accessToken)" in helper
        assert "refreshTokenEnc" in helper
        assert "vaultProvider: string" in helper

    def test_refresh_middleware_reuses_as1_auto_refresh_and_as2_vault(self):
        result = _render(
            "github",
            oauth_client_import="../oauth-client",
            token_vault_import="../token-vault",
        )
        helper = result.files[2].content
        assert 'from "../oauth-client"' in helper
        assert 'from "../token-vault"' in helper
        assert "AutoRefreshFetch" in helper
        assert "autoRefresh" in helper
        assert "needsRefresh" in helper
        assert "TokenRefreshError" in helper
        assert "decryptOutboundVaultRecord" in helper
        assert "refreshOutboundVaultRecord" in helper
        assert "createOutboundAutoRefreshFetch" in helper
        assert 'grant_type: "refresh_token"' in helper
        assert "encryptOutboundTokenSet" in helper
        assert "rotated: boolean" in helper

    def test_refresh_middleware_rejects_expired_tokens_without_refresh_token(self):
        result = _render("github")
        helper = result.files[2].content
        assert "stored outbound OAuth token is expired and has no refresh_token" in helper
        assert "refreshTokenEnc" in helper
        assert (
            "expiresAt: record.expiresAt ? Date.parse(record.expiresAt) / 1000 : null"
            in helper
        )

    def test_scope_upgrade_reuses_existing_connection_and_authorizes_missing_scopes(self):
        result = _render("github", oauth_client_import="../oauth-client")
        helper = result.files[3].content
        assert 'from "../oauth-client"' in helper
        assert "beginOutboundScopeUpgrade" in helper
        assert "scope upgrade requires an existing outbound OAuth connection" in helper
        assert "missingScopeValues(current.scope, requestedScopes)" in helper
        assert 'scope_upgrade: "true"' in helper
        assert "scope: mergedScopes" in helper
        assert 'status: "already_granted"' in helper
        assert "disconnectOutbound" not in helper
        assert "revokeOutbound" not in helper

    def test_disconnect_helper_revokes_then_deletes_local_vault_record(self):
        result = _render("google")
        helper = result.files[4].content
        assert "disconnectOutboundOAuth" in helper
        assert 'OutboundOAuthDisconnectTrigger = "user_unlink" | "dsar_erasure"' in helper
        assert "decryptOutboundVaultRecord(record, masterKeyRaw)" in helper
        assert "token.refreshToken || token.accessToken" in helper
        assert 'token_type_hint: hint' in helper
        assert "await store.delete(userId, provider.provider)" in helper
        assert 'revocationOutcome = "revocation_failed"' in helper

    def test_disconnect_route_accepts_dsar_trigger_and_uses_outbound_store(self):
        result = _render("google")
        route = result.files[8].content
        assert "disconnectOutboundOAuth" in route
        assert "outboundTokenStore" in route
        assert 'triggerParam === "dsar_erasure" ? "dsar_erasure" : "user_unlink"' in route
        assert 'disconnectOutboundOAuth(' in route
        assert "OAUTH_TOKEN_VAULT_MASTER_KEY!" in route

    def test_scope_upgrade_merge_preserves_refresh_token_when_provider_omits_it(self):
        result = _render("github")
        helper = result.files[3].content
        assert "mergeOutboundScopeUpgradeToken" in helper
        assert "refreshToken: upgradedToken.refreshToken || previous.refreshToken" in helper
        assert "scope: mergeScopes(previous.scope, upgradedToken.scope)" in helper
        assert "encryptOutboundTokenSet" in helper

    def test_scope_upgrade_route_requires_requested_scope_and_sets_flow_cookie(self):
        result = _render("github")
        route = result.files[7].content
        assert 'beginOutboundScopeUpgrade' in route
        assert 'outboundTokenStore' in route
        assert 'missing_requested_scopes' in route
        assert 'beginOutboundScopeUpgrade(' in route
        assert 'outbound_oauth_flow_github=' in route
        assert 'missingScopes: result.authorization!.missingScopes' in route

    def test_provider_metadata_marks_as2_token_vault_support(self):
        result = _render("github", "slack")
        support = {p.provider: p.token_vault_supported for p in result.providers}
        assert support == {"github": True, "slack": False}
        data = result.to_dict()
        assert data["providers"][0]["token_vault_supported"] is True
        assert data["providers"][1]["token_vault_supported"] is False
        helper = result.files[0].content
        assert "tokenVaultSupported: true" in helper
        assert "tokenVaultSupported: false" in helper
        assert "revocationEndpoint:" in helper

    def test_env_declares_per_provider_secrets_without_values(self):
        result = _render("github")
        env = {item.name: item for item in result.env}
        assert env["OAUTH_GITHUB_CLIENT_ID"].required is True
        assert env["OAUTH_GITHUB_CLIENT_ID"].source == "fs.2b.1"
        assert env["OAUTH_GITHUB_CLIENT_SECRET"].sensitive is True
        assert env["OAUTH_TOKEN_VAULT_MASTER_KEY"].source == "fs.2b.2"
        assert env["OAUTH_TOKEN_VAULT_MASTER_KEY"].sensitive is True
        text = "\n".join(f.content for f in result.files)
        assert "client-secret-value" not in text
        assert "OAUTH_TOKEN_VAULT_MASTER_KEY!" in text

    def test_to_dict_is_json_ready_shape(self):
        data = _render("discord").to_dict()
        assert data["providers"][0]["provider"] == "discord"
        assert data["providers"][0]["scope"] == ["identify", "email"]
        assert data["files"][0]["path"] == "auth/outbound-oauth-flow.ts"
        assert data["env"][1]["sensitive"] is True


class TestOutboundOAuthFullLifecycle:

    def test_fs_2b_7_simulates_three_vendor_connect_use_refresh_revoke(self):
        """Lock the generated-app lifecycle for three AS.2-backed vendors."""
        result = _render_catalog_subset("github", "google_workspace", "microsoft_365")
        files = _files_by_path(result)
        provider_support = {
            item.provider: item.token_vault_provider for item in result.providers
        }

        assert provider_support == {
            "github": "github",
            "google_workspace": "google",
            "microsoft_365": "microsoft",
        }

        flow = files["auth/outbound-oauth-flow.ts"]
        refresh = files["auth/outbound-refresh-middleware.ts"]
        disconnect = files["auth/outbound-disconnect.ts"]
        assert "exchangeOutboundCode" in flow
        assert "createOutboundAutoRefreshFetch" in refresh
        assert "refreshOutboundVaultRecord" in refresh
        assert "disconnectOutboundOAuth" in disconnect

        for provider, vault_provider in provider_support.items():
            authorize = files[f"app/api/integrations/{provider}/authorize/route.ts"]
            callback = files[f"app/api/integrations/{provider}/callback/route.ts"]
            scope_upgrade = files[
                f"app/api/integrations/{provider}/scope-upgrade/route.ts"
            ]
            disconnect_route = files[
                f"app/api/integrations/{provider}/disconnect/route.ts"
            ]

            # connect: authorize starts AS.1 flow, callback exchanges the code,
            # validates state, and encrypts the token set through AS.2.
            assert f'beginOutboundAuthorization("{provider}")' in authorize
            assert f"outbound_oauth_flow_{provider}=" in authorize
            assert f'outboundProviderById("{provider}")' in callback
            assert "verifyOutboundCallback(flow, state)" in callback
            assert "exchangeOutboundCode(provider, flow, code)" in callback
            assert "encryptOutboundTokenSet(" in callback
            assert "provider.tokenVaultProvider || provider.provider" in callback
            assert f'tokenVaultProvider: "{vault_provider}"' in flow

            # use + refresh: generated callers load the stored record, decrypt
            # it, refresh when due, and save rotated tokens back to the store.
            assert "store.load(userId, provider.provider)" in refresh
            assert "decryptOutboundVaultRecord(record, masterKeyRaw)" in refresh
            assert "needsRefresh(current" in refresh
            assert 'grant_type: "refresh_token"' in refresh
            assert "await store.save(next)" in refresh

            # scope upgrade uses the same connection rather than reconnecting.
            assert "beginOutboundScopeUpgrade(" in scope_upgrade
            assert "outboundTokenStore" in scope_upgrade

            # revoke: disconnect accepts user unlink / DSAR and erases the local
            # vault record after the helper's best-effort provider revoke path.
            assert f'"{provider}"' in disconnect_route
            assert "disconnectOutboundOAuth(" in disconnect_route
            assert "outboundTokenStore" in disconnect_route
            assert 'triggerParam === "dsar_erasure" ? "dsar_erasure"' in disconnect_route
            assert "await store.delete(userId, provider.provider)" in disconnect

    def test_fs_2b_7_three_vendor_lifecycle_pins_vendor_specific_edges(self):
        result = _render_catalog_subset("github", "google_workspace", "microsoft_365")
        providers = {item.provider: item for item in result.providers}

        assert providers["github"].revocation_endpoint is None
        assert providers["github"].extra_authorize_params == (("allow_signup", "true"),)
        assert providers["google_workspace"].revocation_endpoint == (
            "https://oauth2.googleapis.com/revoke"
        )
        assert providers["google_workspace"].extra_authorize_params == (
            ("access_type", "offline"),
            ("prompt", "consent"),
        )
        assert providers["microsoft_365"].revocation_endpoint is None
        assert "offline_access" in providers["microsoft_365"].scope

        env = {item.name: item for item in result.env}
        for name in (
            "OAUTH_GITHUB_CLIENT_ID",
            "OAUTH_GITHUB_CLIENT_SECRET",
            "OAUTH_GOOGLE_WORKSPACE_CLIENT_ID",
            "OAUTH_GOOGLE_WORKSPACE_CLIENT_SECRET",
            "OAUTH_MICROSOFT_365_CLIENT_ID",
            "OAUTH_MICROSOFT_365_CLIENT_SECRET",
            "OAUTH_TOKEN_VAULT_MASTER_KEY",
        ):
            assert env[name].required is True

        assert env["OAUTH_GITHUB_CLIENT_SECRET"].sensitive is True
        assert env["OAUTH_GOOGLE_WORKSPACE_CLIENT_SECRET"].sensitive is True
        assert env["OAUTH_MICROSOFT_365_CLIENT_SECRET"].sensitive is True


class TestOutboundOAuthValidation:

    @pytest.mark.parametrize(
        "field",
        [
            "provider_plans",
            "flow_path",
            "route_prefix",
            "oauth_client_import",
            "token_vault_import",
            "token_vault_path",
            "refresh_middleware_path",
            "scope_upgrade_path",
            "disconnect_path",
        ],
    )
    def test_required_fields(self, field):
        kwargs = dict(
            provider_plans=(_plan("github"),),
            flow_path="auth/outbound-oauth-flow.ts",
            route_prefix="app/api/integrations",
            oauth_client_import="@/shared/oauth-client",
            token_vault_import="@/shared/token-vault",
            token_vault_path="auth/outbound-token-vault.ts",
            refresh_middleware_path="auth/outbound-refresh-middleware.ts",
            scope_upgrade_path="auth/outbound-scope-upgrade.ts",
            disconnect_path="auth/outbound-disconnect.ts",
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
