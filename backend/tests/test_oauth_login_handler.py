"""AS.6.1 — backend.security.oauth_login_handler contract tests.

Validates the OmniSight self-login OAuth backend handler that wires
the AS.1 OAuth shared library to the eleven ``Sign in with Google /
GitHub / Microsoft / Apple / Discord / GitLab / Bitbucket / Slack /
Notion / Salesforce / HubSpot`` SSO buttons via two HTTP endpoints
(``GET /api/v1/auth/oauth/{vendor}/authorize`` and ``.../callback``)
mounted in :mod:`backend.routers.auth`.

Test families
─────────────
1. SUPPORTED_PROVIDERS / cookie-envelope constants — pinned values,
   immutable shapes, no module-level mutable container.
2. assert_provider_supported — accepts the eleven AS.6.1 / FX2.D9.7 slugs,
   rejects unknown / mixed-case slugs, raises the right exception
   class.
3. lookup_provider_credentials — reads the per-vendor Settings
   field pair, raises ProviderNotConfiguredError on missing.
4. compute_redirect_uri — produces the canonical
   ``{base}/api/v1/auth/oauth/{vendor}/callback`` URL,
   normalises trailing-slash quirks.
5. derive_signing_key + resolve_signing_key — primary-key
   precedence, fallback-seed derivation, both-empty raises.
6. encode_signed_flow / decode_signed_flow — round-trip,
   tamper detection (HMAC mismatch), TTL expiry, malformed
   envelope, version pin.
7. extract_user_identity — per-vendor field-name dispatch
   (Google/GitHub/Microsoft/Discord/GitLab/Bitbucket/Slack userinfo,
   Notion token response, HubSpot token metadata userinfo + Apple id_token), missing
   fields raise IdentityFieldMissingError, name fallback to
   email-local-part.
8. exchange_authorization_code — happy path with httpx
   MockTransport, RFC 6749 §5.2 error shape surfaces as
   TokenResponseError, network error wrapped, missing code /
   verifier rejected up front.
9. fetch_userinfo — happy path, vendor without userinfo
   endpoint raises (Apple), HTTP non-2xx raises, non-JSON body
   raises.
10. decode_id_token_claims_unverified — accepts a 3-part JWS,
    rejects malformed shapes.
11. begin_oauth_login — knob-off raises OAuthFeatureDisabled,
    end-to-end produces an authorize_url containing the right
    state + code_challenge + the cookie verifies + the FlowSession
    has the right TTL.
12. complete_oauth_login — full flow with mocked vendor (token +
    userinfo) for each provider, state-mismatch /
    expired / cookie-tamper paths, cookie-provider mismatch
    rejection.
13. Module-global state audit (per SOP §1) — no top-level mutable
    container, importing the module is side-effect-free.
14. Settings field declaration drift guard.
15. Google/GitHub/Microsoft/Apple/Discord/GitLab/Bitbucket/Slack/Notion/Salesforce/HubSpot router integration — mocked authorize → callback →
    OmniSight session cookie + DB session creation.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from backend.security import oauth_login_handler as olh
from backend.security import oauth_client as oc
from backend.security.oauth_client import (
    FlowSession,
    StateExpiredError,
    StateMismatchError,
    TokenResponseError,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _FakeSettings:
    """Minimal Settings stand-in for tests that never touch the real
    pydantic singleton — the singleton's env source is process-wide
    and would bleed across tests."""

    def __init__(self, **overrides):
        defaults = {
            "oauth_google_client_id": "g-id",
            "oauth_google_client_secret": "g-secret",
            "oauth_github_client_id": "gh-id",
            "oauth_github_client_secret": "gh-secret",
            "oauth_microsoft_client_id": "ms-id",
            "oauth_microsoft_client_secret": "ms-secret",
            "oauth_apple_client_id": "ap-id",
            "oauth_apple_client_secret": "ap-secret",
            "oauth_discord_client_id": "dc-id",
            "oauth_discord_client_secret": "dc-secret",
            "oauth_gitlab_client_id": "gl-id",
            "oauth_gitlab_client_secret": "gl-secret",
            "oauth_bitbucket_client_id": "bb-id",
            "oauth_bitbucket_client_secret": "bb-secret",
            "oauth_slack_client_id": "sl-id",
            "oauth_slack_client_secret": "sl-secret",
            "oauth_notion_client_id": "nt-id",
            "oauth_notion_client_secret": "nt-secret",
            "oauth_salesforce_client_id": "sf-id",
            "oauth_salesforce_client_secret": "sf-secret",
            "oauth_salesforce_login_base_url": "",
            "oauth_hubspot_client_id": "hs-id",
            "oauth_hubspot_client_secret": "hs-secret",
            "oauth_redirect_base_url": "https://omnisight.example.com",
            "oauth_flow_signing_key": "test-signing-key-with-enough-entropy-1234",
            "decision_bearer": "decision-bearer-fallback-key-x",
        }
        defaults.update(overrides)
        for k, v in defaults.items():
            setattr(self, k, v)


def _mock_transport(handler):
    """Wrap a sync handler(request) → httpx.Response into a
    MockTransport for use in an httpx.AsyncClient."""
    return httpx.MockTransport(handler)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_supported_providers_pinned():
    assert olh.SUPPORTED_PROVIDERS == frozenset({
        "google", "github", "microsoft", "apple", "discord", "gitlab",
        "bitbucket", "slack", "notion", "salesforce", "hubspot",
    })


def test_supported_providers_is_frozenset():
    """No module-level mutable container — SOP §1."""
    assert isinstance(olh.SUPPORTED_PROVIDERS, frozenset)


def test_flow_cookie_constants():
    assert olh.FLOW_COOKIE_NAME == "omnisight_oauth_flow"
    assert olh.FLOW_COOKIE_PATH == "/api/v1/auth/oauth"
    assert olh.FLOW_COOKIE_TTL_SECONDS == oc.DEFAULT_STATE_TTL_SECONDS == 600


def test_module_no_top_level_mutable_state():
    """SOP §1: scan module dict for mutable containers."""
    forbidden_types = (list, dict, set, bytearray)
    for name, value in vars(olh).items():
        if name.startswith("_") or callable(value):
            continue
        if isinstance(value, type):
            continue
        # ALL = list[str] is fine because it's __all__ (a marker).
        if name == "__all__":
            continue
        assert not isinstance(value, forbidden_types), (
            f"module-global {name!r} is mutable {type(value).__name__}; "
            f"SOP §1 forbids — make it a tuple / frozenset / MappingProxyType"
        )


def test_export_count_pinned():
    assert len(olh.__all__) == 35
    assert "begin_oauth_login" in olh.__all__
    assert "complete_oauth_login" in olh.__all__
    assert "encode_signed_flow" in olh.__all__
    assert "decode_signed_flow" in olh.__all__


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — assert_provider_supported
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize(
    "p",
    [
        "google", "github", "microsoft", "apple", "discord", "gitlab",
        "bitbucket", "slack", "notion", "salesforce", "hubspot",
    ],
)
def test_assert_provider_supported_accepts_supported(p):
    olh.assert_provider_supported(p)  # no raise


@pytest.mark.parametrize("p", ["Google", "GITHUB", "facebook", ""])
def test_assert_provider_supported_rejects_unknown(p):
    with pytest.raises(olh.ProviderNotSupportedError):
        olh.assert_provider_supported(p)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — lookup_provider_credentials
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_lookup_provider_credentials_returns_frozen():
    creds = olh.lookup_provider_credentials("google", settings_obj=_FakeSettings())
    assert creds.provider == "google"
    assert creds.client_id == "g-id"
    assert creds.client_secret == "g-secret"
    with pytest.raises(Exception):
        # frozen dataclass — assignment must raise
        creds.client_id = "spoof"  # type: ignore[misc]


def test_lookup_provider_credentials_discord():
    creds = olh.lookup_provider_credentials("discord", settings_obj=_FakeSettings())
    assert creds.provider == "discord"
    assert creds.client_id == "dc-id"
    assert creds.client_secret == "dc-secret"


def test_lookup_provider_credentials_gitlab():
    creds = olh.lookup_provider_credentials("gitlab", settings_obj=_FakeSettings())
    assert creds.provider == "gitlab"
    assert creds.client_id == "gl-id"
    assert creds.client_secret == "gl-secret"


def test_lookup_provider_credentials_bitbucket():
    creds = olh.lookup_provider_credentials("bitbucket", settings_obj=_FakeSettings())
    assert creds.provider == "bitbucket"
    assert creds.client_id == "bb-id"
    assert creds.client_secret == "bb-secret"


def test_lookup_provider_credentials_slack():
    creds = olh.lookup_provider_credentials("slack", settings_obj=_FakeSettings())
    assert creds.provider == "slack"
    assert creds.client_id == "sl-id"
    assert creds.client_secret == "sl-secret"


def test_lookup_provider_credentials_notion():
    creds = olh.lookup_provider_credentials("notion", settings_obj=_FakeSettings())
    assert creds.provider == "notion"
    assert creds.client_id == "nt-id"
    assert creds.client_secret == "nt-secret"


def test_lookup_provider_credentials_salesforce():
    creds = olh.lookup_provider_credentials("salesforce", settings_obj=_FakeSettings())
    assert creds.provider == "salesforce"
    assert creds.client_id == "sf-id"
    assert creds.client_secret == "sf-secret"


def test_lookup_provider_credentials_hubspot():
    creds = olh.lookup_provider_credentials("hubspot", settings_obj=_FakeSettings())
    assert creds.provider == "hubspot"
    assert creds.client_id == "hs-id"
    assert creds.client_secret == "hs-secret"


def test_lookup_provider_credentials_raises_on_missing():
    s = _FakeSettings(oauth_google_client_id="", oauth_google_client_secret="")
    with pytest.raises(olh.ProviderNotConfiguredError) as excinfo:
        olh.lookup_provider_credentials("google", settings_obj=s)
    msg = str(excinfo.value)
    assert "OMNISIGHT_OAUTH_GOOGLE_CLIENT_ID" in msg
    assert "OMNISIGHT_OAUTH_GOOGLE_CLIENT_SECRET" in msg


def test_lookup_provider_credentials_raises_on_partial_missing():
    s = _FakeSettings(oauth_github_client_secret="")
    with pytest.raises(olh.ProviderNotConfiguredError) as excinfo:
        olh.lookup_provider_credentials("github", settings_obj=s)
    msg = str(excinfo.value)
    assert "OMNISIGHT_OAUTH_GITHUB_CLIENT_SECRET" in msg
    assert "OMNISIGHT_OAUTH_GITHUB_CLIENT_ID" not in msg


def test_lookup_provider_credentials_rejects_unsupported():
    with pytest.raises(olh.ProviderNotSupportedError):
        olh.lookup_provider_credentials("facebook", settings_obj=_FakeSettings())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 4 — compute_redirect_uri
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_compute_redirect_uri_canonical():
    uri = olh.compute_redirect_uri("google", base_url="https://omnisight.example.com")
    assert uri == "https://omnisight.example.com/api/v1/auth/oauth/google/callback"


def test_compute_redirect_uri_strips_trailing_slash():
    uri = olh.compute_redirect_uri("github", base_url="https://x.example.com/")
    assert uri == "https://x.example.com/api/v1/auth/oauth/github/callback"


def test_compute_redirect_uri_normalises_prefix_with_no_leading_slash():
    uri = olh.compute_redirect_uri("apple", base_url="https://x.com", api_prefix="api/v2")
    assert uri == "https://x.com/api/v2/auth/oauth/apple/callback"


def test_compute_redirect_uri_requires_base_url():
    with pytest.raises(ValueError):
        olh.compute_redirect_uri("google", base_url="")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 5 — signing-key derivation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_derive_signing_key_uses_primary_when_set():
    k1 = olh.derive_signing_key(raw_key="primary-key-with-entropy-x", fallback_seed="fb")
    k2 = olh.derive_signing_key(raw_key="primary-key-with-entropy-x", fallback_seed="other")
    assert k1 == k2  # fallback ignored when primary is set
    assert len(k1) == 32  # SHA-256 output


def test_derive_signing_key_uses_fallback_when_primary_empty():
    k = olh.derive_signing_key(raw_key="", fallback_seed="decision-bearer-12345678")
    assert len(k) == 32
    # Domain-separation label must change the digest:
    raw = __import__("hashlib").sha256(b"decision-bearer-12345678").digest()
    assert k != raw


def test_derive_signing_key_rejects_short_primary():
    with pytest.raises(olh.SigningKeyUnavailableError):
        olh.derive_signing_key(raw_key="too-short", fallback_seed="fb")


def test_derive_signing_key_raises_when_both_empty():
    with pytest.raises(olh.SigningKeyUnavailableError):
        olh.derive_signing_key(raw_key="", fallback_seed="")


def test_resolve_signing_key_reads_settings():
    s = _FakeSettings(oauth_flow_signing_key="primary-key-with-good-entropy")
    k = olh.resolve_signing_key(settings_obj=s)
    assert len(k) == 32


def test_resolve_signing_key_falls_back_to_decision_bearer():
    s = _FakeSettings(oauth_flow_signing_key="", decision_bearer="bearer-with-some-entropy")
    k = olh.resolve_signing_key(settings_obj=s)
    assert len(k) == 32


def test_resolve_signing_key_raises_when_both_empty():
    s = _FakeSettings(oauth_flow_signing_key="", decision_bearer="")
    with pytest.raises(olh.SigningKeyUnavailableError):
        olh.resolve_signing_key(settings_obj=s)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 6 — cookie sign/verify
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_flow(ttl_s: int = 600, now: float | None = None) -> FlowSession:
    ts = time.time() if now is None else now
    return FlowSession(
        provider="google",
        state="s-abc",
        code_verifier="v" * 50,
        nonce="n-xyz",
        redirect_uri="https://omnisight.example.com/api/v1/auth/oauth/google/callback",
        scope=("openid", "email", "profile"),
        created_at=ts,
        expires_at=ts + ttl_s,
        extra=(("ret_to", "/dashboard"),),
    )


def test_serialize_round_trip():
    flow = _make_flow()
    data = olh.serialize_flow(flow)
    assert data["v"] == 1
    again = olh.deserialize_flow(data)
    assert again == flow


def test_deserialize_rejects_unknown_version():
    flow = _make_flow()
    data = olh.serialize_flow(flow)
    data["v"] = 999
    with pytest.raises(olh.FlowCookieInvalidError):
        olh.deserialize_flow(data)


def test_deserialize_rejects_non_mapping():
    with pytest.raises(olh.FlowCookieInvalidError):
        olh.deserialize_flow("not a dict")  # type: ignore[arg-type]


def test_cookie_round_trip_succeeds():
    key = b"k" * 32
    flow = _make_flow()
    cookie = olh.encode_signed_flow(flow, key=key)
    again = olh.decode_signed_flow(cookie, key=key)
    assert again == flow


def test_cookie_signature_mismatch_rejected():
    key = b"k" * 32
    flow = _make_flow()
    cookie = olh.encode_signed_flow(flow, key=key)
    # Flip the last char of the signature
    body, sig = cookie.rsplit(".", 1)
    bad = body + "." + ("A" if sig[-1] != "A" else "B") + sig[1:]
    with pytest.raises(olh.FlowCookieInvalidError):
        olh.decode_signed_flow(bad, key=key)


def test_cookie_wrong_key_rejected():
    flow = _make_flow()
    cookie = olh.encode_signed_flow(flow, key=b"k" * 32)
    with pytest.raises(olh.FlowCookieInvalidError):
        olh.decode_signed_flow(cookie, key=b"x" * 32)


def test_cookie_expired_raises_state_expired():
    """Distinct exception class from FlowCookieInvalidError so the
    route handler can give a different user-facing message."""
    key = b"k" * 32
    now0 = 1000.0
    flow = _make_flow(ttl_s=600, now=now0)
    cookie = olh.encode_signed_flow(flow, key=key)
    with pytest.raises(StateExpiredError):
        olh.decode_signed_flow(cookie, key=key, now=now0 + 601)


def test_cookie_missing_raises_missing_error():
    with pytest.raises(olh.FlowCookieMissingError):
        olh.decode_signed_flow("", key=b"k" * 32)


def test_cookie_malformed_shape_rejected():
    with pytest.raises(olh.FlowCookieInvalidError):
        olh.decode_signed_flow("no-dot-in-cookie", key=b"k" * 32)
    with pytest.raises(olh.FlowCookieInvalidError):
        olh.decode_signed_flow("a.b.c", key=b"k" * 32)


def test_cookie_body_not_base64_rejected():
    with pytest.raises(olh.FlowCookieInvalidError):
        olh.decode_signed_flow("not-base64!.also-not", key=b"k" * 32)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 7 — extract_user_identity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_extract_identity_google():
    ident = olh.extract_user_identity(
        provider="google",
        userinfo={"sub": "g-123", "email": "Foo@Bar.COM", "name": "Foo Bar"},
        id_token_claims=None,
    )
    assert ident == olh.OAuthUserIdentity(
        provider="google", subject="g-123", email="foo@bar.com", name="Foo Bar",
    )


def test_extract_identity_google_falls_back_to_given_name():
    ident = olh.extract_user_identity(
        provider="google",
        userinfo={"sub": "g", "email": "a@b.com", "given_name": "Alice"},
        id_token_claims=None,
    )
    assert ident.name == "Alice"


def test_extract_identity_github_numeric_id_coerced_to_str():
    ident = olh.extract_user_identity(
        provider="github",
        userinfo={"id": 12345, "email": "u@gh.io", "login": "octo"},
        id_token_claims=None,
    )
    assert ident.subject == "12345"
    assert ident.name == "octo"  # falls back to login when name missing


def test_extract_identity_microsoft_uses_preferred_username_for_email():
    ident = olh.extract_user_identity(
        provider="microsoft",
        userinfo={"sub": "m-1", "preferred_username": "alice@msft.com", "name": "Alice"},
        id_token_claims=None,
    )
    assert ident.email == "alice@msft.com"


def test_extract_identity_discord_uses_snowflake_id_and_global_name():
    ident = olh.extract_user_identity(
        provider="discord",
        userinfo={
            "id": "80351110224678912",
            "email": "User.Discord@Example.COM",
            "global_name": "Discord User",
            "username": "discorduser",
        },
        id_token_claims=None,
    )
    assert ident == olh.OAuthUserIdentity(
        provider="discord",
        subject="80351110224678912",
        email="user.discord@example.com",
        name="Discord User",
    )


def test_extract_identity_discord_falls_back_to_username():
    ident = olh.extract_user_identity(
        provider="discord",
        userinfo={
            "id": "80351110224678912",
            "email": "user@example.com",
            "username": "discorduser",
        },
        id_token_claims=None,
    )
    assert ident.name == "discorduser"


def test_extract_identity_gitlab_uses_oidc_sub_and_name():
    ident = olh.extract_user_identity(
        provider="gitlab",
        userinfo={
            "sub": "gitlab|42",
            "email": "User.GitLab@Example.COM",
            "name": "GitLab User",
            "nickname": "gitlabuser",
        },
        id_token_claims=None,
    )
    assert ident == olh.OAuthUserIdentity(
        provider="gitlab",
        subject="gitlab|42",
        email="user.gitlab@example.com",
        name="GitLab User",
    )


def test_extract_identity_gitlab_falls_back_to_nickname():
    ident = olh.extract_user_identity(
        provider="gitlab",
        userinfo={
            "sub": "gitlab|43",
            "email": "user@example.com",
            "nickname": "gitlabuser",
        },
        id_token_claims=None,
    )
    assert ident.name == "gitlabuser"


def test_extract_identity_bitbucket_uses_uuid_and_display_name():
    ident = olh.extract_user_identity(
        provider="bitbucket",
        userinfo={
            "uuid": "{0f4c9a2e-6d42-4d1f-a111-5a0f1f99c123}",
            "email": "User.Bitbucket@Example.COM",
            "display_name": "Bitbucket User",
            "nickname": "bitbucketuser",
        },
        id_token_claims=None,
    )
    assert ident == olh.OAuthUserIdentity(
        provider="bitbucket",
        subject="{0f4c9a2e-6d42-4d1f-a111-5a0f1f99c123}",
        email="user.bitbucket@example.com",
        name="Bitbucket User",
    )


def test_extract_identity_bitbucket_falls_back_to_nickname():
    ident = olh.extract_user_identity(
        provider="bitbucket",
        userinfo={
            "uuid": "{0f4c9a2e-6d42-4d1f-a111-5a0f1f99c124}",
            "email": "user@example.com",
            "nickname": "bitbucketuser",
        },
        id_token_claims=None,
    )
    assert ident.name == "bitbucketuser"


def test_extract_identity_slack_uses_root_oidc_claims():
    ident = olh.extract_user_identity(
        provider="slack",
        userinfo={
            "sub": "U1234567890",
            "email": "User.Slack@Example.COM",
            "name": "Slack User",
        },
        id_token_claims=None,
    )
    assert ident == olh.OAuthUserIdentity(
        provider="slack",
        subject="U1234567890",
        email="user.slack@example.com",
        name="Slack User",
    )


def test_extract_identity_slack_falls_back_to_given_name():
    ident = olh.extract_user_identity(
        provider="slack",
        userinfo={
            "sub": "U1234567891",
            "email": "user@example.com",
            "given_name": "SlackGiven",
        },
        id_token_claims=None,
    )
    assert ident.name == "SlackGiven"


def test_extract_identity_notion_uses_token_response_owner_user():
    ident = olh.extract_user_identity(
        provider="notion",
        userinfo={
            "access_token": "nt-at-1",
            "workspace_name": "OmniSight",
            "owner": {
                "type": "user",
                "user": {
                    "id": "notion-user-123",
                    "name": "Notion User",
                    "person": {"email": "User.Notion@Example.COM"},
                },
            },
        },
        id_token_claims=None,
    )
    assert ident == olh.OAuthUserIdentity(
        provider="notion",
        subject="notion-user-123",
        email="user.notion@example.com",
        name="Notion User",
    )


def test_extract_identity_notion_falls_back_to_workspace_name():
    ident = olh.extract_user_identity(
        provider="notion",
        userinfo={
            "workspace_name": "Workspace Name",
            "owner": {
                "type": "user",
                "user": {
                    "id": "notion-user-124",
                    "person": {"email": "user@example.com"},
                },
            },
        },
        id_token_claims=None,
    )
    assert ident.name == "Workspace Name"


def test_extract_identity_salesforce_uses_user_id_subject():
    ident = olh.extract_user_identity(
        provider="salesforce",
        userinfo={
            "user_id": "005xx000001Sv6hAAC",
            "sub": "https://login.salesforce.com/id/00Dxx0000001gPFEAY/005xx000001Sv6hAAC",
            "email": "User.Salesforce@Example.COM",
            "name": "User Salesforce",
            "preferred_username": "user.salesforce@example.com",
        },
        id_token_claims=None,
    )
    assert ident == olh.OAuthUserIdentity(
        provider="salesforce",
        subject="005xx000001Sv6hAAC",
        email="user.salesforce@example.com",
        name="User Salesforce",
    )


def test_extract_identity_salesforce_falls_back_to_preferred_username():
    ident = olh.extract_user_identity(
        provider="salesforce",
        userinfo={
            "user_id": "005xx000001Sv6hAAD",
            "email": "user.salesforce@example.com",
            "preferred_username": "Salesforce Username",
        },
        id_token_claims=None,
    )
    assert ident.name == "Salesforce Username"


def test_extract_identity_hubspot_uses_user_id_and_user_email():
    ident = olh.extract_user_identity(
        provider="hubspot",
        userinfo={
            "user_id": 123456,
            "user": "User.HubSpot@Example.COM",
            "hub_domain": "example.hubspot.com",
            "hub_id": 98765,
        },
        id_token_claims=None,
    )
    assert ident == olh.OAuthUserIdentity(
        provider="hubspot",
        subject="123456",
        email="user.hubspot@example.com",
        name="User.HubSpot@Example.COM",
    )


def test_extract_identity_hubspot_accepts_email_fallback():
    ident = olh.extract_user_identity(
        provider="hubspot",
        userinfo={
            "user_id": "123457",
            "email": "hubspot.user@example.com",
            "hub_domain": "fallback.hubspot.com",
        },
        id_token_claims=None,
    )
    assert ident.email == "hubspot.user@example.com"
    assert ident.name == "fallback.hubspot.com"


def test_extract_identity_apple_reads_id_token_claims():
    ident = olh.extract_user_identity(
        provider="apple",
        userinfo=None,
        id_token_claims={"sub": "apple-sub", "email": "user@privaterelay.appleid.com"},
    )
    assert ident.subject == "apple-sub"
    assert ident.email == "user@privaterelay.appleid.com"
    # Name falls back to email-local-part since Apple doesn't ship name in id_token
    assert ident.name == "user"


def test_extract_identity_missing_subject_raises():
    with pytest.raises(olh.IdentityFieldMissingError):
        olh.extract_user_identity(
            provider="google",
            userinfo={"email": "a@b.com"},
            id_token_claims=None,
        )


def test_extract_identity_missing_email_raises():
    with pytest.raises(olh.IdentityFieldMissingError):
        olh.extract_user_identity(
            provider="github",
            userinfo={"id": 1, "login": "u"},
            id_token_claims=None,
        )


def test_extract_identity_apple_missing_claims_raises():
    with pytest.raises(olh.IdentityFieldMissingError):
        olh.extract_user_identity(
            provider="apple", userinfo=None, id_token_claims=None,
        )


def test_extract_identity_name_falls_back_to_email_local():
    ident = olh.extract_user_identity(
        provider="google",
        userinfo={"sub": "g", "email": "alice@example.com"},  # no name field
        id_token_claims=None,
    )
    assert ident.name == "alice"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 8 — exchange_authorization_code
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_exchange_code_happy_path():
    from backend.security.oauth_vendors import GOOGLE
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid email profile",
                "id_token": "header.payload.sig",
            },
        )

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    token = _run(olh.exchange_authorization_code(
        vendor=GOOGLE,
        code="auth-code-xyz",
        code_verifier="v" * 50,
        client_id="g-id",
        client_secret="g-secret",
        redirect_uri="https://x/cb",
        http_client=client,
        now=1000.0,
    ))
    _run(client.aclose())

    assert token.access_token == "at-1"
    assert token.refresh_token == "rt-1"
    assert token.expires_at == 1000.0 + 3600
    assert "openid" in token.scope
    assert "code=auth-code-xyz" in captured["body"]
    assert "code_verifier=" in captured["body"]
    assert captured["url"] == GOOGLE.token_endpoint


def test_exchange_code_rfc_error_shape_raises():
    from backend.security.oauth_vendors import GITHUB

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "bad code"},
        )

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    with pytest.raises(TokenResponseError) as excinfo:
        _run(olh.exchange_authorization_code(
            vendor=GITHUB,
            code="bad",
            code_verifier="v" * 50,
            client_id="gh-id",
            client_secret="gh-secret",
            redirect_uri="https://x/cb",
            http_client=client,
        ))
    _run(client.aclose())
    assert "invalid_grant" in str(excinfo.value)


def test_exchange_code_rejects_missing_code_upfront():
    from backend.security.oauth_vendors import GOOGLE
    with pytest.raises(TokenResponseError):
        _run(olh.exchange_authorization_code(
            vendor=GOOGLE, code="", code_verifier="v" * 50,
            client_id="g", client_secret="s", redirect_uri="https://x/cb",
        ))


def test_exchange_code_rejects_missing_verifier_upfront():
    from backend.security.oauth_vendors import GOOGLE
    with pytest.raises(TokenResponseError):
        _run(olh.exchange_authorization_code(
            vendor=GOOGLE, code="c", code_verifier="",
            client_id="g", client_secret="s", redirect_uri="https://x/cb",
        ))


def test_exchange_code_non_json_body_raises():
    from backend.security.oauth_vendors import GOOGLE

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>error</html>")

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    with pytest.raises(TokenResponseError):
        _run(olh.exchange_authorization_code(
            vendor=GOOGLE, code="c", code_verifier="v" * 50,
            client_id="g", client_secret="s", redirect_uri="https://x/cb",
            http_client=client,
        ))
    _run(client.aclose())


def test_exchange_code_notion_uses_basic_auth_without_client_secret_body():
    from backend.security.oauth_vendors import NOTION
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "access_token": "nt-at-1",
                "token_type": "bearer",
                "workspace_name": "OmniSight",
                "owner": {
                    "type": "user",
                    "user": {
                        "id": "notion-user-123",
                        "name": "Notion User",
                        "person": {"email": "notion@example.com"},
                    },
                },
            },
        )

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    token = _run(olh.exchange_authorization_code(
        vendor=NOTION,
        code="notion-code",
        code_verifier="v" * 50,
        client_id="nt-id",
        client_secret="nt-secret",
        redirect_uri="https://x/cb",
        http_client=client,
    ))
    _run(client.aclose())

    assert token.access_token == "nt-at-1"
    assert captured["auth"] == "Basic bnQtaWQ6bnQtc2VjcmV0"
    assert "client_id=" not in captured["body"]
    assert "client_secret=" not in captured["body"]
    assert "code_verifier=" not in captured["body"]
    assert "code=notion-code" in captured["body"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 9 — fetch_userinfo
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_fetch_userinfo_happy_path():
    from backend.security.oauth_vendors import GOOGLE
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"sub": "g-1", "email": "u@g.com", "name": "U"})

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    info = _run(olh.fetch_userinfo(vendor=GOOGLE, access_token="at-1", http_client=client))
    _run(client.aclose())
    assert info["sub"] == "g-1"
    assert captured["auth"] == "Bearer at-1"


def test_fetch_userinfo_bitbucket_merges_primary_email():
    from backend.security.oauth_vendors import BITBUCKET
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["authorization"] == "Bearer bb-at-1"
        if str(request.url) == "https://api.bitbucket.org/2.0/user":
            return httpx.Response(200, json={
                "uuid": "{0f4c9a2e-6d42-4d1f-a111-5a0f1f99c123}",
                "display_name": "Bitbucket User",
            })
        return httpx.Response(200, json={
            "values": [
                {
                    "email": "secondary.bitbucket@example.com",
                    "is_primary": False,
                    "is_confirmed": True,
                },
                {
                    "email": "primary.bitbucket@example.com",
                    "is_primary": True,
                    "is_confirmed": True,
                },
            ],
        })

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    info = _run(olh.fetch_userinfo(
        vendor=BITBUCKET,
        access_token="bb-at-1",
        http_client=client,
    ))
    _run(client.aclose())

    assert seen == [
        "https://api.bitbucket.org/2.0/user",
        "https://api.bitbucket.org/2.0/user/emails",
    ]
    assert info["uuid"] == "{0f4c9a2e-6d42-4d1f-a111-5a0f1f99c123}"
    assert info["email"] == "primary.bitbucket@example.com"


def test_fetch_userinfo_bitbucket_missing_primary_email_raises():
    from backend.security.oauth_vendors import BITBUCKET

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://api.bitbucket.org/2.0/user":
            return httpx.Response(200, json={"uuid": "{bb}"})
        return httpx.Response(200, json={"values": []})

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    with pytest.raises(olh.UserinfoFetchError):
        _run(olh.fetch_userinfo(
            vendor=BITBUCKET,
            access_token="bb-at-1",
            http_client=client,
        ))
    _run(client.aclose())


def test_fetch_userinfo_hubspot_uses_bearer_header_not_query_param():
    from backend.security.oauth_vendors import HUBSPOT
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={
            "user_id": 123456,
            "user": "hubspot.user@example.com",
            "hub_id": 98765,
            "hub_domain": "example.hubspot.com",
        })

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    info = _run(olh.fetch_userinfo(
        vendor=HUBSPOT,
        access_token="hs-at-1",
        http_client=client,
    ))
    _run(client.aclose())

    assert captured["url"] == "https://api.hubapi.com/integrations/v1/me"
    assert "access_token" not in captured["url"]
    assert captured["auth"] == "Bearer hs-at-1"
    assert info["user_id"] == 123456
    assert info["user"] == "hubspot.user@example.com"


def test_fetch_userinfo_apple_no_endpoint_raises():
    from backend.security.oauth_vendors import APPLE
    with pytest.raises(olh.UserinfoFetchError):
        _run(olh.fetch_userinfo(vendor=APPLE, access_token="at"))


def test_fetch_userinfo_non_2xx_raises():
    from backend.security.oauth_vendors import GOOGLE

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_token"})

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    with pytest.raises(olh.UserinfoFetchError):
        _run(olh.fetch_userinfo(vendor=GOOGLE, access_token="bad", http_client=client))
    _run(client.aclose())


def test_fetch_userinfo_non_json_raises():
    from backend.security.oauth_vendors import GOOGLE

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html/>")

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    with pytest.raises(olh.UserinfoFetchError):
        _run(olh.fetch_userinfo(vendor=GOOGLE, access_token="x", http_client=client))
    _run(client.aclose())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 10 — decode_id_token_claims_unverified
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_jws(payload: dict[str, Any]) -> str:
    """Compose a fake unsigned JWS for tests — header.payload.sig with
    sig as a placeholder."""
    import base64
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")
    ).rstrip(b"=").decode()
    return f"{header}.{body}.sig-placeholder"


def test_decode_id_token_round_trip():
    jws = _make_jws({"sub": "apple-1", "email": "u@p.com"})
    claims = olh.decode_id_token_claims_unverified(jws)
    assert claims["sub"] == "apple-1"
    assert claims["email"] == "u@p.com"


def test_decode_id_token_rejects_empty():
    with pytest.raises(olh.IdTokenDecodeError):
        olh.decode_id_token_claims_unverified("")


def test_decode_id_token_rejects_non_three_part():
    with pytest.raises(olh.IdTokenDecodeError):
        olh.decode_id_token_claims_unverified("only.two")
    with pytest.raises(olh.IdTokenDecodeError):
        olh.decode_id_token_claims_unverified("a.b.c.d")


def test_decode_id_token_rejects_invalid_payload():
    bad = "h." + "@@@bad-base64@@@" + ".s"
    with pytest.raises(olh.IdTokenDecodeError):
        olh.decode_id_token_claims_unverified(bad)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 11 — begin_oauth_login
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_begin_oauth_login_knob_off_raises(monkeypatch):
    monkeypatch.setattr(olh.oauth_client, "is_enabled", lambda: False)
    with pytest.raises(olh.OAuthFeatureDisabled):
        olh.begin_oauth_login(
            provider="google",
            base_url="https://x.com",
            settings_obj=_FakeSettings(),
        )


def test_begin_oauth_login_unsupported_provider():
    with pytest.raises(olh.ProviderNotSupportedError):
        olh.begin_oauth_login(
            provider="facebook",
            base_url="https://x.com",
            settings_obj=_FakeSettings(),
        )


def test_begin_oauth_login_unconfigured_raises():
    s = _FakeSettings(oauth_microsoft_client_id="", oauth_microsoft_client_secret="")
    with pytest.raises(olh.ProviderNotConfiguredError):
        olh.begin_oauth_login(
            provider="microsoft", base_url="https://x.com", settings_obj=s,
        )


def test_begin_oauth_login_signing_key_unavailable():
    s = _FakeSettings(oauth_flow_signing_key="", decision_bearer="")
    with pytest.raises(olh.SigningKeyUnavailableError):
        olh.begin_oauth_login(
            provider="google", base_url="https://x.com", settings_obj=s,
        )


def test_begin_oauth_login_returns_authorize_url_and_cookie():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="google", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    assert isinstance(start, olh.AuthorizationStart)
    assert start.authorize_url.startswith("https://accounts.google.com/")
    assert "client_id=g-id" in start.authorize_url
    assert "redirect_uri=" in start.authorize_url
    assert f"state={start.flow.state}" in start.authorize_url
    # Cookie must round-trip with the same key
    key = olh.resolve_signing_key(settings_obj=s)
    decoded = olh.decode_signed_flow(start.flow_cookie, key=key)
    assert decoded.provider == "google"
    assert decoded.code_verifier == start.flow.code_verifier
    assert decoded.nonce is not None  # OIDC vendor → nonce present


def test_begin_oauth_login_github_no_nonce():
    """GitHub is non-OIDC — no nonce should be generated."""
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="github", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    assert start.flow.nonce is None


def test_begin_oauth_login_discord_authorize_url():
    """Discord is standard OAuth2 with identify+email scopes."""
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="discord", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    parsed = urlparse(start.authorize_url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "discord.com"
    assert parsed.path == "/oauth2/authorize"
    assert query["client_id"] == ["dc-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/discord/callback"
    ]
    assert query["scope"] == ["identify email"]
    assert start.flow.provider == "discord"
    assert start.flow.nonce is None


def test_begin_oauth_login_gitlab_authorize_url():
    """GitLab is OIDC-flavoured OAuth2 with read_user+profile scopes."""
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="gitlab", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    parsed = urlparse(start.authorize_url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "gitlab.com"
    assert parsed.path == "/oauth/authorize"
    assert query["client_id"] == ["gl-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/gitlab/callback"
    ]
    assert query["scope"] == ["read_user openid email profile"]
    assert start.flow.provider == "gitlab"
    assert start.flow.nonce is not None


def test_begin_oauth_login_bitbucket_authorize_url():
    """Bitbucket is standard OAuth2 with account+email scopes."""
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="bitbucket", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    parsed = urlparse(start.authorize_url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "bitbucket.org"
    assert parsed.path == "/site/oauth2/authorize"
    assert query["client_id"] == ["bb-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/bitbucket/callback"
    ]
    assert query["scope"] == ["account email"]
    assert start.flow.provider == "bitbucket"
    assert start.flow.nonce is None


def test_begin_oauth_login_slack_authorize_url():
    """Slack sign-in uses OIDC scopes and Slack's OpenID authorize path."""
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="slack", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    parsed = urlparse(start.authorize_url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "slack.com"
    assert parsed.path == "/openid/connect/authorize"
    assert query["client_id"] == ["sl-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/slack/callback"
    ]
    assert query["scope"] == ["openid email profile"]
    assert start.flow.provider == "slack"
    assert start.flow.nonce is not None


def test_begin_oauth_login_notion_authorize_url_omits_scope():
    """Notion permissions are fixed by the integration; no scope param."""
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="notion", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    parsed = urlparse(start.authorize_url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "api.notion.com"
    assert parsed.path == "/v1/oauth/authorize"
    assert query["client_id"] == ["nt-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/notion/callback"
    ]
    assert "scope" not in query
    assert query["owner"] == ["user"]
    assert start.flow.provider == "notion"
    assert start.flow.scope == ()
    assert start.flow.nonce is None


def test_begin_oauth_login_salesforce_authorize_url():
    """Salesforce defaults to production login.salesforce.com."""
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="salesforce", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    parsed = urlparse(start.authorize_url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "login.salesforce.com"
    assert parsed.path == "/services/oauth2/authorize"
    assert query["client_id"] == ["sf-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/salesforce/callback"
    ]
    assert query["scope"] == ["id email profile openid"]
    assert start.flow.provider == "salesforce"
    assert start.flow.nonce is not None


def test_begin_oauth_login_salesforce_sandbox_authorize_url():
    """Sandbox/community split is driven by login base URL setting."""
    s = _FakeSettings(oauth_salesforce_login_base_url="https://test.salesforce.com")
    start = olh.begin_oauth_login(
        provider="salesforce", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    parsed = urlparse(start.authorize_url)
    assert parsed.netloc == "test.salesforce.com"
    assert parsed.path == "/services/oauth2/authorize"


def test_begin_oauth_login_salesforce_community_authorize_url():
    s = _FakeSettings(
        oauth_salesforce_login_base_url="https://acme.my.site.com/"
    )
    start = olh.begin_oauth_login(
        provider="salesforce", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    parsed = urlparse(start.authorize_url)
    assert parsed.netloc == "acme.my.site.com"
    assert parsed.path == "/services/oauth2/authorize"


def test_begin_oauth_login_salesforce_rejects_invalid_login_base_url():
    s = _FakeSettings(oauth_salesforce_login_base_url="http://test.salesforce.com")
    with pytest.raises(olh.ProviderNotConfiguredError):
        olh.begin_oauth_login(
            provider="salesforce", base_url="https://omnisight.example.com",
            settings_obj=s,
        )


def test_begin_oauth_login_hubspot_authorize_url():
    """FX2.D9.7.11 pins HubSpot to oauth + CRM contacts read scopes."""
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="hubspot", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    parsed = urlparse(start.authorize_url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "app.hubspot.com"
    assert parsed.path == "/oauth/authorize"
    assert query["client_id"] == ["hs-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/hubspot/callback"
    ]
    assert query["scope"] == ["oauth crm.objects.contacts.read"]
    assert start.flow.provider == "hubspot"
    assert start.flow.nonce is None


def test_begin_oauth_login_apple_includes_form_post_param():
    """Apple's catalog entry pre-bakes ``response_mode=form_post`` —
    must appear in the authorize URL."""
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="apple", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    assert "response_mode=form_post" in start.authorize_url


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 12 — complete_oauth_login (full flow)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _good_token_handler(scope: str = "openid email profile",
                         id_token: str | None = None):
    payload = {
        "access_token": "at-good",
        "refresh_token": "rt-good",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": scope,
    }
    if id_token:
        payload["id_token"] = id_token

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    return handler


def _composite_handler(token_handler, userinfo_handler):
    """Route requests to the right per-vendor handler based on the
    URL path tail (`token` vs `userinfo`)."""
    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "userinfo" in u.lower() or "/user" in u:
            return userinfo_handler(request)
        return token_handler(request)
    return handler


def test_complete_oauth_login_google_full_flow():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="google", base_url="https://omnisight.example.com",
        settings_obj=s,
    )

    def userinfo_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "sub": "google-sub-1", "email": "alice@example.com", "name": "Alice",
        })

    client = httpx.AsyncClient(transport=_mock_transport(
        _composite_handler(_good_token_handler(), userinfo_handler)
    ))
    result = _run(olh.complete_oauth_login(
        provider="google",
        flow_cookie=start.flow_cookie,
        returned_state=start.flow.state,
        code="auth-code",
        settings_obj=s,
        http_client=client,
    ))
    _run(client.aclose())

    assert result.identity.provider == "google"
    assert result.identity.subject == "google-sub-1"
    assert result.identity.email == "alice@example.com"
    assert result.identity.name == "Alice"
    assert result.token.access_token == "at-good"
    assert result.flow.provider == "google"


def test_complete_oauth_login_discord_full_flow():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="discord", base_url="https://omnisight.example.com",
        settings_obj=s,
    )

    def userinfo_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://discord.com/api/users/@me"
        assert request.headers["authorization"] == "Bearer at-good"
        return httpx.Response(200, json={
            "id": "80351110224678912",
            "email": "dana.discord@example.com",
            "global_name": "Dana Discord",
        })

    client = httpx.AsyncClient(transport=_mock_transport(
        _composite_handler(_good_token_handler(scope="identify email"),
                           userinfo_handler)
    ))
    result = _run(olh.complete_oauth_login(
        provider="discord",
        flow_cookie=start.flow_cookie,
        returned_state=start.flow.state,
        code="auth-code",
        settings_obj=s,
        http_client=client,
    ))
    _run(client.aclose())

    assert result.identity.provider == "discord"
    assert result.identity.subject == "80351110224678912"
    assert result.identity.email == "dana.discord@example.com"
    assert result.identity.name == "Dana Discord"
    assert result.token.access_token == "at-good"
    assert result.flow.provider == "discord"


def test_complete_oauth_login_gitlab_full_flow():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="gitlab", base_url="https://omnisight.example.com",
        settings_obj=s,
    )

    def userinfo_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://gitlab.com/oauth/userinfo"
        assert request.headers["authorization"] == "Bearer at-good"
        return httpx.Response(200, json={
            "sub": "gitlab|777",
            "email": "dana.gitlab@example.com",
            "name": "Dana GitLab",
        })

    client = httpx.AsyncClient(transport=_mock_transport(
        _composite_handler(
            _good_token_handler(scope="read_user openid email profile"),
            userinfo_handler,
        )
    ))
    result = _run(olh.complete_oauth_login(
        provider="gitlab",
        flow_cookie=start.flow_cookie,
        returned_state=start.flow.state,
        code="auth-code",
        settings_obj=s,
        http_client=client,
    ))
    _run(client.aclose())

    assert result.identity.provider == "gitlab"
    assert result.identity.subject == "gitlab|777"
    assert result.identity.email == "dana.gitlab@example.com"
    assert result.identity.name == "Dana GitLab"
    assert result.token.access_token == "at-good"
    assert result.flow.provider == "gitlab"


def test_complete_oauth_login_bitbucket_full_flow():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="bitbucket", base_url="https://omnisight.example.com",
        settings_obj=s,
    )

    def userinfo_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer at-good"
        if str(request.url) == "https://api.bitbucket.org/2.0/user":
            return httpx.Response(200, json={
                "uuid": "{0f4c9a2e-6d42-4d1f-a111-5a0f1f99c123}",
                "display_name": "Dana Bitbucket",
            })
        assert str(request.url) == "https://api.bitbucket.org/2.0/user/emails"
        return httpx.Response(200, json={
            "values": [
                {
                    "email": "dana.bitbucket@example.com",
                    "is_primary": True,
                    "is_confirmed": True,
                },
            ],
        })

    client = httpx.AsyncClient(transport=_mock_transport(
        _composite_handler(_good_token_handler(scope="account email"),
                           userinfo_handler)
    ))
    result = _run(olh.complete_oauth_login(
        provider="bitbucket",
        flow_cookie=start.flow_cookie,
        returned_state=start.flow.state,
        code="auth-code",
        settings_obj=s,
        http_client=client,
    ))
    _run(client.aclose())

    assert result.identity.provider == "bitbucket"
    assert result.identity.subject == "{0f4c9a2e-6d42-4d1f-a111-5a0f1f99c123}"
    assert result.identity.email == "dana.bitbucket@example.com"
    assert result.identity.name == "Dana Bitbucket"
    assert result.token.access_token == "at-good"
    assert result.flow.provider == "bitbucket"


def test_complete_oauth_login_slack_full_flow():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="slack", base_url="https://omnisight.example.com",
        settings_obj=s,
    )

    def userinfo_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://slack.com/api/openid.connect.userInfo"
        assert request.headers["authorization"] == "Bearer at-good"
        return httpx.Response(200, json={
            "sub": "U1234567890",
            "email": "dana.slack@example.com",
            "name": "Dana Slack",
            "https://slack.com/team_id": "T123",
        })

    client = httpx.AsyncClient(transport=_mock_transport(
        _composite_handler(
            _good_token_handler(scope="openid email profile"),
            userinfo_handler,
        )
    ))
    result = _run(olh.complete_oauth_login(
        provider="slack",
        flow_cookie=start.flow_cookie,
        returned_state=start.flow.state,
        code="auth-code",
        settings_obj=s,
        http_client=client,
    ))
    _run(client.aclose())

    assert result.identity.provider == "slack"
    assert result.identity.subject == "U1234567890"
    assert result.identity.email == "dana.slack@example.com"
    assert result.identity.name == "Dana Slack"
    assert result.token.access_token == "at-good"
    assert result.flow.provider == "slack"


def test_complete_oauth_login_notion_uses_token_response_identity():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="notion", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["authorization"] == "Basic bnQtaWQ6bnQtc2VjcmV0"
        return httpx.Response(200, json={
            "access_token": "notion-access-token",
            "token_type": "bearer",
            "workspace_name": "OmniSight",
            "owner": {
                "type": "user",
                "user": {
                    "id": "notion-user-123",
                    "name": "Dana Notion",
                    "person": {"email": "dana.notion@example.com"},
                },
            },
        })

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    result = _run(olh.complete_oauth_login(
        provider="notion",
        flow_cookie=start.flow_cookie,
        returned_state=start.flow.state,
        code="auth-code",
        settings_obj=s,
        http_client=client,
    ))
    _run(client.aclose())

    assert seen == ["https://api.notion.com/v1/oauth/token"]
    assert result.identity.provider == "notion"
    assert result.identity.subject == "notion-user-123"
    assert result.identity.email == "dana.notion@example.com"
    assert result.identity.name == "Dana Notion"
    assert result.token.access_token == "notion-access-token"
    assert result.flow.provider == "notion"


def test_complete_oauth_login_salesforce_full_flow():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="salesforce", base_url="https://omnisight.example.com",
        settings_obj=s,
    )

    def userinfo_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://login.salesforce.com/services/oauth2/userinfo"
        )
        assert request.headers["authorization"] == "Bearer at-good"
        return httpx.Response(200, json={
            "user_id": "005xx000001Sv6hAAC",
            "email": "dana.salesforce@example.com",
            "name": "Dana Salesforce",
        })

    client = httpx.AsyncClient(transport=_mock_transport(
        _composite_handler(
            _good_token_handler(scope="id email profile openid"),
            userinfo_handler,
        )
    ))
    result = _run(olh.complete_oauth_login(
        provider="salesforce",
        flow_cookie=start.flow_cookie,
        returned_state=start.flow.state,
        code="auth-code",
        settings_obj=s,
        http_client=client,
    ))
    _run(client.aclose())

    assert result.identity.provider == "salesforce"
    assert result.identity.subject == "005xx000001Sv6hAAC"
    assert result.identity.email == "dana.salesforce@example.com"
    assert result.identity.name == "Dana Salesforce"
    assert result.token.access_token == "at-good"
    assert result.flow.provider == "salesforce"


def test_complete_oauth_login_salesforce_sandbox_uses_base_url_for_token_and_userinfo():
    s = _FakeSettings(oauth_salesforce_login_base_url="https://test.salesforce.com")
    start = olh.begin_oauth_login(
        provider="salesforce", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == "https://test.salesforce.com/services/oauth2/token":
            return httpx.Response(200, json={
                "access_token": "sf-at-1",
                "token_type": "Bearer",
                "scope": "id email profile openid",
            })
        assert str(request.url) == (
            "https://test.salesforce.com/services/oauth2/userinfo"
        )
        return httpx.Response(200, json={
            "user_id": "005xx000001Sv6hAAD",
            "email": "sandbox.salesforce@example.com",
            "name": "Sandbox Salesforce",
        })

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    result = _run(olh.complete_oauth_login(
        provider="salesforce",
        flow_cookie=start.flow_cookie,
        returned_state=start.flow.state,
        code="auth-code",
        settings_obj=s,
        http_client=client,
    ))
    _run(client.aclose())

    assert seen == [
        "https://test.salesforce.com/services/oauth2/token",
        "https://test.salesforce.com/services/oauth2/userinfo",
    ]
    assert result.identity.subject == "005xx000001Sv6hAAD"
    assert result.identity.email == "sandbox.salesforce@example.com"


def test_complete_oauth_login_hubspot_full_flow_uses_bearer_header_userinfo():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="hubspot", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == "https://api.hubapi.com/oauth/v1/token":
            return httpx.Response(200, json={
                "access_token": "hubspot-access-token",
                "refresh_token": "hubspot-refresh-token",
                "expires_in": 1800,
                "scope": "oauth crm.objects.contacts.read",
            })
        assert str(request.url) == "https://api.hubapi.com/integrations/v1/me"
        assert "access_token" not in str(request.url)
        assert request.headers["authorization"] == "Bearer hubspot-access-token"
        return httpx.Response(200, json={
            "user_id": 123456,
            "user": "dana.hubspot@example.com",
            "hub_id": 98765,
            "hub_domain": "example.hubspot.com",
        })

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    result = _run(olh.complete_oauth_login(
        provider="hubspot",
        flow_cookie=start.flow_cookie,
        returned_state=start.flow.state,
        code="auth-code",
        settings_obj=s,
        http_client=client,
    ))
    _run(client.aclose())

    assert seen == [
        "https://api.hubapi.com/oauth/v1/token",
        "https://api.hubapi.com/integrations/v1/me",
    ]
    assert result.identity.provider == "hubspot"
    assert result.identity.subject == "123456"
    assert result.identity.email == "dana.hubspot@example.com"
    assert result.identity.name == "dana.hubspot@example.com"
    assert result.token.access_token == "hubspot-access-token"
    assert result.flow.provider == "hubspot"


def test_complete_oauth_login_apple_uses_id_token_claims():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="apple", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    id_token = _make_jws({"sub": "apple-1", "email": "u@apple.com"})

    client = httpx.AsyncClient(transport=_mock_transport(
        _good_token_handler(scope="name email", id_token=id_token)
    ))
    result = _run(olh.complete_oauth_login(
        provider="apple",
        flow_cookie=start.flow_cookie,
        returned_state=start.flow.state,
        code="auth-code",
        settings_obj=s,
        http_client=client,
    ))
    _run(client.aclose())
    assert result.identity.subject == "apple-1"
    assert result.identity.email == "u@apple.com"


def test_complete_oauth_login_apple_missing_id_token_raises():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="apple", base_url="https://omnisight.example.com",
        settings_obj=s,
    )

    client = httpx.AsyncClient(transport=_mock_transport(_good_token_handler()))
    with pytest.raises(olh.IdentityFieldMissingError):
        _run(olh.complete_oauth_login(
            provider="apple",
            flow_cookie=start.flow_cookie,
            returned_state=start.flow.state,
            code="auth-code",
            settings_obj=s,
            http_client=client,
        ))
    _run(client.aclose())


def test_complete_oauth_login_state_mismatch():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="google", base_url="https://omnisight.example.com",
        settings_obj=s,
    )

    client = httpx.AsyncClient(transport=_mock_transport(_good_token_handler()))
    with pytest.raises(StateMismatchError):
        _run(olh.complete_oauth_login(
            provider="google",
            flow_cookie=start.flow_cookie,
            returned_state="wrong-state",  # mismatch
            code="auth-code",
            settings_obj=s,
            http_client=client,
        ))
    _run(client.aclose())


def test_complete_oauth_login_state_expired():
    s = _FakeSettings()
    # Mint at t=1000, verify at t=10000 (well past 600s TTL).
    start = olh.begin_oauth_login(
        provider="google", base_url="https://omnisight.example.com",
        settings_obj=s, now=1000.0,
    )
    with pytest.raises(StateExpiredError):
        _run(olh.complete_oauth_login(
            provider="google",
            flow_cookie=start.flow_cookie,
            returned_state=start.flow.state,
            code="auth-code",
            settings_obj=s,
            now=10000.0,
        ))


def test_complete_oauth_login_provider_path_vs_cookie_mismatch():
    """A cookie issued for ``google`` cannot be used at the ``github``
    callback URL — must reject."""
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="google", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    with pytest.raises(olh.FlowCookieInvalidError):
        _run(olh.complete_oauth_login(
            provider="github",  # mismatched
            flow_cookie=start.flow_cookie,
            returned_state=start.flow.state,
            code="auth-code",
            settings_obj=s,
        ))


def test_complete_oauth_login_missing_cookie_raises():
    s = _FakeSettings()
    with pytest.raises(olh.FlowCookieMissingError):
        _run(olh.complete_oauth_login(
            provider="google",
            flow_cookie="",
            returned_state="s",
            code="c",
            settings_obj=s,
        ))


def test_complete_oauth_login_tampered_cookie_raises():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="google", base_url="https://omnisight.example.com",
        settings_obj=s,
    )
    body, sig = start.flow_cookie.rsplit(".", 1)
    bad_cookie = body + "." + ("Z" if sig[0] != "Z" else "Y") + sig[1:]
    with pytest.raises(olh.FlowCookieInvalidError):
        _run(olh.complete_oauth_login(
            provider="google",
            flow_cookie=bad_cookie,
            returned_state=start.flow.state,
            code="c",
            settings_obj=s,
        ))


def test_complete_oauth_login_token_endpoint_error_propagates():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="google", base_url="https://omnisight.example.com",
        settings_obj=s,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    client = httpx.AsyncClient(transport=_mock_transport(handler))
    with pytest.raises(TokenResponseError):
        _run(olh.complete_oauth_login(
            provider="google",
            flow_cookie=start.flow_cookie,
            returned_state=start.flow.state,
            code="bad-code",
            settings_obj=s,
            http_client=client,
        ))
    _run(client.aclose())


def test_complete_oauth_login_userinfo_failure_propagates():
    s = _FakeSettings()
    start = olh.begin_oauth_login(
        provider="github", base_url="https://omnisight.example.com",
        settings_obj=s,
    )

    def userinfo_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=_mock_transport(
        _composite_handler(_good_token_handler(scope="read:user user:email"),
                           userinfo_handler)
    ))
    with pytest.raises(olh.UserinfoFetchError):
        _run(olh.complete_oauth_login(
            provider="github",
            flow_cookie=start.flow_cookie,
            returned_state=start.flow.state,
            code="c",
            settings_obj=s,
            http_client=client,
        ))
    _run(client.aclose())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 13 — Module-global state audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_module_reload_stable():
    """Importing the module twice must produce identical constants."""
    h1 = importlib.reload(olh)
    h2 = importlib.reload(olh)
    assert h1.SUPPORTED_PROVIDERS == h2.SUPPORTED_PROVIDERS
    assert h1.FLOW_COOKIE_NAME == h2.FLOW_COOKIE_NAME
    assert h1.FLOW_COOKIE_TTL_SECONDS == h2.FLOW_COOKIE_TTL_SECONDS


def test_mask_email_basic():
    assert olh.mask_email("foo@bar.com").startswith("fo")
    assert olh.mask_email("foo@bar.com").endswith("@bar.com")
    assert olh.mask_email("") == "***"
    assert olh.mask_email("noatmark") == "no***"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 14 — Settings field declaration drift guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_settings_declares_all_oauth_credential_fields():
    """SOP §3 drift guard — adding a provider to SUPPORTED_PROVIDERS
    without adding the matching Settings fields would silently break
    lookup_provider_credentials at runtime."""
    from backend.config import Settings
    expected = set()
    for p in olh.SUPPORTED_PROVIDERS:
        expected.add(f"oauth_{p}_client_id")
        expected.add(f"oauth_{p}_client_secret")
    declared = set(Settings.model_fields.keys())
    missing = expected - declared
    assert not missing, f"Settings missing oauth fields: {missing}"


def test_settings_declares_redirect_base_and_signing_key():
    from backend.config import Settings
    assert "oauth_redirect_base_url" in Settings.model_fields
    assert "oauth_flow_signing_key" in Settings.model_fields


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 15 — Google/GitHub/Microsoft/Apple/Discord/GitLab/Bitbucket router integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
async def _google_oauth_http_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """PG-backed HTTP fixture for the Google authorize → callback flow.

    Mirrors the K3/Q2 E2E fixtures: install the shared pg_test_pool,
    pin bootstrap green, set the Google OAuth Settings singleton knobs,
    and keep token/userinfo network calls mocked at the handler boundary.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    monkeypatch.setenv("OMNISIGHT_COOKIE_SECURE", "false")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import bootstrap as _boot
    from backend import config as _cfg
    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    monkeypatch.setattr(_cfg.settings, "oauth_google_client_id", "google-client-id")
    monkeypatch.setattr(
        _cfg.settings, "oauth_google_client_secret", "google-client-secret"
    )
    monkeypatch.setattr(
        _cfg.settings, "oauth_redirect_base_url", "https://omnisight.example.com"
    )
    monkeypatch.setattr(
        _cfg.settings,
        "oauth_flow_signing_key",
        "google-oauth-flow-signing-key-2026",
    )

    async def _fake_exchange_authorization_code(**kwargs):
        assert kwargs["client_id"] == "google-client-id"
        assert kwargs["client_secret"] == "google-client-secret"
        assert kwargs["code"] == "google-auth-code"
        assert kwargs["redirect_uri"] == (
            "https://omnisight.example.com/api/v1/auth/oauth/google/callback"
        )
        return oc.TokenSet(
            access_token="google-access-token",
            refresh_token="google-refresh-token",
            token_type="Bearer",
            expires_at=time.time() + 3600,
            scope=("openid", "email", "profile"),
            id_token="header.payload.sig",
            raw={"access_token": "google-access-token"},
        )

    async def _fake_fetch_userinfo(**kwargs):
        assert kwargs["access_token"] == "google-access-token"
        return {
            "sub": "google-subject-1",
            "email": "Alice.Google@Example.COM",
            "name": "Alice Google",
        }

    monkeypatch.setattr(
        olh, "exchange_authorization_code", _fake_exchange_authorization_code
    )
    monkeypatch.setattr(olh, "fetch_userinfo", _fake_fetch_userinfo)
    monkeypatch.setattr(
        "backend.routers.auth._oauth_log_audit_safe",
        lambda *args, **kwargs: None,
    )

    if db._db is not None:
        await db.close()
    await db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            yield {"client": ac, "pool": pg_test_pool}
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


@pytest.mark.asyncio
async def test_google_oauth_authorize_callback_establishes_session(
    _google_oauth_http_client,
):
    env = _google_oauth_http_client
    client = env["client"]

    authorize = await client.get("/api/v1/auth/oauth/google/authorize")

    assert authorize.status_code == 302
    assert olh.FLOW_COOKIE_NAME in client.cookies
    location = authorize.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert query["client_id"] == ["google-client-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/google/callback"
    ]
    assert query["scope"] == ["openid email profile"]
    state = query["state"][0]

    callback = await client.get(
        "/api/v1/auth/oauth/google/callback",
        params={"code": "google-auth-code", "state": state},
        headers={
            "cf-connecting-ip": "203.0.113.7",
            "user-agent": "pytest-google-oauth",
        },
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    assert olh.FLOW_COOKIE_NAME not in client.cookies
    assert "omnisight_session" in client.cookies
    assert "omnisight_csrf" in client.cookies

    async with env["pool"].acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, name, role, oidc_provider, oidc_subject, "
            "auth_methods FROM users WHERE email = $1",
            "alice.google@example.com",
        )
        assert user is not None
        session = await conn.fetchrow(
            "SELECT user_id FROM sessions WHERE user_id = $1",
            user["id"],
        )
        assert session is not None

    auth_methods = user["auth_methods"]
    if isinstance(auth_methods, str):
        auth_methods = json.loads(auth_methods)
    assert user["name"] == "Alice Google"
    assert user["role"] == "viewer"
    assert user["oidc_provider"] == "google"
    assert user["oidc_subject"] == "google-subject-1"
    assert auth_methods == ["oauth_google"]
    assert session["user_id"] == user["id"]


@pytest.fixture()
async def _github_oauth_http_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """PG-backed HTTP fixture for the GitHub authorize → callback flow.

    Mirrors the Google OAuth E2E fixture above: install the shared
    pg_test_pool, pin bootstrap green, set the GitHub OAuth Settings
    singleton knobs, and mock token/userinfo at the handler boundary.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    monkeypatch.setenv("OMNISIGHT_COOKIE_SECURE", "false")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import bootstrap as _boot
    from backend import config as _cfg
    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    monkeypatch.setattr(_cfg.settings, "oauth_github_client_id", "github-client-id")
    monkeypatch.setattr(
        _cfg.settings, "oauth_github_client_secret", "github-client-secret"
    )
    monkeypatch.setattr(
        _cfg.settings, "oauth_redirect_base_url", "https://omnisight.example.com"
    )
    monkeypatch.setattr(
        _cfg.settings,
        "oauth_flow_signing_key",
        "github-oauth-flow-signing-key-2026",
    )

    async def _fake_exchange_authorization_code(**kwargs):
        assert kwargs["client_id"] == "github-client-id"
        assert kwargs["client_secret"] == "github-client-secret"
        assert kwargs["code"] == "github-auth-code"
        assert kwargs["redirect_uri"] == (
            "https://omnisight.example.com/api/v1/auth/oauth/github/callback"
        )
        return oc.TokenSet(
            access_token="github-access-token",
            refresh_token=None,
            token_type="Bearer",
            expires_at=None,
            scope=("read:user", "user:email"),
            id_token=None,
            raw={"access_token": "github-access-token"},
        )

    async def _fake_fetch_userinfo(**kwargs):
        assert kwargs["vendor"].provider_id == "github"
        assert kwargs["access_token"] == "github-access-token"
        return {
            "id": 424242,
            "email": "Bob.GitHub@Example.COM",
            "name": "Bob GitHub",
            "login": "bob-gh",
        }

    monkeypatch.setattr(
        olh, "exchange_authorization_code", _fake_exchange_authorization_code
    )
    monkeypatch.setattr(olh, "fetch_userinfo", _fake_fetch_userinfo)
    monkeypatch.setattr(
        "backend.routers.auth._oauth_log_audit_safe",
        lambda *args, **kwargs: None,
    )

    if db._db is not None:
        await db.close()
    await db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            yield {"client": ac, "pool": pg_test_pool}
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


@pytest.mark.asyncio
async def test_github_oauth_authorize_callback_establishes_session(
    _github_oauth_http_client,
):
    env = _github_oauth_http_client
    client = env["client"]

    authorize = await client.get("/api/v1/auth/oauth/github/authorize")

    assert authorize.status_code == 302
    assert olh.FLOW_COOKIE_NAME in client.cookies
    location = authorize.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "github.com"
    assert parsed.path == "/login/oauth/authorize"
    assert query["client_id"] == ["github-client-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/github/callback"
    ]
    assert query["scope"] == ["read:user user:email"]
    assert query["allow_signup"] == ["true"]
    state = query["state"][0]

    callback = await client.get(
        "/api/v1/auth/oauth/github/callback",
        params={"code": "github-auth-code", "state": state},
        headers={
            "cf-connecting-ip": "203.0.113.8",
            "user-agent": "pytest-github-oauth",
        },
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    assert olh.FLOW_COOKIE_NAME not in client.cookies
    assert "omnisight_session" in client.cookies
    assert "omnisight_csrf" in client.cookies

    async with env["pool"].acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, name, role, oidc_provider, oidc_subject, "
            "auth_methods FROM users WHERE email = $1",
            "bob.github@example.com",
        )
        assert user is not None
        session = await conn.fetchrow(
            "SELECT user_id FROM sessions WHERE user_id = $1",
            user["id"],
        )
        assert session is not None

    auth_methods = user["auth_methods"]
    if isinstance(auth_methods, str):
        auth_methods = json.loads(auth_methods)
    assert user["name"] == "Bob GitHub"
    assert user["role"] == "viewer"
    assert user["oidc_provider"] == "github"
    assert user["oidc_subject"] == "424242"
    assert auth_methods == ["oauth_github"]
    assert session["user_id"] == user["id"]


@pytest.fixture()
async def _discord_oauth_http_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """PG-backed HTTP fixture for the Discord authorize → callback flow.

    Mirrors the GitHub OAuth E2E fixture above: install the shared
    pg_test_pool, pin bootstrap green, set the Discord OAuth Settings
    singleton knobs, and mock token/userinfo at the handler boundary.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    monkeypatch.setenv("OMNISIGHT_COOKIE_SECURE", "false")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import bootstrap as _boot
    from backend import config as _cfg
    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    monkeypatch.setattr(_cfg.settings, "oauth_discord_client_id", "discord-client-id")
    monkeypatch.setattr(
        _cfg.settings, "oauth_discord_client_secret", "discord-client-secret"
    )
    monkeypatch.setattr(
        _cfg.settings, "oauth_redirect_base_url", "https://omnisight.example.com"
    )
    monkeypatch.setattr(
        _cfg.settings,
        "oauth_flow_signing_key",
        "discord-oauth-flow-signing-key-2026",
    )

    async def _fake_exchange_authorization_code(**kwargs):
        assert kwargs["client_id"] == "discord-client-id"
        assert kwargs["client_secret"] == "discord-client-secret"
        assert kwargs["code"] == "discord-auth-code"
        assert kwargs["redirect_uri"] == (
            "https://omnisight.example.com/api/v1/auth/oauth/discord/callback"
        )
        return oc.TokenSet(
            access_token="discord-access-token",
            refresh_token="discord-refresh-token",
            token_type="Bearer",
            expires_at=time.time() + 3600,
            scope=("identify", "email"),
            id_token=None,
            raw={"access_token": "discord-access-token"},
        )

    async def _fake_fetch_userinfo(**kwargs):
        assert kwargs["vendor"].provider_id == "discord"
        assert kwargs["access_token"] == "discord-access-token"
        return {
            "id": "80351110224678912",
            "email": "Eve.Discord@Example.COM",
            "global_name": "Eve Discord",
            "username": "eve-discord",
        }

    monkeypatch.setattr(
        olh, "exchange_authorization_code", _fake_exchange_authorization_code
    )
    monkeypatch.setattr(olh, "fetch_userinfo", _fake_fetch_userinfo)
    monkeypatch.setattr(
        "backend.routers.auth._oauth_log_audit_safe",
        lambda *args, **kwargs: None,
    )

    if db._db is not None:
        await db.close()
    await db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            yield {"client": ac, "pool": pg_test_pool}
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


@pytest.mark.asyncio
async def test_discord_oauth_authorize_callback_establishes_session(
    _discord_oauth_http_client,
):
    env = _discord_oauth_http_client
    client = env["client"]

    authorize = await client.get("/api/v1/auth/oauth/discord/authorize")

    assert authorize.status_code == 302
    assert olh.FLOW_COOKIE_NAME in client.cookies
    location = authorize.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "discord.com"
    assert parsed.path == "/oauth2/authorize"
    assert query["client_id"] == ["discord-client-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/discord/callback"
    ]
    assert query["scope"] == ["identify email"]
    state = query["state"][0]

    callback = await client.get(
        "/api/v1/auth/oauth/discord/callback",
        params={"code": "discord-auth-code", "state": state},
        headers={
            "cf-connecting-ip": "203.0.113.10",
            "user-agent": "pytest-discord-oauth",
        },
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    assert olh.FLOW_COOKIE_NAME not in client.cookies
    assert "omnisight_session" in client.cookies
    assert "omnisight_csrf" in client.cookies

    async with env["pool"].acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, name, role, oidc_provider, oidc_subject, "
            "auth_methods FROM users WHERE email = $1",
            "eve.discord@example.com",
        )
        assert user is not None
        session = await conn.fetchrow(
            "SELECT user_id FROM sessions WHERE user_id = $1",
            user["id"],
        )
        assert session is not None

    auth_methods = user["auth_methods"]
    if isinstance(auth_methods, str):
        auth_methods = json.loads(auth_methods)
    assert user["name"] == "Eve Discord"
    assert user["role"] == "viewer"
    assert user["oidc_provider"] == "discord"
    assert user["oidc_subject"] == "80351110224678912"
    assert auth_methods == ["oauth_discord"]
    assert session["user_id"] == user["id"]


@pytest.fixture()
async def _gitlab_oauth_http_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """PG-backed HTTP fixture for the GitLab authorize → callback flow.

    Mirrors the Discord OAuth E2E fixture above: install the shared
    pg_test_pool, pin bootstrap green, set the GitLab OAuth Settings
    singleton knobs, and mock token/userinfo at the handler boundary.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    monkeypatch.setenv("OMNISIGHT_COOKIE_SECURE", "false")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import bootstrap as _boot
    from backend import config as _cfg
    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    monkeypatch.setattr(_cfg.settings, "oauth_gitlab_client_id", "gitlab-client-id")
    monkeypatch.setattr(
        _cfg.settings, "oauth_gitlab_client_secret", "gitlab-client-secret"
    )
    monkeypatch.setattr(
        _cfg.settings, "oauth_redirect_base_url", "https://omnisight.example.com"
    )
    monkeypatch.setattr(
        _cfg.settings,
        "oauth_flow_signing_key",
        "gitlab-oauth-flow-signing-key-2026",
    )

    async def _fake_exchange_authorization_code(**kwargs):
        assert kwargs["client_id"] == "gitlab-client-id"
        assert kwargs["client_secret"] == "gitlab-client-secret"
        assert kwargs["code"] == "gitlab-auth-code"
        assert kwargs["redirect_uri"] == (
            "https://omnisight.example.com/api/v1/auth/oauth/gitlab/callback"
        )
        return oc.TokenSet(
            access_token="gitlab-access-token",
            refresh_token="gitlab-refresh-token",
            token_type="Bearer",
            expires_at=time.time() + 3600,
            scope=("read_user", "openid", "email", "profile"),
            id_token=None,
            raw={"access_token": "gitlab-access-token"},
        )

    async def _fake_fetch_userinfo(**kwargs):
        assert kwargs["vendor"].provider_id == "gitlab"
        assert kwargs["access_token"] == "gitlab-access-token"
        return {
            "sub": "gitlab|9001",
            "email": "Eve.GitLab@Example.COM",
            "name": "Eve GitLab",
            "nickname": "eve-gitlab",
        }

    monkeypatch.setattr(
        olh, "exchange_authorization_code", _fake_exchange_authorization_code
    )
    monkeypatch.setattr(olh, "fetch_userinfo", _fake_fetch_userinfo)
    monkeypatch.setattr(
        "backend.routers.auth._oauth_log_audit_safe",
        lambda *args, **kwargs: None,
    )

    if db._db is not None:
        await db.close()
    await db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            yield {"client": ac, "pool": pg_test_pool}
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


@pytest.mark.asyncio
async def test_gitlab_oauth_authorize_callback_establishes_session(
    _gitlab_oauth_http_client,
):
    env = _gitlab_oauth_http_client
    client = env["client"]

    authorize = await client.get("/api/v1/auth/oauth/gitlab/authorize")

    assert authorize.status_code == 302
    assert olh.FLOW_COOKIE_NAME in client.cookies
    location = authorize.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "gitlab.com"
    assert parsed.path == "/oauth/authorize"
    assert query["client_id"] == ["gitlab-client-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/gitlab/callback"
    ]
    assert query["scope"] == ["read_user openid email profile"]
    state = query["state"][0]

    callback = await client.get(
        "/api/v1/auth/oauth/gitlab/callback",
        params={"code": "gitlab-auth-code", "state": state},
        headers={
            "cf-connecting-ip": "203.0.113.11",
            "user-agent": "pytest-gitlab-oauth",
        },
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    assert olh.FLOW_COOKIE_NAME not in client.cookies
    assert "omnisight_session" in client.cookies
    assert "omnisight_csrf" in client.cookies

    async with env["pool"].acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, name, role, oidc_provider, oidc_subject, "
            "auth_methods FROM users WHERE email = $1",
            "eve.gitlab@example.com",
        )
        assert user is not None
        session = await conn.fetchrow(
            "SELECT user_id FROM sessions WHERE user_id = $1",
            user["id"],
        )
        assert session is not None

    auth_methods = user["auth_methods"]
    if isinstance(auth_methods, str):
        auth_methods = json.loads(auth_methods)
    assert user["name"] == "Eve GitLab"
    assert user["role"] == "viewer"
    assert user["oidc_provider"] == "gitlab"
    assert user["oidc_subject"] == "gitlab|9001"
    assert auth_methods == ["oauth_gitlab"]
    assert session["user_id"] == user["id"]


@pytest.fixture()
async def _bitbucket_oauth_http_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """PG-backed HTTP fixture for the Bitbucket authorize → callback flow.

    Mirrors the GitLab OAuth E2E fixture above: install the shared
    pg_test_pool, pin bootstrap green, set the Bitbucket OAuth Settings
    singleton knobs, and mock token/userinfo at the handler boundary.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    monkeypatch.setenv("OMNISIGHT_COOKIE_SECURE", "false")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import bootstrap as _boot
    from backend import config as _cfg
    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    monkeypatch.setattr(
        _cfg.settings, "oauth_bitbucket_client_id", "bitbucket-client-id"
    )
    monkeypatch.setattr(
        _cfg.settings, "oauth_bitbucket_client_secret", "bitbucket-client-secret"
    )
    monkeypatch.setattr(
        _cfg.settings, "oauth_redirect_base_url", "https://omnisight.example.com"
    )
    monkeypatch.setattr(
        _cfg.settings,
        "oauth_flow_signing_key",
        "bitbucket-oauth-flow-signing-key-2026",
    )

    async def _fake_exchange_authorization_code(**kwargs):
        assert kwargs["client_id"] == "bitbucket-client-id"
        assert kwargs["client_secret"] == "bitbucket-client-secret"
        assert kwargs["code"] == "bitbucket-auth-code"
        assert kwargs["redirect_uri"] == (
            "https://omnisight.example.com/api/v1/auth/oauth/bitbucket/callback"
        )
        return oc.TokenSet(
            access_token="bitbucket-access-token",
            refresh_token="bitbucket-refresh-token",
            token_type="Bearer",
            expires_at=time.time() + 3600,
            scope=("account", "email"),
            id_token=None,
            raw={"access_token": "bitbucket-access-token"},
        )

    async def _fake_fetch_userinfo(**kwargs):
        assert kwargs["vendor"].provider_id == "bitbucket"
        assert kwargs["access_token"] == "bitbucket-access-token"
        return {
            "uuid": "{0f4c9a2e-6d42-4d1f-a111-5a0f1f99c123}",
            "email": "Eve.Bitbucket@Example.COM",
            "display_name": "Eve Bitbucket",
            "nickname": "eve-bitbucket",
        }

    monkeypatch.setattr(
        olh, "exchange_authorization_code", _fake_exchange_authorization_code
    )
    monkeypatch.setattr(olh, "fetch_userinfo", _fake_fetch_userinfo)
    monkeypatch.setattr(
        "backend.routers.auth._oauth_log_audit_safe",
        lambda *args, **kwargs: None,
    )

    if db._db is not None:
        await db.close()
    await db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            yield {"client": ac, "pool": pg_test_pool}
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


@pytest.mark.asyncio
async def test_bitbucket_oauth_authorize_callback_establishes_session(
    _bitbucket_oauth_http_client,
):
    env = _bitbucket_oauth_http_client
    client = env["client"]

    authorize = await client.get("/api/v1/auth/oauth/bitbucket/authorize")

    assert authorize.status_code == 302
    assert olh.FLOW_COOKIE_NAME in client.cookies
    location = authorize.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "bitbucket.org"
    assert parsed.path == "/site/oauth2/authorize"
    assert query["client_id"] == ["bitbucket-client-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/bitbucket/callback"
    ]
    assert query["scope"] == ["account email"]
    state = query["state"][0]

    callback = await client.get(
        "/api/v1/auth/oauth/bitbucket/callback",
        params={"code": "bitbucket-auth-code", "state": state},
        headers={
            "cf-connecting-ip": "203.0.113.11",
            "user-agent": "pytest-bitbucket-oauth",
        },
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    assert olh.FLOW_COOKIE_NAME not in client.cookies
    assert "omnisight_session" in client.cookies
    assert "omnisight_csrf" in client.cookies

    async with env["pool"].acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, name, role, oidc_provider, oidc_subject, "
            "auth_methods FROM users WHERE email = $1",
            "eve.bitbucket@example.com",
        )
        assert user is not None
        session = await conn.fetchrow(
            "SELECT user_id FROM sessions WHERE user_id = $1",
            user["id"],
        )
        assert session is not None

    auth_methods = user["auth_methods"]
    if isinstance(auth_methods, str):
        auth_methods = json.loads(auth_methods)
    assert user["name"] == "Eve Bitbucket"
    assert user["role"] == "viewer"
    assert user["oidc_provider"] == "bitbucket"
    assert user["oidc_subject"] == "{0f4c9a2e-6d42-4d1f-a111-5a0f1f99c123}"
    assert auth_methods == ["oauth_bitbucket"]
    assert session["user_id"] == user["id"]


@pytest.fixture()
async def _slack_oauth_http_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """PG-backed HTTP fixture for the Slack authorize → callback flow.

    Mirrors the Bitbucket OAuth E2E fixture above: install the shared
    pg_test_pool, pin bootstrap green, set the Slack OAuth Settings
    singleton knobs, and mock token/userinfo at the handler boundary.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    monkeypatch.setenv("OMNISIGHT_COOKIE_SECURE", "false")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import bootstrap as _boot
    from backend import config as _cfg
    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    monkeypatch.setattr(_cfg.settings, "oauth_slack_client_id", "slack-client-id")
    monkeypatch.setattr(
        _cfg.settings, "oauth_slack_client_secret", "slack-client-secret"
    )
    monkeypatch.setattr(
        _cfg.settings, "oauth_redirect_base_url", "https://omnisight.example.com"
    )
    monkeypatch.setattr(
        _cfg.settings,
        "oauth_flow_signing_key",
        "slack-oauth-flow-signing-key-2026",
    )

    async def _fake_exchange_authorization_code(**kwargs):
        assert kwargs["client_id"] == "slack-client-id"
        assert kwargs["client_secret"] == "slack-client-secret"
        assert kwargs["code"] == "slack-auth-code"
        assert kwargs["redirect_uri"] == (
            "https://omnisight.example.com/api/v1/auth/oauth/slack/callback"
        )
        return oc.TokenSet(
            access_token="slack-access-token",
            refresh_token=None,
            token_type="Bearer",
            expires_at=time.time() + 3600,
            scope=("openid", "email", "profile"),
            id_token=None,
            raw={"access_token": "slack-access-token"},
        )

    async def _fake_fetch_userinfo(**kwargs):
        assert kwargs["vendor"].provider_id == "slack"
        assert kwargs["access_token"] == "slack-access-token"
        return {
            "sub": "U1234567890",
            "email": "Eve.Slack@Example.COM",
            "name": "Eve Slack",
            "https://slack.com/team_id": "T123",
        }

    monkeypatch.setattr(
        olh, "exchange_authorization_code", _fake_exchange_authorization_code
    )
    monkeypatch.setattr(olh, "fetch_userinfo", _fake_fetch_userinfo)
    monkeypatch.setattr(
        "backend.routers.auth._oauth_log_audit_safe",
        lambda *args, **kwargs: None,
    )

    if db._db is not None:
        await db.close()
    await db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            yield {"client": ac, "pool": pg_test_pool}
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


@pytest.mark.asyncio
async def test_slack_oauth_authorize_callback_establishes_session(
    _slack_oauth_http_client,
):
    env = _slack_oauth_http_client
    client = env["client"]

    authorize = await client.get("/api/v1/auth/oauth/slack/authorize")

    assert authorize.status_code == 302
    assert olh.FLOW_COOKIE_NAME in client.cookies
    location = authorize.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "slack.com"
    assert parsed.path == "/openid/connect/authorize"
    assert query["client_id"] == ["slack-client-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/slack/callback"
    ]
    assert query["scope"] == ["openid email profile"]
    state = query["state"][0]

    callback = await client.get(
        "/api/v1/auth/oauth/slack/callback",
        params={"code": "slack-auth-code", "state": state},
        headers={
            "cf-connecting-ip": "203.0.113.11",
            "user-agent": "pytest-slack-oauth",
        },
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    assert olh.FLOW_COOKIE_NAME not in client.cookies
    assert "omnisight_session" in client.cookies
    assert "omnisight_csrf" in client.cookies

    async with env["pool"].acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, name, role, oidc_provider, oidc_subject, "
            "auth_methods FROM users WHERE email = $1",
            "eve.slack@example.com",
        )
        assert user is not None
        session = await conn.fetchrow(
            "SELECT user_id FROM sessions WHERE user_id = $1",
            user["id"],
        )
        assert session is not None

    auth_methods = user["auth_methods"]
    if isinstance(auth_methods, str):
        auth_methods = json.loads(auth_methods)
    assert user["name"] == "Eve Slack"
    assert user["role"] == "viewer"
    assert user["oidc_provider"] == "slack"
    assert user["oidc_subject"] == "U1234567890"
    assert auth_methods == ["oauth_slack"]
    assert session["user_id"] == user["id"]


@pytest.fixture()
async def _notion_oauth_http_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """PG-backed HTTP fixture for the Notion authorize → callback flow.

    Mirrors the Slack OAuth E2E fixture above: install the shared
    pg_test_pool, pin bootstrap green, set the Notion OAuth Settings
    singleton knobs, and mock token exchange at the handler boundary.
    Notion returns owner.user in the token response, so no userinfo
    mock is installed.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    monkeypatch.setenv("OMNISIGHT_COOKIE_SECURE", "false")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import bootstrap as _boot
    from backend import config as _cfg
    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    monkeypatch.setattr(_cfg.settings, "oauth_notion_client_id", "notion-client-id")
    monkeypatch.setattr(
        _cfg.settings, "oauth_notion_client_secret", "notion-client-secret"
    )
    monkeypatch.setattr(
        _cfg.settings, "oauth_redirect_base_url", "https://omnisight.example.com"
    )
    monkeypatch.setattr(
        _cfg.settings,
        "oauth_flow_signing_key",
        "notion-oauth-flow-signing-key-2026",
    )

    async def _fake_exchange_authorization_code(**kwargs):
        assert kwargs["client_id"] == "notion-client-id"
        assert kwargs["client_secret"] == "notion-client-secret"
        assert kwargs["code"] == "notion-auth-code"
        assert kwargs["redirect_uri"] == (
            "https://omnisight.example.com/api/v1/auth/oauth/notion/callback"
        )
        return oc.TokenSet(
            access_token="notion-access-token",
            refresh_token=None,
            token_type="bearer",
            expires_at=None,
            scope=(),
            id_token=None,
            raw={
                "access_token": "notion-access-token",
                "workspace_name": "OmniSight",
                "owner": {
                    "type": "user",
                    "user": {
                        "id": "notion-user-123",
                        "name": "Eve Notion",
                        "person": {"email": "Eve.Notion@Example.COM"},
                    },
                },
            },
        )

    monkeypatch.setattr(
        olh, "exchange_authorization_code", _fake_exchange_authorization_code
    )
    monkeypatch.setattr(
        "backend.routers.auth._oauth_log_audit_safe",
        lambda *args, **kwargs: None,
    )

    if db._db is not None:
        await db.close()
    await db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            yield {"client": ac, "pool": pg_test_pool}
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


@pytest.mark.asyncio
async def test_notion_oauth_authorize_callback_establishes_session(
    _notion_oauth_http_client,
):
    env = _notion_oauth_http_client
    client = env["client"]

    authorize = await client.get("/api/v1/auth/oauth/notion/authorize")

    assert authorize.status_code == 302
    assert olh.FLOW_COOKIE_NAME in client.cookies
    location = authorize.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "api.notion.com"
    assert parsed.path == "/v1/oauth/authorize"
    assert query["client_id"] == ["notion-client-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/notion/callback"
    ]
    assert "scope" not in query
    state = query["state"][0]

    callback = await client.get(
        "/api/v1/auth/oauth/notion/callback",
        params={"code": "notion-auth-code", "state": state},
        headers={
            "cf-connecting-ip": "203.0.113.12",
            "user-agent": "pytest-notion-oauth",
        },
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    assert olh.FLOW_COOKIE_NAME not in client.cookies
    assert "omnisight_session" in client.cookies
    assert "omnisight_csrf" in client.cookies

    async with env["pool"].acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, name, role, oidc_provider, oidc_subject, "
            "auth_methods FROM users WHERE email = $1",
            "eve.notion@example.com",
        )
        assert user is not None
        session = await conn.fetchrow(
            "SELECT user_id FROM sessions WHERE user_id = $1",
            user["id"],
        )
        assert session is not None

    auth_methods = user["auth_methods"]
    if isinstance(auth_methods, str):
        auth_methods = json.loads(auth_methods)
    assert user["name"] == "Eve Notion"
    assert user["role"] == "viewer"
    assert user["oidc_provider"] == "notion"
    assert user["oidc_subject"] == "notion-user-123"
    assert auth_methods == ["oauth_notion"]
    assert session["user_id"] == user["id"]


@pytest.fixture()
async def _salesforce_oauth_http_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """PG-backed HTTP fixture for the Salesforce authorize → callback flow.

    Mirrors the Notion OAuth E2E fixture above: install the shared
    pg_test_pool, pin bootstrap green, set the Salesforce OAuth Settings
    singleton knobs, and mock token/userinfo at the handler boundary.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    monkeypatch.setenv("OMNISIGHT_COOKIE_SECURE", "false")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import bootstrap as _boot
    from backend import config as _cfg
    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    monkeypatch.setattr(
        _cfg.settings, "oauth_salesforce_client_id", "salesforce-client-id"
    )
    monkeypatch.setattr(
        _cfg.settings, "oauth_salesforce_client_secret", "salesforce-client-secret"
    )
    monkeypatch.setattr(_cfg.settings, "oauth_salesforce_login_base_url", "")
    monkeypatch.setattr(
        _cfg.settings, "oauth_redirect_base_url", "https://omnisight.example.com"
    )
    monkeypatch.setattr(
        _cfg.settings,
        "oauth_flow_signing_key",
        "salesforce-oauth-flow-signing-key-2026",
    )

    async def _fake_exchange_authorization_code(**kwargs):
        assert kwargs["client_id"] == "salesforce-client-id"
        assert kwargs["client_secret"] == "salesforce-client-secret"
        assert kwargs["code"] == "salesforce-auth-code"
        assert kwargs["vendor"].provider_id == "salesforce"
        assert kwargs["vendor"].token_endpoint == (
            "https://login.salesforce.com/services/oauth2/token"
        )
        assert kwargs["redirect_uri"] == (
            "https://omnisight.example.com/api/v1/auth/oauth/salesforce/callback"
        )
        return oc.TokenSet(
            access_token="salesforce-access-token",
            refresh_token="salesforce-refresh-token",
            token_type="Bearer",
            expires_at=time.time() + 3600,
            scope=("id", "email", "profile", "openid"),
            id_token=None,
            raw={"access_token": "salesforce-access-token"},
        )

    async def _fake_fetch_userinfo(**kwargs):
        assert kwargs["vendor"].provider_id == "salesforce"
        assert kwargs["vendor"].userinfo_endpoint == (
            "https://login.salesforce.com/services/oauth2/userinfo"
        )
        assert kwargs["access_token"] == "salesforce-access-token"
        return {
            "user_id": "005xx000001Sv6hAAC",
            "email": "Eve.Salesforce@Example.COM",
            "name": "Eve Salesforce",
        }

    monkeypatch.setattr(
        olh, "exchange_authorization_code", _fake_exchange_authorization_code
    )
    monkeypatch.setattr(olh, "fetch_userinfo", _fake_fetch_userinfo)
    monkeypatch.setattr(
        "backend.routers.auth._oauth_log_audit_safe",
        lambda *args, **kwargs: None,
    )

    if db._db is not None:
        await db.close()
    await db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            yield {"client": ac, "pool": pg_test_pool}
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


@pytest.mark.asyncio
async def test_salesforce_oauth_authorize_callback_establishes_session(
    _salesforce_oauth_http_client,
):
    env = _salesforce_oauth_http_client
    client = env["client"]

    authorize = await client.get("/api/v1/auth/oauth/salesforce/authorize")

    assert authorize.status_code == 302
    assert olh.FLOW_COOKIE_NAME in client.cookies
    location = authorize.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "login.salesforce.com"
    assert parsed.path == "/services/oauth2/authorize"
    assert query["client_id"] == ["salesforce-client-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/salesforce/callback"
    ]
    assert query["scope"] == ["id email profile openid"]
    state = query["state"][0]

    callback = await client.get(
        "/api/v1/auth/oauth/salesforce/callback",
        params={"code": "salesforce-auth-code", "state": state},
        headers={
            "cf-connecting-ip": "203.0.113.13",
            "user-agent": "pytest-salesforce-oauth",
        },
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    assert olh.FLOW_COOKIE_NAME not in client.cookies
    assert "omnisight_session" in client.cookies
    assert "omnisight_csrf" in client.cookies

    async with env["pool"].acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, name, role, oidc_provider, oidc_subject, "
            "auth_methods FROM users WHERE email = $1",
            "eve.salesforce@example.com",
        )
        assert user is not None
        session = await conn.fetchrow(
            "SELECT user_id FROM sessions WHERE user_id = $1",
            user["id"],
        )
        assert session is not None

    auth_methods = user["auth_methods"]
    if isinstance(auth_methods, str):
        auth_methods = json.loads(auth_methods)
    assert user["name"] == "Eve Salesforce"
    assert user["role"] == "viewer"
    assert user["oidc_provider"] == "salesforce"
    assert user["oidc_subject"] == "005xx000001Sv6hAAC"
    assert auth_methods == ["oauth_salesforce"]
    assert session["user_id"] == user["id"]


@pytest.fixture()
async def _hubspot_oauth_http_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """PG-backed HTTP fixture for the HubSpot authorize → callback flow.

    Mirrors the Salesforce OAuth E2E fixture above: install the shared
    pg_test_pool, pin bootstrap green, set the HubSpot OAuth Settings
    singleton knobs, and mock token/userinfo at the handler boundary.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    monkeypatch.setenv("OMNISIGHT_COOKIE_SECURE", "false")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import bootstrap as _boot
    from backend import config as _cfg
    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    monkeypatch.setattr(_cfg.settings, "oauth_hubspot_client_id", "hubspot-client-id")
    monkeypatch.setattr(
        _cfg.settings, "oauth_hubspot_client_secret", "hubspot-client-secret"
    )
    monkeypatch.setattr(
        _cfg.settings, "oauth_redirect_base_url", "https://omnisight.example.com"
    )
    monkeypatch.setattr(
        _cfg.settings,
        "oauth_flow_signing_key",
        "hubspot-oauth-flow-signing-key-2026",
    )

    async def _fake_exchange_authorization_code(**kwargs):
        assert kwargs["client_id"] == "hubspot-client-id"
        assert kwargs["client_secret"] == "hubspot-client-secret"
        assert kwargs["code"] == "hubspot-auth-code"
        assert kwargs["vendor"].provider_id == "hubspot"
        assert kwargs["vendor"].token_endpoint == "https://api.hubapi.com/oauth/v1/token"
        assert kwargs["redirect_uri"] == (
            "https://omnisight.example.com/api/v1/auth/oauth/hubspot/callback"
        )
        return oc.TokenSet(
            access_token="hubspot-access-token",
            refresh_token="hubspot-refresh-token",
            token_type="Bearer",
            expires_at=time.time() + 1800,
            scope=("oauth", "crm.objects.contacts.read"),
            id_token=None,
            raw={"access_token": "hubspot-access-token"},
        )

    async def _fake_fetch_userinfo(**kwargs):
        assert kwargs["vendor"].provider_id == "hubspot"
        assert kwargs["vendor"].userinfo_endpoint == (
            "https://api.hubapi.com/integrations/v1/me"
        )
        assert kwargs["access_token"] == "hubspot-access-token"
        return {
            "user_id": 123456,
            "user": "Eve.HubSpot@Example.COM",
            "hub_id": 98765,
            "hub_domain": "example.hubspot.com",
        }

    monkeypatch.setattr(
        olh, "exchange_authorization_code", _fake_exchange_authorization_code
    )
    monkeypatch.setattr(olh, "fetch_userinfo", _fake_fetch_userinfo)
    monkeypatch.setattr(
        "backend.routers.auth._oauth_log_audit_safe",
        lambda *args, **kwargs: None,
    )

    if db._db is not None:
        await db.close()
    await db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            yield {"client": ac, "pool": pg_test_pool}
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


@pytest.mark.asyncio
async def test_hubspot_oauth_authorize_callback_establishes_session(
    _hubspot_oauth_http_client,
):
    env = _hubspot_oauth_http_client
    client = env["client"]

    authorize = await client.get("/api/v1/auth/oauth/hubspot/authorize")

    assert authorize.status_code == 302
    assert olh.FLOW_COOKIE_NAME in client.cookies
    location = authorize.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "app.hubspot.com"
    assert parsed.path == "/oauth/authorize"
    assert query["client_id"] == ["hubspot-client-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/hubspot/callback"
    ]
    assert query["scope"] == ["oauth crm.objects.contacts.read"]
    state = query["state"][0]

    callback = await client.get(
        "/api/v1/auth/oauth/hubspot/callback",
        params={"code": "hubspot-auth-code", "state": state},
        headers={
            "cf-connecting-ip": "203.0.113.14",
            "user-agent": "pytest-hubspot-oauth",
        },
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    assert olh.FLOW_COOKIE_NAME not in client.cookies
    assert "omnisight_session" in client.cookies
    assert "omnisight_csrf" in client.cookies

    async with env["pool"].acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, name, role, oidc_provider, oidc_subject, "
            "auth_methods FROM users WHERE email = $1",
            "eve.hubspot@example.com",
        )
        assert user is not None
        session = await conn.fetchrow(
            "SELECT user_id FROM sessions WHERE user_id = $1",
            user["id"],
        )
        assert session is not None

    auth_methods = user["auth_methods"]
    if isinstance(auth_methods, str):
        auth_methods = json.loads(auth_methods)
    assert user["name"] == "Eve.HubSpot@Example.COM"
    assert user["role"] == "viewer"
    assert user["oidc_provider"] == "hubspot"
    assert user["oidc_subject"] == "123456"
    assert auth_methods == ["oauth_hubspot"]
    assert session["user_id"] == user["id"]


@pytest.fixture()
async def _microsoft_oauth_http_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """PG-backed HTTP fixture for the Microsoft authorize → callback flow.

    Mirrors the GitHub OAuth E2E fixture above: install the shared
    pg_test_pool, pin bootstrap green, set the Microsoft OAuth Settings
    singleton knobs, and mock token/userinfo at the handler boundary.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    monkeypatch.setenv("OMNISIGHT_COOKIE_SECURE", "false")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import bootstrap as _boot
    from backend import config as _cfg
    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    monkeypatch.setattr(
        _cfg.settings, "oauth_microsoft_client_id", "microsoft-client-id"
    )
    monkeypatch.setattr(
        _cfg.settings, "oauth_microsoft_client_secret", "microsoft-client-secret"
    )
    monkeypatch.setattr(
        _cfg.settings, "oauth_redirect_base_url", "https://omnisight.example.com"
    )
    monkeypatch.setattr(
        _cfg.settings,
        "oauth_flow_signing_key",
        "microsoft-oauth-flow-signing-key-2026",
    )

    async def _fake_exchange_authorization_code(**kwargs):
        assert kwargs["client_id"] == "microsoft-client-id"
        assert kwargs["client_secret"] == "microsoft-client-secret"
        assert kwargs["code"] == "microsoft-auth-code"
        assert kwargs["redirect_uri"] == (
            "https://omnisight.example.com/api/v1/auth/oauth/microsoft/callback"
        )
        return oc.TokenSet(
            access_token="microsoft-access-token",
            refresh_token="microsoft-refresh-token",
            token_type="Bearer",
            expires_at=time.time() + 3600,
            scope=("openid", "email", "profile", "offline_access"),
            id_token="header.payload.sig",
            raw={"access_token": "microsoft-access-token"},
        )

    async def _fake_fetch_userinfo(**kwargs):
        assert kwargs["vendor"].provider_id == "microsoft"
        assert kwargs["access_token"] == "microsoft-access-token"
        return {
            "sub": "microsoft-subject-1",
            "preferred_username": "Carol.Microsoft@Example.COM",
            "name": "Carol Microsoft",
        }

    monkeypatch.setattr(
        olh, "exchange_authorization_code", _fake_exchange_authorization_code
    )
    monkeypatch.setattr(olh, "fetch_userinfo", _fake_fetch_userinfo)
    monkeypatch.setattr(
        "backend.routers.auth._oauth_log_audit_safe",
        lambda *args, **kwargs: None,
    )

    if db._db is not None:
        await db.close()
    await db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            yield {"client": ac, "pool": pg_test_pool}
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


@pytest.mark.asyncio
async def test_microsoft_oauth_authorize_callback_establishes_session(
    _microsoft_oauth_http_client,
):
    env = _microsoft_oauth_http_client
    client = env["client"]

    authorize = await client.get("/api/v1/auth/oauth/microsoft/authorize")

    assert authorize.status_code == 302
    assert olh.FLOW_COOKIE_NAME in client.cookies
    location = authorize.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "login.microsoftonline.com"
    assert parsed.path == "/common/oauth2/v2.0/authorize"
    assert query["client_id"] == ["microsoft-client-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/microsoft/callback"
    ]
    assert query["scope"] == ["openid email profile offline_access"]
    state = query["state"][0]

    callback = await client.get(
        "/api/v1/auth/oauth/microsoft/callback",
        params={"code": "microsoft-auth-code", "state": state},
        headers={
            "cf-connecting-ip": "203.0.113.9",
            "user-agent": "pytest-microsoft-oauth",
        },
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    assert olh.FLOW_COOKIE_NAME not in client.cookies
    assert "omnisight_session" in client.cookies
    assert "omnisight_csrf" in client.cookies

    async with env["pool"].acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, name, role, oidc_provider, oidc_subject, "
            "auth_methods FROM users WHERE email = $1",
            "carol.microsoft@example.com",
        )
        assert user is not None
        session = await conn.fetchrow(
            "SELECT user_id FROM sessions WHERE user_id = $1",
            user["id"],
        )
        assert session is not None

    auth_methods = user["auth_methods"]
    if isinstance(auth_methods, str):
        auth_methods = json.loads(auth_methods)
    assert user["name"] == "Carol Microsoft"
    assert user["role"] == "viewer"
    assert user["oidc_provider"] == "microsoft"
    assert user["oidc_subject"] == "microsoft-subject-1"
    assert auth_methods == ["oauth_microsoft"]
    assert session["user_id"] == user["id"]


@pytest.fixture()
async def _apple_oauth_http_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """PG-backed HTTP fixture for the Apple authorize → callback flow.

    Mirrors the Microsoft OAuth E2E fixture above: install the shared
    pg_test_pool, pin bootstrap green, set the Apple OAuth Settings
    singleton knobs, and mock token exchange at the handler boundary.
    Apple has no userinfo endpoint, so identity must come from the
    token response's id_token JWS payload.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    monkeypatch.setenv("OMNISIGHT_COOKIE_SECURE", "false")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import bootstrap as _boot
    from backend import config as _cfg
    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    monkeypatch.setattr(_cfg.settings, "oauth_apple_client_id", "apple-client-id")
    monkeypatch.setattr(
        _cfg.settings, "oauth_apple_client_secret", "apple-client-secret-jwt"
    )
    monkeypatch.setattr(
        _cfg.settings, "oauth_redirect_base_url", "https://omnisight.example.com"
    )
    monkeypatch.setattr(
        _cfg.settings,
        "oauth_flow_signing_key",
        "apple-oauth-flow-signing-key-2026",
    )

    apple_id_token = _make_jws({
        "sub": "apple-subject-1",
        "email": "Dana.Apple@Example.COM",
    })

    async def _fake_exchange_authorization_code(**kwargs):
        assert kwargs["client_id"] == "apple-client-id"
        assert kwargs["client_secret"] == "apple-client-secret-jwt"
        assert kwargs["code"] == "apple-auth-code"
        assert kwargs["redirect_uri"] == (
            "https://omnisight.example.com/api/v1/auth/oauth/apple/callback"
        )
        return oc.TokenSet(
            access_token="apple-access-token",
            refresh_token="apple-refresh-token",
            token_type="Bearer",
            expires_at=time.time() + 3600,
            scope=("name", "email"),
            id_token=apple_id_token,
            raw={"access_token": "apple-access-token"},
        )

    async def _fake_fetch_userinfo(**kwargs):
        raise AssertionError("Apple callback must decode id_token, not fetch userinfo")

    monkeypatch.setattr(
        olh, "exchange_authorization_code", _fake_exchange_authorization_code
    )
    monkeypatch.setattr(olh, "fetch_userinfo", _fake_fetch_userinfo)
    monkeypatch.setattr(
        "backend.routers.auth._oauth_log_audit_safe",
        lambda *args, **kwargs: None,
    )

    if db._db is not None:
        await db.close()
    await db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            yield {"client": ac, "pool": pg_test_pool}
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


@pytest.mark.asyncio
async def test_apple_oauth_authorize_callback_establishes_session(
    _apple_oauth_http_client,
):
    env = _apple_oauth_http_client
    client = env["client"]

    authorize = await client.get("/api/v1/auth/oauth/apple/authorize")

    assert authorize.status_code == 302
    assert olh.FLOW_COOKIE_NAME in client.cookies
    location = authorize.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "appleid.apple.com"
    assert parsed.path == "/auth/authorize"
    assert query["client_id"] == ["apple-client-id"]
    assert query["redirect_uri"] == [
        "https://omnisight.example.com/api/v1/auth/oauth/apple/callback"
    ]
    assert query["scope"] == ["name email"]
    assert query["response_mode"] == ["form_post"]
    state = query["state"][0]

    callback = await client.get(
        "/api/v1/auth/oauth/apple/callback",
        params={"code": "apple-auth-code", "state": state},
        headers={
            "cf-connecting-ip": "203.0.113.10",
            "user-agent": "pytest-apple-oauth",
        },
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    assert olh.FLOW_COOKIE_NAME not in client.cookies
    assert "omnisight_session" in client.cookies
    assert "omnisight_csrf" in client.cookies

    async with env["pool"].acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, name, role, oidc_provider, oidc_subject, "
            "auth_methods FROM users WHERE email = $1",
            "dana.apple@example.com",
        )
        assert user is not None
        session = await conn.fetchrow(
            "SELECT user_id FROM sessions WHERE user_id = $1",
            user["id"],
        )
        assert session is not None

    auth_methods = user["auth_methods"]
    if isinstance(auth_methods, str):
        auth_methods = json.loads(auth_methods)
    assert user["name"] == "dana.apple"
    assert user["role"] == "viewer"
    assert user["oidc_provider"] == "apple"
    assert user["oidc_subject"] == "apple-subject-1"
    assert auth_methods == ["oauth_apple"]
    assert session["user_id"] == user["id"]
    # TODO(AS.1.4): replace the current Apple id_token unverify-decode
    # bridge with JWKS-backed JWS signature verification.
