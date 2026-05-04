"""AS.1.1 — `backend.security.oauth_client` core lib contract tests.

Exercises every public surface of the OAuth core lib:

    1. PKCE                       verifier length window per RFC 7636 §4.1,
                                  challenge = b64url(sha256(verifier)) per
                                  §4.2, urlsafe-no-pad alphabet, uniqueness
                                  over many calls.
    2. state / nonce              urlsafe-no-pad alphabet, length floor for
                                  ≥256-bit entropy, uniqueness over many
                                  calls.
    3. Authorize URL builder      RFC 6749 §4.1.1 query params, scope
                                  joining, OIDC nonce conditional, vendor
                                  extra_params, collision rejection.
    4. begin_authorization        round-trips redirect URI, scope, extra
                                  metadata; mints distinct PKCE / state /
                                  nonce on each call; respects state TTL.
    5. verify_state_and_consume   constant-time match success, mismatch
                                  raises StateMismatchError, expiry raises
                                  StateExpiredError.
    6. parse_token_response       success shape + error shape (RFC 6749
                                  §5.1 / §5.2), expires_at absolute,
                                  scope splitting, OIDC id_token, raw
                                  preserved.
    7. apply_rotation             refresh_token rotated detection, scope /
                                  token_type / id_token preservation when
                                  the response omits them.
    8. auto_refresh               no-op when fresh, error without
                                  refresh_token, rotation hook fires,
                                  rotated flag correct.
    9. AutoRefreshAuth            integration via httpx.MockTransport,
                                  Authorization header set with the post-
                                  refresh token, sync path raises.
   10. is_enabled                 true by default (forward-promotion
                                  guard for AS.0.9), respects monkey-
                                  patched ``settings.as_enabled = False``.
   11. Module-global state audit  reload yields identical constants /
                                  functions; randomness comes from
                                  :mod:`secrets`; no module-level mutable
                                  state.

The lib is pure — no DB, no asyncio at module level — so most tests are
synchronous and dependency-free. Async tests (auto_refresh + middleware)
use ``asyncio.run`` inline; we don't need pytest-asyncio for two cases.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import re
import time
from typing import Any, Mapping
from unittest.mock import patch

import httpx
import pytest

from backend.security import oauth_client as oc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. PKCE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

URLSAFE_B64 = re.compile(r"^[A-Za-z0-9_-]+$")


def test_pkce_verifier_length_in_rfc_window():
    p = oc.generate_pkce()
    assert oc.PKCE_VERIFIER_MIN_LENGTH <= len(p.code_verifier) <= oc.PKCE_VERIFIER_MAX_LENGTH


def test_pkce_verifier_urlsafe_no_pad():
    p = oc.generate_pkce()
    assert URLSAFE_B64.match(p.code_verifier), p.code_verifier
    assert "=" not in p.code_verifier


def test_pkce_challenge_is_s256_of_verifier():
    p = oc.generate_pkce()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(p.code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert p.code_challenge == expected
    assert p.code_challenge_method == "S256"


def test_pkce_challenge_urlsafe_no_pad():
    p = oc.generate_pkce()
    assert URLSAFE_B64.match(p.code_challenge), p.code_challenge
    assert "=" not in p.code_challenge


def test_pkce_uniqueness_over_many_calls():
    seen = {oc.generate_pkce().code_verifier for _ in range(200)}
    assert len(seen) == 200, "PKCE verifier collisions detected"


def test_pkcepair_is_frozen():
    p = oc.generate_pkce()
    with pytest.raises(Exception):  # FrozenInstanceError
        p.code_verifier = "x"  # type: ignore[misc]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. state / nonce
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_state_urlsafe_no_pad_and_length():
    s = oc.generate_state()
    assert URLSAFE_B64.match(s), s
    # 32 raw bytes -> 43 b64url chars (no pad)
    assert len(s) == 43


def test_nonce_urlsafe_no_pad_and_length():
    n = oc.generate_nonce()
    assert URLSAFE_B64.match(n), n
    assert len(n) == 43


def test_state_uniqueness():
    seen = {oc.generate_state() for _ in range(300)}
    assert len(seen) == 300


def test_nonce_uniqueness():
    seen = {oc.generate_nonce() for _ in range(300)}
    assert len(seen) == 300


def test_state_and_nonce_distinct_pools():
    """Sanity: a state and a nonce drawn back-to-back differ."""
    assert oc.generate_state() != oc.generate_nonce()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. build_authorize_url
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _parse_qs(url: str) -> dict[str, list[str]]:
    from urllib.parse import urlparse, parse_qs

    return parse_qs(urlparse(url).query, keep_blank_values=True)


def test_authorize_url_has_required_params():
    url = oc.build_authorize_url(
        authorize_endpoint="https://provider.example/authorize",
        client_id="cid-123",
        redirect_uri="https://app.example/callback",
        scope=["openid", "email"],
        state="STATE",
        code_challenge="CHALLENGE",
    )
    qs = _parse_qs(url)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["cid-123"]
    assert qs["redirect_uri"] == ["https://app.example/callback"]
    assert qs["scope"] == ["openid email"]  # space-joined
    assert qs["state"] == ["STATE"]
    assert qs["code_challenge"] == ["CHALLENGE"]
    assert qs["code_challenge_method"] == ["S256"]
    assert "nonce" not in qs


def test_authorize_url_includes_nonce_when_provided():
    url = oc.build_authorize_url(
        authorize_endpoint="https://provider.example/authorize",
        client_id="cid",
        redirect_uri="https://app.example/cb",
        scope=["openid"],
        state="S",
        code_challenge="C",
        nonce="N-VALUE",
    )
    assert _parse_qs(url)["nonce"] == ["N-VALUE"]


def test_authorize_url_extra_params_appended():
    url = oc.build_authorize_url(
        authorize_endpoint="https://provider.example/authorize",
        client_id="cid",
        redirect_uri="https://app.example/cb",
        scope=["openid"],
        state="S",
        code_challenge="C",
        extra_params={"access_type": "offline", "prompt": "consent"},
    )
    qs = _parse_qs(url)
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]


def test_authorize_url_extra_params_collision_rejected():
    with pytest.raises(ValueError, match="collides with core OAuth param"):
        oc.build_authorize_url(
            authorize_endpoint="https://p.example/auth",
            client_id="cid",
            redirect_uri="https://app/cb",
            scope=["openid"],
            state="S",
            code_challenge="C",
            extra_params={"state": "ATTACKER-STATE"},
        )


def test_authorize_url_handles_endpoint_with_existing_query():
    url = oc.build_authorize_url(
        authorize_endpoint="https://p.example/auth?xx=1",
        client_id="cid",
        redirect_uri="https://app/cb",
        scope=["openid"],
        state="S",
        code_challenge="C",
    )
    # Existing query preserved; ours appended with `&` not `?`.
    assert "?xx=1&response_type=code" in url


@pytest.mark.parametrize(
    "kwargs",
    [
        {"authorize_endpoint": ""},
        {"client_id": ""},
        {"redirect_uri": ""},
        {"state": ""},
        {"code_challenge": ""},
    ],
)
def test_authorize_url_required_args_blank_raises(kwargs):
    base = dict(
        authorize_endpoint="https://p/auth",
        client_id="cid",
        redirect_uri="https://app/cb",
        scope=["openid"],
        state="S",
        code_challenge="C",
    )
    base.update(kwargs)
    with pytest.raises(ValueError):
        oc.build_authorize_url(**base)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. begin_authorization
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_begin_authorization_round_trips_inputs():
    url, flow = oc.begin_authorization(
        provider="github",
        authorize_endpoint="https://github.com/login/oauth/authorize",
        client_id="cid",
        redirect_uri="https://app/cb",
        scope=["read:user", "user:email"],
        extra={"return_to": "/dashboard"},
    )
    assert flow.provider == "github"
    assert flow.redirect_uri == "https://app/cb"
    assert flow.scope == ("read:user", "user:email")
    assert flow.extra == (("return_to", "/dashboard"),)
    qs = _parse_qs(url)
    assert qs["state"] == [flow.state]
    assert qs["scope"] == ["read:user user:email"]


def test_begin_authorization_omits_nonce_for_non_oidc():
    _, flow = oc.begin_authorization(
        provider="github",
        authorize_endpoint="https://gh/auth",
        client_id="cid",
        redirect_uri="https://app/cb",
        scope=["read:user"],
    )
    assert flow.nonce is None


def test_begin_authorization_includes_nonce_for_oidc():
    url, flow = oc.begin_authorization(
        provider="google",
        authorize_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        client_id="cid",
        redirect_uri="https://app/cb",
        scope=["openid", "email"],
        use_oidc_nonce=True,
    )
    assert flow.nonce is not None
    assert _parse_qs(url)["nonce"] == [flow.nonce]


def test_begin_authorization_distinct_per_call():
    a = oc.begin_authorization(
        provider="x", authorize_endpoint="https://p/a", client_id="c",
        redirect_uri="r", scope=["s"],
    )[1]
    b = oc.begin_authorization(
        provider="x", authorize_endpoint="https://p/a", client_id="c",
        redirect_uri="r", scope=["s"],
    )[1]
    assert a.state != b.state
    assert a.code_verifier != b.code_verifier


def test_begin_authorization_state_ttl_honoured():
    url, flow = oc.begin_authorization(
        provider="x", authorize_endpoint="https://p/a", client_id="c",
        redirect_uri="r", scope=["s"], state_ttl_seconds=42, now=1000.0,
    )
    assert flow.created_at == 1000.0
    assert flow.expires_at == 1042.0


def test_begin_authorization_extra_authorize_params_passthrough():
    url, _ = oc.begin_authorization(
        provider="google", authorize_endpoint="https://g/a", client_id="c",
        redirect_uri="r", scope=["openid"],
        extra_authorize_params={"access_type": "offline"},
    )
    assert _parse_qs(url)["access_type"] == ["offline"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. verify_state_and_consume
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_flow(*, state="S", expires_at=None, now=1000.0):
    return oc.FlowSession(
        provider="x",
        state=state,
        code_verifier="V",
        nonce=None,
        redirect_uri="https://app/cb",
        scope=("openid",),
        created_at=now,
        expires_at=now + 600 if expires_at is None else expires_at,
    )


def test_verify_state_success():
    flow = _make_flow()
    # Should not raise
    oc.verify_state_and_consume(flow, "S", now=flow.created_at + 1)


def test_verify_state_mismatch_raises():
    flow = _make_flow()
    with pytest.raises(oc.StateMismatchError):
        oc.verify_state_and_consume(flow, "wrong", now=flow.created_at + 1)


def test_verify_state_blank_returned_raises():
    flow = _make_flow()
    with pytest.raises(oc.StateMismatchError):
        oc.verify_state_and_consume(flow, "", now=flow.created_at + 1)


def test_verify_state_expired_raises():
    flow = _make_flow(now=1000.0)  # expires at 1600
    with pytest.raises(oc.StateExpiredError):
        oc.verify_state_and_consume(flow, "S", now=2000.0)


def test_verify_state_at_exact_expiry_raises():
    flow = _make_flow(now=1000.0)
    with pytest.raises(oc.StateExpiredError):
        oc.verify_state_and_consume(flow, "S", now=flow.expires_at)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. parse_token_response
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_parse_minimum_success_payload():
    t = oc.parse_token_response(
        {"access_token": "AT", "token_type": "Bearer"}, now=1000.0
    )
    assert t.access_token == "AT"
    assert t.token_type == "Bearer"
    assert t.refresh_token is None
    assert t.expires_at is None
    assert t.scope == ()
    assert t.id_token is None


def test_parse_full_success_payload():
    payload = {
        "access_token": "AT",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "RT",
        "scope": "openid email profile",
        "id_token": "eyJ.JWT.SIG",
    }
    t = oc.parse_token_response(payload, now=1000.0)
    assert t.expires_at == 4600.0
    assert t.scope == ("openid", "email", "profile")
    assert t.id_token == "eyJ.JWT.SIG"
    assert t.refresh_token == "RT"
    # Raw preserved
    assert t.raw["scope"] == "openid email profile"


def test_parse_scope_comma_separated_supported():
    """Some providers (Slack, Discord) use commas; we accept both."""
    t = oc.parse_token_response(
        {"access_token": "AT", "scope": "channels:read,users:read"},
        now=1000.0,
    )
    assert t.scope == ("channels:read", "users:read")


def test_parse_scope_list_supported():
    t = oc.parse_token_response(
        {"access_token": "AT", "scope": ["a", "b"]}, now=1000.0
    )
    assert t.scope == ("a", "b")


def test_parse_scope_dedupes_in_order():
    t = oc.parse_token_response(
        {"access_token": "AT", "scope": "a b a c"}, now=1000.0
    )
    assert t.scope == ("a", "b", "c")


def test_parse_default_token_type_bearer():
    t = oc.parse_token_response({"access_token": "AT"}, now=1000.0)
    assert t.token_type == "Bearer"


def test_parse_error_payload_raises():
    with pytest.raises(oc.TokenResponseError, match="invalid_grant"):
        oc.parse_token_response(
            {"error": "invalid_grant", "error_description": "code reused"}
        )


def test_parse_missing_access_token_raises():
    with pytest.raises(oc.TokenResponseError, match="missing access_token"):
        oc.parse_token_response({"token_type": "Bearer"})


def test_parse_negative_expires_in_raises():
    with pytest.raises(oc.TokenResponseError, match="negative"):
        oc.parse_token_response({"access_token": "AT", "expires_in": -1})


def test_parse_non_numeric_expires_in_raises():
    with pytest.raises(oc.TokenResponseError, match="not a number"):
        oc.parse_token_response({"access_token": "AT", "expires_in": "soon"})


def test_parse_non_string_id_token_raises():
    with pytest.raises(oc.TokenResponseError, match="id_token"):
        oc.parse_token_response({"access_token": "AT", "id_token": 123})


def test_parse_non_string_refresh_token_raises():
    with pytest.raises(oc.TokenResponseError, match="refresh_token"):
        oc.parse_token_response({"access_token": "AT", "refresh_token": 42})


def test_parse_non_mapping_payload_raises():
    with pytest.raises(oc.TokenResponseError, match="must be a mapping"):
        oc.parse_token_response("not-a-dict")  # type: ignore[arg-type]


def test_token_needs_refresh_within_skew():
    t = oc.TokenSet(
        access_token="AT", refresh_token="RT", token_type="Bearer",
        expires_at=1000.0, scope=(), id_token=None, raw={},
    )
    assert t.needs_refresh(skew_seconds=60, now=950.0) is True
    assert t.needs_refresh(skew_seconds=60, now=900.0) is False


def test_token_no_expiry_never_needs_refresh():
    t = oc.TokenSet(
        access_token="AT", refresh_token="RT", token_type="Bearer",
        expires_at=None, scope=(), id_token=None, raw={},
    )
    assert t.needs_refresh(now=time.time() + 1e9) is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. apply_rotation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _t(**kw):
    base = dict(
        access_token="AT-old", refresh_token="RT-old", token_type="Bearer",
        expires_at=1000.0, scope=("openid",), id_token=None, raw={},
    )
    base.update(kw)
    return oc.TokenSet(**base)  # type: ignore[arg-type]


def test_rotation_detected_when_refresh_token_changes():
    prev = _t(refresh_token="RT-1")
    new, rotated = oc.apply_rotation(
        prev,
        {"access_token": "AT-new", "refresh_token": "RT-2", "expires_in": 3600},
        now=2000.0,
    )
    assert rotated is True
    assert new.refresh_token == "RT-2"
    assert new.access_token == "AT-new"
    assert new.expires_at == 5600.0


def test_rotation_not_detected_when_refresh_token_omitted():
    """Some providers don't rotate; old refresh_token preserved."""
    prev = _t(refresh_token="RT-keep")
    new, rotated = oc.apply_rotation(
        prev,
        {"access_token": "AT-new", "expires_in": 3600},
        now=2000.0,
    )
    assert rotated is False
    assert new.refresh_token == "RT-keep"


def test_rotation_not_detected_when_provider_returns_same_refresh():
    prev = _t(refresh_token="RT-same")
    new, rotated = oc.apply_rotation(
        prev,
        {"access_token": "AT-new", "refresh_token": "RT-same", "expires_in": 3600},
    )
    assert rotated is False
    assert new.refresh_token == "RT-same"


def test_rotation_keeps_scope_when_response_omits_it():
    prev = _t(scope=("openid", "email"))
    new, _ = oc.apply_rotation(
        prev, {"access_token": "AT-new", "expires_in": 60}, now=1000.0,
    )
    assert new.scope == ("openid", "email")


def test_rotation_keeps_id_token_when_response_omits_it():
    prev = _t(id_token="eyJ.OLD")
    new, _ = oc.apply_rotation(
        prev, {"access_token": "AT-new"}, now=1000.0,
    )
    assert new.id_token == "eyJ.OLD"


def test_rotation_overrides_id_token_when_response_has_it():
    prev = _t(id_token="eyJ.OLD")
    new, _ = oc.apply_rotation(
        prev, {"access_token": "AT-new", "id_token": "eyJ.NEW"},
    )
    assert new.id_token == "eyJ.NEW"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. auto_refresh
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_auto_refresh_noop_when_fresh():
    fresh = _t(expires_at=time.time() + 3600)
    called = []

    async def refresh_fn(rt: str) -> Mapping[str, Any]:
        called.append(rt)
        return {"access_token": "should-not-happen"}

    out = asyncio.run(oc.auto_refresh(fresh, refresh_fn))
    assert out is fresh
    assert called == []


def test_auto_refresh_raises_without_refresh_token():
    stale = _t(refresh_token=None, expires_at=time.time() - 5)

    async def refresh_fn(rt: str) -> Mapping[str, Any]:  # pragma: no cover - never reached
        return {}

    with pytest.raises(oc.TokenRefreshError, match="re-authenticate"):
        asyncio.run(oc.auto_refresh(stale, refresh_fn))


def test_auto_refresh_invokes_callback_on_rotation():
    stale = _t(refresh_token="RT-1", expires_at=time.time() - 1)
    rotation_log: list[tuple[str, str, bool]] = []

    async def refresh_fn(rt: str) -> Mapping[str, Any]:
        return {
            "access_token": "AT-new",
            "refresh_token": "RT-2",
            "expires_in": 3600,
        }

    async def on_rotated(old, new, rotated):
        rotation_log.append((old.refresh_token, new.refresh_token, rotated))

    out = asyncio.run(oc.auto_refresh(stale, refresh_fn, on_rotated=on_rotated))
    assert out.access_token == "AT-new"
    assert out.refresh_token == "RT-2"
    assert rotation_log == [("RT-1", "RT-2", True)]


def test_auto_refresh_callback_fires_even_when_refresh_token_unchanged():
    """Caller still wants the new access_token persisted."""
    stale = _t(refresh_token="RT-keep", expires_at=time.time() - 1)
    rotation_log: list[tuple[bool]] = []

    async def refresh_fn(rt: str) -> Mapping[str, Any]:
        return {"access_token": "AT-new", "expires_in": 3600}

    async def on_rotated(old, new, rotated):
        rotation_log.append((rotated,))

    asyncio.run(oc.auto_refresh(stale, refresh_fn, on_rotated=on_rotated))
    assert rotation_log == [(False,)]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. AutoRefreshAuth (httpx middleware)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_auto_refresh_auth_sets_authorization_header_with_fresh_token():
    stale = _t(refresh_token="RT-1", expires_at=time.time() - 5)

    async def refresh_fn(rt: str) -> Mapping[str, Any]:
        return {
            "access_token": "AT-fresh",
            "refresh_token": "RT-2",
            "expires_in": 3600,
        }

    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["authorization"] = req.headers.get("authorization", "")
        return httpx.Response(200, json={"ok": True})

    auth = oc.AutoRefreshAuth(stale, refresh_fn)
    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport, auth=auth) as client:
            r = await client.get("https://api.example/me")
        return r

    r = asyncio.run(run())
    assert r.status_code == 200
    assert captured["authorization"] == "Bearer AT-fresh"
    # Auth instance state mutated for subsequent requests in same client.
    assert auth.token.access_token == "AT-fresh"
    assert auth.token.refresh_token == "RT-2"


def test_auto_refresh_auth_uses_token_type_verbatim():
    """Some providers (rare) use lowercase 'bearer' or DPoP — preserve."""
    fresh = _t(token_type="bearer", expires_at=time.time() + 3600)

    async def refresh_fn(rt):  # pragma: no cover - never reached when fresh
        return {}

    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["authorization"] = req.headers.get("authorization", "")
        return httpx.Response(204)

    auth = oc.AutoRefreshAuth(fresh, refresh_fn)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth) as cli:
            await cli.get("https://api.example/me")
    asyncio.run(run())
    assert captured["authorization"] == "bearer AT-old"


def test_auto_refresh_auth_rejects_non_token_set():
    async def refresh_fn(rt):  # pragma: no cover - never reached
        return {}

    with pytest.raises(TypeError, match="TokenSet"):
        oc.AutoRefreshAuth("not-a-token", refresh_fn)  # type: ignore[arg-type]


def test_auto_refresh_auth_sync_path_raises():
    """Sync httpx clients can't await the refresh callback — bail loudly."""
    fresh = _t(expires_at=time.time() + 3600)

    async def refresh_fn(rt):  # pragma: no cover - never reached
        return {}

    auth = oc.AutoRefreshAuth(fresh, refresh_fn)

    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler), auth=auth) as cli:
        with pytest.raises(RuntimeError, match="AsyncClient"):
            cli.get("https://api.example/me")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 10. is_enabled (AS.0.8 single-knob hook)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_is_enabled_default_true_when_settings_missing_field():
    """AS.0.9 §7.2.6 forward-promotion: lib defaults to True until
    AS.3.1 lands ``Settings.as_enabled``. Either the attribute is
    absent (fallback True) or it's present as a bool — verify True
    in both cases."""
    from backend.config import settings

    actual = oc.is_enabled()
    expected = bool(getattr(settings, "as_enabled", True))
    assert actual is expected


def test_is_enabled_respects_false_when_field_present():
    from backend.config import settings

    if not hasattr(settings, "as_enabled"):
        pytest.skip(
            "Settings.as_enabled not yet declared — AS.3.1 will land it; "
            "this assertion auto-promotes when the field appears."
        )
    with patch.object(settings, "as_enabled", False):
        assert oc.is_enabled() is False


def test_is_enabled_does_not_block_pure_helpers():
    """Per AS.0.8 §3.1 noop matrix entry for AS.1: the lib's pure
    helpers MUST stay callable even with the knob off — only the
    HTTP endpoint surfaces 503. (A backfill script that needs to
    parse a stored token must not be locked out of the parser.)"""
    # Simulate knob off (regardless of whether the field exists yet).
    from backend.config import settings

    if hasattr(settings, "as_enabled"):
        with patch.object(settings, "as_enabled", False):
            _ = oc.generate_pkce()
            _ = oc.generate_state()
            _ = oc.parse_token_response({"access_token": "AT"})
    else:
        _ = oc.generate_pkce()
        _ = oc.generate_state()
        _ = oc.parse_token_response({"access_token": "AT"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 11. Module-global state audit (per SOP §1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_module_constants_stable_across_reload():
    """SOP §1 module-global audit answer #1: every uvicorn worker
    reads the same constants because they're literals in the module.
    Reload sanity-checks we haven't accidentally introduced module-
    level mutable state (e.g. a dict that grows on import)."""
    constants_before = (
        oc.PKCE_VERIFIER_MIN_LENGTH,
        oc.PKCE_VERIFIER_MAX_LENGTH,
        oc.DEFAULT_STATE_TTL_SECONDS,
        oc.DEFAULT_REFRESH_SKEW_SECONDS,
        oc.ALL_OAUTH_EVENTS,
    )
    importlib.reload(oc)
    constants_after = (
        oc.PKCE_VERIFIER_MIN_LENGTH,
        oc.PKCE_VERIFIER_MAX_LENGTH,
        oc.DEFAULT_STATE_TTL_SECONDS,
        oc.DEFAULT_REFRESH_SKEW_SECONDS,
        oc.ALL_OAUTH_EVENTS,
    )
    assert constants_before == constants_after


def test_canonical_event_strings_match_design_freeze():
    """AS.0.8 §5 audit-behaviour matrix references these exact strings.
    Pin them — any drift breaks the cross-reference between AS.5.1
    ``EVENT_OAUTH_*`` (when it lands) and AS.0.8 §5 truth table."""
    assert oc.EVENT_OAUTH_LOGIN_INIT == "oauth.login_init"
    assert oc.EVENT_OAUTH_LOGIN_CALLBACK == "oauth.login_callback"
    assert oc.EVENT_OAUTH_REFRESH == "oauth.refresh"
    assert oc.EVENT_OAUTH_UNLINK == "oauth.unlink"
    assert oc.EVENT_OAUTH_TOKEN_ROTATED == "oauth.token_rotated"
    # Tuple is the union of all five.
    assert set(oc.ALL_OAUTH_EVENTS) == {
        "oauth.login_init",
        "oauth.login_callback",
        "oauth.refresh",
        "oauth.unlink",
        "oauth.token_rotated",
    }


def test_randomness_source_is_secrets_module():
    """Provenance grep guard — guarantees we don't accidentally fall
    back to ``random.random()`` (predictable) in a future patch.
    Mirrors the AS.0.10 password-generator drift-guard."""
    import pathlib

    src = pathlib.Path(oc.__file__).read_text(encoding="utf-8")
    assert "import secrets" in src
    # The non-secrets random module should NOT be imported.
    assert "import random" not in src
    assert "from random " not in src


def test_no_module_level_mutable_state():
    """Walks the module dict, fails on any list / dict / set / bytearray.

    The AS.0.10 audit established this pattern; reapplied here.
    Acceptable: ``__loader__`` / ``__spec__`` etc. (Python machinery),
    typing aliases, frozensets, tuples, frozen dataclasses.
    """

    forbidden_types = (list, dict, set, bytearray)
    # The Python module itself has __dict__ etc., so we ignore dunders.
    offenders = []
    for name, val in vars(oc).items():
        if name.startswith("__") and name.endswith("__"):
            continue
        if isinstance(val, forbidden_types):
            offenders.append((name, type(val).__name__))
    assert offenders == [], (
        f"module-level mutable containers detected: {offenders}"
    )


def test_public_surface_matches_all():
    """Every name in __all__ is importable and the converse — no leaked
    privates."""
    for name in oc.__all__:
        assert hasattr(oc, name), f"__all__ promises {name!r} but it's absent"
    # Symbols starting with uppercase or our known lowercase helpers are
    # public; everything else with a leading underscore is private.
    public_names = {n for n in dir(oc) if not n.startswith("_")}
    # `httpx`, the module-level imports, are visible but not in __all__ —
    # filter them out to keep the assertion focused on our own symbols.
    not_ours = {
        "annotations", "base64", "hashlib", "hmac", "secrets", "time",
        "urllib", "dataclass", "Any", "Awaitable", "Callable", "Mapping",
        "Optional", "Sequence", "httpx",
    }
    promised = set(oc.__all__)
    leaked = (public_names - promised) - not_ours
    assert not leaked, f"public names not in __all__: {sorted(leaked)}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 12. Cross-twin drift guard — Python ↔ TS (AS.1.2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# AS.1.2 lands the TS twin at templates/_shared/oauth-client/index.ts.
# The two sides must agree on:
#   * the 5 canonical OAuth audit event strings (AS.0.8 §5 truth-table)
#   * the 4 numeric defaults that gate behaviour (PKCE bounds, state
#     TTL, refresh skew)
# These tests parse the literal values out of the TS source and assert
# byte-/value-identity with the Python side. Same pattern as the
# AS.0.10 cross-twin drift guard in test_password_generator.py.

import pathlib  # noqa: E402  — keep family-local imports near the family

_TS_TWIN_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "templates"
    / "_shared"
    / "oauth-client"
    / "index.ts"
)


def _read_ts_twin() -> str:
    if not _TS_TWIN_PATH.exists():
        pytest.skip(f"TS twin not present at {_TS_TWIN_PATH}")
    return _TS_TWIN_PATH.read_text(encoding="utf-8")


def _extract_ts_string_const(src: str, name: str) -> str:
    """Pull `export const NAME = "..."` literal value out of TS source."""
    m = re.search(
        rf'export\s+const\s+{name}\s*=\s*"((?:[^"\\]|\\.)*)"',
        src,
    )
    assert m, f"could not find `export const {name} = \"...\"` in TS twin"
    return m.group(1)


def _extract_ts_number_const(src: str, name: str) -> int:
    """Pull `export const NAME = <int>` literal value out of TS source."""
    m = re.search(
        rf'export\s+const\s+{name}\s*=\s*(-?\d+)\b',
        src,
    )
    assert m, f"could not find `export const {name} = <int>` in TS twin"
    return int(m.group(1))


def test_oauth_event_strings_parity_python_ts():
    """SHA-256 of the 5 canonical event strings (joined in declaration
    order) must match between sides. Any rename, reorder, addition, or
    removal on either side breaks this oracle."""
    ts_src = _read_ts_twin()
    ts_events = [
        _extract_ts_string_const(ts_src, "EVENT_OAUTH_LOGIN_INIT"),
        _extract_ts_string_const(ts_src, "EVENT_OAUTH_LOGIN_CALLBACK"),
        _extract_ts_string_const(ts_src, "EVENT_OAUTH_REFRESH"),
        _extract_ts_string_const(ts_src, "EVENT_OAUTH_UNLINK"),
        _extract_ts_string_const(ts_src, "EVENT_OAUTH_TOKEN_ROTATED"),
    ]
    py_events = [
        oc.EVENT_OAUTH_LOGIN_INIT,
        oc.EVENT_OAUTH_LOGIN_CALLBACK,
        oc.EVENT_OAUTH_REFRESH,
        oc.EVENT_OAUTH_UNLINK,
        oc.EVENT_OAUTH_TOKEN_ROTATED,
    ]
    py_hash = hashlib.sha256("\n".join(py_events).encode("utf-8")).hexdigest()
    ts_hash = hashlib.sha256("\n".join(ts_events).encode("utf-8")).hexdigest()
    assert py_hash == ts_hash, (
        f"OAuth audit event-string drift between Python and TS twin\n"
        f"  Python: {py_events}\n"
        f"  TS    : {ts_events}\n"
        f"  Python SHA-256: {py_hash}\n"
        f"  TS     SHA-256: {ts_hash}"
    )
    # Also confirm the per-string parity for nicer error triage.
    for py_str, ts_str in zip(py_events, ts_events, strict=True):
        assert py_str == ts_str


def test_oauth_defaults_parity_python_ts_pkce_min_length():
    ts_src = _read_ts_twin()
    assert _extract_ts_number_const(ts_src, "PKCE_VERIFIER_MIN_LENGTH") == oc.PKCE_VERIFIER_MIN_LENGTH


def test_oauth_defaults_parity_python_ts_pkce_max_length():
    ts_src = _read_ts_twin()
    assert _extract_ts_number_const(ts_src, "PKCE_VERIFIER_MAX_LENGTH") == oc.PKCE_VERIFIER_MAX_LENGTH


def test_oauth_defaults_parity_python_ts_state_ttl():
    ts_src = _read_ts_twin()
    assert _extract_ts_number_const(ts_src, "DEFAULT_STATE_TTL_SECONDS") == oc.DEFAULT_STATE_TTL_SECONDS


def test_oauth_defaults_parity_python_ts_refresh_skew():
    ts_src = _read_ts_twin()
    assert _extract_ts_number_const(ts_src, "DEFAULT_REFRESH_SKEW_SECONDS") == oc.DEFAULT_REFRESH_SKEW_SECONDS


def test_ts_twin_uses_web_crypto_not_math_random():
    """TS-side RNG-provenance pin (mirrors the Python side's
    `import secrets` grep). Web Crypto via `getRandomValues` is the
    only acceptable source; `Math.random()` is predictable and would
    silently weaken state / nonce / PKCE entropy."""
    ts_src = _read_ts_twin()
    assert "getRandomValues" in ts_src
    assert "Math.random" not in ts_src


def test_ts_twin_declares_all_five_event_strings():
    """Sanity: the TS source actually contains all five `export const
    EVENT_OAUTH_*` declarations. Catches a partial port that leaves
    some strings hard-coded inline (which would silently bypass the
    cross-twin drift guard)."""
    ts_src = _read_ts_twin()
    for name in (
        "EVENT_OAUTH_LOGIN_INIT",
        "EVENT_OAUTH_LOGIN_CALLBACK",
        "EVENT_OAUTH_REFRESH",
        "EVENT_OAUTH_UNLINK",
        "EVENT_OAUTH_TOKEN_ROTATED",
    ):
        assert re.search(rf'export\s+const\s+{name}\s*=', ts_src), (
            f"TS twin missing `export const {name}`"
        )
