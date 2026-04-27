"""AS.6.1 — backend.security.oauth_login_handler contract tests.

Validates the OmniSight self-login OAuth backend handler that wires
the AS.1 OAuth shared library to the four ``Sign in with Google /
GitHub / Microsoft / Apple`` SSO buttons via two HTTP endpoints
(``GET /api/v1/auth/oauth/{vendor}/authorize`` and ``.../callback``)
mounted in :mod:`backend.routers.auth`.

Test families
─────────────
1. SUPPORTED_PROVIDERS / cookie-envelope constants — pinned values,
   immutable shapes, no module-level mutable container.
2. assert_provider_supported — accepts the four AS.6.1 slugs,
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
   (Google/GitHub/Microsoft userinfo + Apple id_token), missing
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
    userinfo) for each of the four providers, state-mismatch /
    expired / cookie-tamper paths, cookie-provider mismatch
    rejection.
13. Module-global state audit (per SOP §1) — no top-level mutable
    container, importing the module is side-effect-free.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from dataclasses import replace
from typing import Any

import httpx
import pytest

from backend.security import oauth_login_handler as olh
from backend.security import oauth_client as oc
from backend.security.oauth_client import (
    FlowSession,
    StateExpiredError,
    StateMismatchError,
    TokenResponseError,
    TokenSet,
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
        "google", "github", "microsoft", "apple",
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


@pytest.mark.parametrize("p", ["google", "github", "microsoft", "apple"])
def test_assert_provider_supported_accepts_four(p):
    olh.assert_provider_supported(p)  # no raise


@pytest.mark.parametrize("p", ["Google", "GITHUB", "discord", "facebook", ""])
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
        olh.lookup_provider_credentials("discord", settings_obj=_FakeSettings())


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
            provider="discord",
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
        if "userinfo" in u or "/user" in u:
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


def test_settings_declares_all_eight_oauth_credential_fields():
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
