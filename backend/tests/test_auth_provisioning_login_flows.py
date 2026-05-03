"""FS.2.6 -- Main five inbound-auth provider login flow tests.

FS.2 covers the five mainstream inbound-auth choices from the roadmap:
Clerk, Auth0, WorkOS, NextAuth.js, and Lucia. These tests are a small
capstone over FS.2.1-FS.2.5: the managed providers must return a usable
OIDC login config, and the two self-hosted frameworks must render the
authorize/callback, account-linking, and email+MFA pieces that complete
the generated-app login flow.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.auth_provisioning import (
    AccountLinkingStackOptions,
    AuthProviderSetupResult,
    EmailMfaBaselineOptions,
    SelfHostedAuthScaffoldOptions,
    VendorOAuthAppConfigOptions,
    render_account_linking_stack,
    render_email_mfa_baseline,
    render_self_hosted_auth_scaffold,
    render_vendor_oauth_app_config_plan,
)
from backend.auth_provisioning.auth0 import Auth0AuthProvisionAdapter
from backend.auth_provisioning.clerk import CLERK_API_BASE, ClerkAuthProvisionAdapter
from backend.auth_provisioning.workos import WORKOS_API_BASE, WorkOSAuthProvisionAdapter


APP_BASE_URL = "https://app.example.com"
APP_NAME = "tenant-demo"
MAIN_INBOUND_AUTH_PROVIDERS = ("clerk", "auth0", "workos", "nextauth", "lucia")
MANAGED_AUTH_PROVIDERS = ("clerk", "auth0", "workos")
SELF_HOSTED_FRAMEWORKS = ("nextauth", "lucia")
ACCOUNT_LINKING_PROVIDERS = ("google", "github", "microsoft", "apple")


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


def _callback(provider: str) -> str:
    return f"{APP_BASE_URL}/api/auth/callback/{provider}"


def _setup_result(**overrides) -> AuthProviderSetupResult:
    kwargs = dict(
        provider="auth0",
        application_id="client_123",
        application_name=APP_NAME,
        client_id="client_123",
        client_secret="secret_123",
        issuer_url="https://tenant.us.auth0.com/",
        redirect_uris=(_callback("auth0"),),
        allowed_origins=(APP_BASE_URL,),
        scopes=("openid", "email", "profile"),
        created=True,
    )
    kwargs.update(overrides)
    return AuthProviderSetupResult(**kwargs)


async def _setup_managed_provider(provider: str) -> AuthProviderSetupResult:
    if provider == "clerk":
        respx.get(f"{CLERK_API_BASE}/organizations").mock(
            return_value=_ok({"data": []})
        )
        respx.post(f"{CLERK_API_BASE}/organizations").mock(
            return_value=_ok(
                {
                    "id": "org_123",
                    "name": APP_NAME,
                    "slug": APP_NAME,
                },
                status=201,
            )
        )
        adapter = ClerkAuthProvisionAdapter(
            token="sk_test_ABCDEF0123456789",
            application_name=APP_NAME,
            created_by="user_123",
            publishable_key="pk_test_123",
            issuer_url="https://settled-moth-12.clerk.accounts.dev",
        )
    elif provider == "auth0":
        api_base = "https://tenant.us.auth0.com/api/v2"
        respx.get(f"{api_base}/clients").mock(return_value=_ok([]))
        respx.post(f"{api_base}/clients").mock(
            return_value=_ok(
                {
                    "client_id": "client_123",
                    "client_secret": "secret_123",
                    "name": APP_NAME,
                    "app_type": "regular_web",
                    "callbacks": [_callback("auth0")],
                    "web_origins": [APP_BASE_URL],
                },
                status=201,
            )
        )
        adapter = Auth0AuthProvisionAdapter(
            token="mgmt_ABCDEF0123456789",
            application_name=APP_NAME,
            tenant_domain="tenant.us.auth0.com",
        )
    elif provider == "workos":
        respx.get(f"{WORKOS_API_BASE}/connect/applications").mock(
            return_value=_ok({"data": []})
        )
        respx.post(f"{WORKOS_API_BASE}/connect/applications").mock(
            return_value=_ok(
                {
                    "connect_application": {
                        "id": "app_123",
                        "client_id": "client_123",
                        "name": APP_NAME,
                        "application_type": "oauth",
                        "redirect_uris": [
                            {"uri": _callback("workos"), "default": True}
                        ],
                        "scopes": ["openid", "email", "profile"],
                    },
                },
                status=201,
            )
        )
        adapter = WorkOSAuthProvisionAdapter(
            token="sk_test_ABCDEF0123456789",
            application_name=APP_NAME,
            organization_id="org_123",
        )
    else:
        raise AssertionError(f"unexpected managed provider {provider}")

    result = await adapter.setup_application(
        slug=APP_NAME,
        redirect_uris=(_callback(provider),),
        allowed_origins=(APP_BASE_URL,),
    )
    config = adapter.get_client_config()
    assert config is not None
    assert config["provider"] == provider
    assert config["application_name"] == APP_NAME
    return result


def _vendor_plan(provider: str):
    return render_vendor_oauth_app_config_plan(
        VendorOAuthAppConfigOptions(
            provider=provider,
            app_name=APP_NAME,
            app_base_url=APP_BASE_URL,
        )
    )


def test_fs_2_6_pins_the_main_five_provider_set():
    assert MAIN_INBOUND_AUTH_PROVIDERS == (
        MANAGED_AUTH_PROVIDERS + SELF_HOSTED_FRAMEWORKS
    )


class TestManagedProviderLoginConfigs:

    @respx.mock
    @pytest.mark.parametrize("provider", MANAGED_AUTH_PROVIDERS)
    async def test_managed_provider_setup_yields_oidc_login_config(self, provider):
        result = await _setup_managed_provider(provider)

        assert result.provider == provider
        assert result.application_name == APP_NAME
        assert result.application_id
        assert result.client_id
        assert result.issuer_url
        assert result.redirect_uris == (_callback(provider),)
        if provider != "workos":
            assert result.allowed_origins == (APP_BASE_URL,)
        assert result.scopes == ("openid", "email", "profile")
        assert result.status


class TestSelfHostedLoginFlows:

    @pytest.mark.parametrize("framework", SELF_HOSTED_FRAMEWORKS)
    def test_self_hosted_framework_renders_authorize_and_callback_flow(self, framework):
        result = render_self_hosted_auth_scaffold(
            SelfHostedAuthScaffoldOptions(
                framework=framework,
                provider_setup=_setup_result(),
                app_base_url=APP_BASE_URL,
                oauth_client_import="../oauth-client",
            )
        )

        text = "\n".join(f.content for f in result.files)
        assert result.framework == framework
        assert result.provider == "auth0"
        assert "super-secret-value" not in text
        assert "DEFAULT_STATE_TTL_SECONDS" in text
        if framework == "nextauth":
            assert "auth/nextauth.config.ts" in [f.path for f in result.files]
            assert 'checks: ["pkce", "state"]' in text
            assert "pkceCodeVerifier" in text
        else:
            assert "app/api/auth/auth0/route.ts" in [f.path for f in result.files]
            assert "beginAuthorization" in text
            assert "verifyStateAndConsume" in text
            assert "codeVerifier" in text

    @pytest.mark.parametrize("framework", SELF_HOSTED_FRAMEWORKS)
    def test_self_hosted_framework_composes_provider_stack_and_mfa_baseline(
        self,
        framework,
    ):
        provider_stack = render_account_linking_stack(
            AccountLinkingStackOptions(
                framework=framework,
                provider_plans=tuple(
                    _vendor_plan(provider) for provider in ACCOUNT_LINKING_PROVIDERS
                ),
            )
        )
        email_mfa = render_email_mfa_baseline(EmailMfaBaselineOptions(framework))

        stack_text = "\n".join(f.content for f in provider_stack.files)
        mfa_text = "\n".join(f.content for f in email_mfa.files)
        assert [p.provider for p in provider_stack.providers] == list(
            ACCOUNT_LINKING_PROVIDERS
        )
        assert "requiresPasswordConfirmation" in stack_text
        assert "AUTH_LINK_PASSWORD_CONFIRMATION" in [
            item.name for item in provider_stack.env
        ]
        assert email_mfa.methods == (
            "email_password",
            "magic_link",
            "totp",
            "webauthn",
        )
        assert "/api/v1/auth/magic-link/request" in mfa_text
        assert "/api/v1/auth/mfa/totp/enroll" in mfa_text
        assert "/api/v1/auth/mfa/webauthn/challenge/complete" in mfa_text
