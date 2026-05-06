"""FS.2.4 -- Account-linking + multi-provider stack scaffold tests."""

from __future__ import annotations

import pytest

from backend.auth_provisioning import (
    AccountLinkingStackOptions,
    UnsupportedAccountLinkingProviderError,
    VendorOAuthAppConfigPlan,
    VendorOAuthAppConfigOptions,
    list_account_linking_stack_providers,
    render_account_linking_stack,
    render_vendor_oauth_app_config_plan,
)


def _plan(provider: str, **overrides):
    kwargs = dict(
        provider=provider,
        app_name="tenant-demo",
        app_base_url="https://app.example.com",
    )
    kwargs.update(overrides)
    return render_vendor_oauth_app_config_plan(VendorOAuthAppConfigOptions(**kwargs))


def _opts(framework: str, *providers: str, **overrides) -> AccountLinkingStackOptions:
    kwargs = dict(
        framework=framework,
        provider_plans=tuple(_plan(p) for p in providers),
    )
    kwargs.update(overrides)
    return AccountLinkingStackOptions(**kwargs)


class TestAccountLinkingStackRegistry:

    def test_list_account_linking_stack_providers_follows_as03_contract(self):
        assert list_account_linking_stack_providers() == [
            "github",
            "google",
            "microsoft",
            "apple",
            "gitlab",
            "bitbucket",
            "slack",
            "notion",
            "salesforce",
            "hubspot",
            "discord",
        ]

    def test_rejects_provider_outside_account_linking_contract(self):
        bad = VendorOAuthAppConfigPlan(
            provider="facebook",
            display_name="Facebook",
            app_name="tenant-demo",
            callback_url="https://app.example.com/api/auth/callback/facebook",
            automation="manual",
            console_url="https://developers.facebook.com/",
            instructions=(),
            required_env=(),
            metadata={},
        )
        with pytest.raises(UnsupportedAccountLinkingProviderError):
            render_account_linking_stack(AccountLinkingStackOptions(
                framework="nextauth",
                provider_plans=(_plan("google"), bad),
            ))

    def test_rejects_duplicate_providers(self):
        with pytest.raises(ValueError, match="duplicate provider"):
            render_account_linking_stack(_opts("nextauth", "google", "google"))


class TestNextAuthAccountLinkingStack:

    def test_renders_nextauth_multi_provider_manifest(self):
        result = render_account_linking_stack(
            _opts("next-auth", "google", "github")
        )
        assert result.framework == "nextauth"
        assert result.dependencies == ("next-auth",)
        assert [item.provider for item in result.providers] == ["google", "github"]
        assert [f.path for f in result.files] == [
            "auth/oauth-provider-stack.ts",
            "auth/account-linking.ts",
            "auth/nextauth.providers.ts",
        ]
        github = result.providers[1]
        assert github.authorize_endpoint == "https://github.com/login/oauth/authorize"
        assert github.token_endpoint == "https://github.com/login/oauth/access_token"

    def test_nextauth_stack_declares_per_provider_env_without_secret_values(self):
        result = render_account_linking_stack(
            _opts("nextauth", "google", "github")
        )
        env = {item.name: item for item in result.env}
        assert env["OAUTH_GOOGLE_CLIENT_ID"].source == "fs.2.3"
        assert env["OAUTH_GOOGLE_CLIENT_SECRET"].sensitive is True
        assert env["OAUTH_GITHUB_CLIENT_SECRET"].sensitive is True
        assert "AUTH_LINK_PASSWORD_CONFIRMATION" in env
        text = "\n".join(f.content for f in result.files)
        assert "clientSecret: process.env[item.clientSecretEnv]" in text
        assert "super-secret-value" not in text

    def test_account_linking_policy_requires_password_confirmation(self):
        result = render_account_linking_stack(
            _opts("nextauth", "google", "github")
        )
        policy = result.files[1].content
        assert 'supportedOAuthAuthMethods = ["oauth_google", "oauth_github"]' in policy
        assert 'existingMethods.includes("password")' in policy
        assert "!existingMethods.includes(method)" in policy
        assert "existingMethods.length > 1" in policy


class TestLuciaAccountLinkingStack:

    def test_renders_lucia_link_routes_per_provider(self):
        result = render_account_linking_stack(_opts("lucia", "google", "apple"))
        assert result.framework == "lucia"
        assert result.dependencies == ("lucia",)
        assert [f.path for f in result.files] == [
            "auth/oauth-provider-stack.ts",
            "auth/account-linking.ts",
            "app/api/auth/google/link/route.ts",
            "app/api/auth/apple/link/route.ts",
        ]
        text = "\n".join(f.content for f in result.files)
        assert "password_confirmation_required" in text
        assert 'authMethod: authMethodForProvider("apple")' in text

    def test_to_dict_is_json_ready_shape(self):
        data = render_account_linking_stack(
            _opts("lucia", "google", "microsoft")
        ).to_dict()
        assert data["framework"] == "lucia"
        assert data["providers"][0]["auth_method"] == "oauth_google"
        assert data["files"][0]["path"] == "auth/oauth-provider-stack.ts"
        assert data["env"][0]["name"] == "OAUTH_GOOGLE_CLIENT_ID"


class TestAccountLinkingStackValidation:

    def test_requires_at_least_two_provider_plans(self):
        opts = AccountLinkingStackOptions(
            framework="nextauth",
            provider_plans=(_plan("google"),),
        )
        with pytest.raises(ValueError, match="at least two providers"):
            render_account_linking_stack(opts)

    @pytest.mark.parametrize(
        "field",
        ["provider_stack_path", "account_linking_path", "route_prefix"],
    )
    def test_required_paths(self, field):
        kwargs = dict(
            framework="lucia",
            provider_plans=(_plan("google"), _plan("github")),
            provider_stack_path="auth/oauth-provider-stack.ts",
            account_linking_path="auth/account-linking.ts",
            route_prefix="app/api/auth",
        )
        kwargs[field] = ""
        opts = AccountLinkingStackOptions(**kwargs)
        with pytest.raises(ValueError, match=field):
            render_account_linking_stack(opts)
