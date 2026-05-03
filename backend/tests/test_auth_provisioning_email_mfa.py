"""FS.2.5 -- Email + magic-link + MFA baseline scaffold tests."""

from __future__ import annotations

import pytest

from backend.auth_provisioning import (
    AuthScaffoldEnvVar,
    EmailMfaBaselineOptions,
    UnsupportedSelfHostedAuthFrameworkError,
    list_email_mfa_baseline_methods,
    render_email_mfa_baseline,
)


def _opts(framework: str, **overrides) -> EmailMfaBaselineOptions:
    kwargs = dict(framework=framework)
    kwargs.update(overrides)
    return EmailMfaBaselineOptions(**kwargs)


class TestEmailMfaBaselineRegistry:

    def test_list_email_mfa_baseline_methods(self):
        assert list_email_mfa_baseline_methods() == [
            "email_password",
            "magic_link",
            "totp",
            "webauthn",
        ]

    def test_rejects_unknown_framework(self):
        with pytest.raises(UnsupportedSelfHostedAuthFrameworkError):
            render_email_mfa_baseline(_opts("passport"))


class TestNextAuthEmailMfaBaseline:

    def test_renders_nextauth_manifest(self):
        result = render_email_mfa_baseline(_opts("next-auth"))
        assert result.framework == "nextauth"
        assert result.methods == (
            "email_password",
            "magic_link",
            "totp",
            "webauthn",
        )
        assert result.dependencies == ("next-auth",)
        assert [f.path for f in result.files] == [
            "auth/email-mfa-baseline.ts",
            "auth/nextauth.email-mfa.ts",
        ]

    def test_nextauth_declares_method_posture_without_secret_values(self):
        result = render_email_mfa_baseline(_opts("nextauth"))
        text = "\n".join(f.content for f in result.files)
        assert "emailMfaBaselineMethods" in text
        assert "AUTH_MAGIC_LINK_SECRET" not in text
        assert "AUTH_MAGIC_LINK_TTL_SECONDS" in text
        assert "AUTH_MFA_REQUIRED" in text
        assert "WEBAUTHN_RP_ID" in text

    def test_env_metadata_marks_magic_link_secret_sensitive(self):
        result = render_email_mfa_baseline(_opts("nextauth"))
        env = {item.name: item for item in result.env}
        assert env["AUTH_MAGIC_LINK_SECRET"].sensitive is True
        assert env["AUTH_EMAIL_FROM"].required is True
        assert env["WEBAUTHN_ORIGIN"].required is True


class TestLuciaEmailMfaBaseline:

    def test_renders_lucia_route_manifest(self):
        result = render_email_mfa_baseline(_opts("lucia"))
        assert result.framework == "lucia"
        assert result.dependencies == ("lucia",)
        assert [f.path for f in result.files] == [
            "auth/email-mfa-baseline.ts",
            "app/api/auth/magic-link/route.ts",
            "app/api/auth/magic-link/verify/route.ts",
            "app/api/auth/mfa/totp/route.ts",
            "app/api/auth/mfa/webauthn/route.ts",
        ]

    def test_baseline_client_pins_backend_auth_surfaces(self):
        result = render_email_mfa_baseline(_opts("lucia"))
        client = result.files[0].content
        assert "/api/v1/auth/login" in client
        assert "/api/v1/auth/magic-link/request" in client
        assert "/api/v1/auth/magic-link/confirm" in client
        assert "/api/v1/auth/mfa/totp/enroll" in client
        assert "/api/v1/auth/mfa/challenge" in client
        assert "/api/v1/auth/mfa/webauthn/register/begin" in client
        assert "/api/v1/auth/mfa/webauthn/challenge/complete" in client

    def test_lucia_routes_dispatch_magic_link_totp_and_webauthn_actions(self):
        result = render_email_mfa_baseline(_opts("lucia"))
        text = "\n".join(f.content for f in result.files)
        assert "requestMagicLink" in text
        assert "verifyMagicLink" in text
        assert 'body.action === "enroll"' in text
        assert 'body.action === "challenge"' in text
        assert 'body.action === "register_begin"' in text
        assert 'body.action === "challenge_complete"' in text

    def test_to_dict_is_json_ready_shape(self):
        data = render_email_mfa_baseline(
            _opts(
                "lucia",
                extra_env=(AuthScaffoldEnvVar("AUTH_EMAIL_REPLY_TO", False),),
            )
        ).to_dict()
        assert data["framework"] == "lucia"
        assert data["methods"][1] == "magic_link"
        assert data["files"][0]["path"] == "auth/email-mfa-baseline.ts"
        assert data["env"][-1]["name"] == "AUTH_EMAIL_REPLY_TO"


class TestEmailMfaBaselineValidation:

    @pytest.mark.parametrize("field", ["auth_dir", "route_prefix", "api_base_url_env"])
    def test_required_paths(self, field):
        kwargs = dict(
            framework="lucia",
            auth_dir="auth",
            route_prefix="app/api/auth",
            api_base_url_env="NEXT_PUBLIC_API_BASE_URL",
        )
        kwargs[field] = ""
        opts = EmailMfaBaselineOptions(**kwargs)
        with pytest.raises(ValueError, match=field):
            render_email_mfa_baseline(opts)
