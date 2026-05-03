"""FS.2.3 -- Vendor OAuth app config plan tests."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from backend.auth_provisioning import (
    VendorOAuthAppConfigOptions,
    list_vendor_oauth_plan_providers,
    render_vendor_oauth_app_config_plan,
)
from backend.security.oauth_vendors import ALL_VENDOR_IDS


def _opts(provider: str, **overrides) -> VendorOAuthAppConfigOptions:
    kwargs = dict(
        provider=provider,
        app_name="tenant-demo",
        app_base_url="https://app.example.com",
    )
    kwargs.update(overrides)
    return VendorOAuthAppConfigOptions(**kwargs)


class TestVendorPlanRegistry:

    def test_list_vendor_oauth_plan_providers_matches_as1_catalog(self):
        assert list_vendor_oauth_plan_providers() == list(ALL_VENDOR_IDS)

    @pytest.mark.parametrize("provider", ALL_VENDOR_IDS)
    def test_every_as1_vendor_renders_plan(self, provider):
        result = render_vendor_oauth_app_config_plan(_opts(provider))
        assert result.provider == provider
        assert result.app_name == "tenant-demo"
        assert result.callback_url.endswith(f"/api/auth/callback/{provider}")
        assert result.required_env == (
            "AUTH_CLIENT_ID",
            "AUTH_CLIENT_SECRET",
            "AUTH_PROVIDER",
        )


class TestGitHubApiAssistedPlan:

    def test_github_uses_manifest_conversion_api(self):
        result = render_vendor_oauth_app_config_plan(
            _opts("github", github_org="acme")
        )
        assert result.automation == "api-assisted"
        assert result.console_url.startswith(
            "https://github.com/organizations/acme/settings/apps/new?"
        )
        qs = parse_qs(urlparse(result.console_url).query)
        assert "manifest" in qs
        assert result.api_requests[0].method == "POST"
        assert result.api_requests[0].url == (
            "https://api.github.com/app-manifests/{code}/conversions"
        )
        assert result.metadata["manifest"]["callback_urls"] == [
            "https://app.example.com/api/auth/callback/github"
        ]

    def test_github_manifest_defaults_to_user_settings_url(self):
        result = render_vendor_oauth_app_config_plan(_opts("github"))
        assert result.console_url.startswith("https://github.com/settings/apps/new?")


class TestManualPlans:

    def test_google_is_manual_with_console_steps(self):
        result = render_vendor_oauth_app_config_plan(_opts("google"))
        assert result.automation == "manual"
        assert result.api_requests == ()
        assert result.console_url == "https://console.cloud.google.com/apis/credentials"
        assert result.metadata["scope"] == ["openid", "email", "profile"]
        assert any("does not expose" in w for w in result.warnings)

    def test_provider_without_scopes_gets_default_permissions_instruction(self):
        result = render_vendor_oauth_app_config_plan(_opts("notion"))
        details = [step.detail for step in result.instructions]
        assert any("does not use scopes" in d for d in details)
        assert any("PKCE as unsupported" in w for w in result.warnings)

    def test_callback_path_can_be_customized(self):
        result = render_vendor_oauth_app_config_plan(
            _opts("discord", callback_path="auth/callback/{provider}")
        )
        assert result.callback_url == "https://app.example.com/auth/callback/discord"

    def test_callback_drift_is_detected(self):
        result = render_vendor_oauth_app_config_plan(
            _opts(
                "google",
                existing_callback_urls=("https://old.example.com/api/auth/callback/google",),
            )
        )
        assert result.callback_changed is True
        assert any("Callback URL changed" in w for w in result.warnings)


class TestPlanValidation:

    @pytest.mark.parametrize(
        "field",
        ["provider", "app_name", "app_base_url", "callback_path"],
    )
    def test_required_fields(self, field):
        kwargs = dict(
            provider="google",
            app_name="tenant-demo",
            app_base_url="https://app.example.com",
            callback_path="/api/auth/callback/{provider}",
        )
        kwargs[field] = ""
        opts = VendorOAuthAppConfigOptions(**kwargs)
        with pytest.raises(ValueError, match=field):
            render_vendor_oauth_app_config_plan(opts)

    def test_unknown_provider_uses_as1_catalog_error(self):
        with pytest.raises(KeyError):
            render_vendor_oauth_app_config_plan(_opts("myspace"))

    def test_to_dict_is_json_ready_shape(self):
        data = render_vendor_oauth_app_config_plan(_opts("google")).to_dict()
        assert data["provider"] == "google"
        assert data["instructions"][0]["title"].startswith("Open Google")
        assert data["api_requests"] == []
        assert data["metadata"]["is_oidc"] is True
