"""FX2.D9.7.15 — ``oauth_login_*`` audit event family tests.

Mirrors the small emitter contract used by ``backend.security.auth_event``
and ``oauth_audit``: pure builders pin row shape, async emitters route
into ``backend.audit.log``, and the OAuth router schedules the new
family alongside the existing forensic/rollup rows.

Module-global state audit (SOP Step 1):
Test data is local to each test.  The module under test owns no mutable
module-level state; every audit append routes through ``backend.audit``.
"""

from __future__ import annotations

import asyncio
import importlib
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Response

from backend.routers import auth as auth_router
from backend.security import oauth_login_audit as ola
from backend.security import oauth_login_handler as olh
from backend.security.oauth_client import FlowSession


def _run(coro):
    return asyncio.run(coro)


def test_event_names_exact() -> None:
    assert ola.ALL_OAUTH_LOGIN_EVENTS == (
        "oauth_login_initiated",
        "oauth_login_success",
        "oauth_login_failed_provider_not_configured",
        "oauth_login_failed_callback_invalid",
    )


def test_initiated_payload_shape() -> None:
    p = ola.build_initiated_payload(
        ola.OAuthLoginInitiatedContext(
            provider="github",
            state="state-123",
            redirect_uri="https://app.example/api/v1/auth/oauth/github/callback",
            scope=("read:user", "user:email"),
        )
    )

    assert p.action == "oauth_login_initiated"
    assert p.entity_kind == "oauth_login"
    assert p.entity_id == f"github:{ola.fingerprint('state-123')}"
    assert p.before is None
    assert p.after == {
        "provider": "github",
        "state_fp": ola.fingerprint("state-123"),
        "redirect_uri": "https://app.example/api/v1/auth/oauth/github/callback",
        "scope": ["read:user", "user:email"],
    }
    assert p.actor == "anonymous"


def test_success_payload_shape() -> None:
    p = ola.build_success_payload(
        ola.OAuthLoginSuccessContext(
            provider="google", user_id="u-1", state="state-xyz",
        )
    )

    assert p.action == "oauth_login_success"
    assert p.entity_id == f"google:{ola.fingerprint('state-xyz')}"
    assert p.after == {
        "provider": "google",
        "user_id": "u-1",
        "state_fp": ola.fingerprint("state-xyz"),
    }
    assert p.actor == "u-1"


def test_failure_payload_maps_provider_not_configured() -> None:
    p = ola.build_failure_payload(
        ola.OAuthLoginFailureContext(
            provider="slack",
            reason="provider_not_configured",
            detail="set OMNISIGHT_OAUTH_SLACK_CLIENT_ID",
        )
    )

    assert p.action == "oauth_login_failed_provider_not_configured"
    assert p.entity_id == "slack:unknown"
    assert p.after["reason"] == "provider_not_configured"
    assert p.after["detail"] == "set OMNISIGHT_OAUTH_SLACK_CLIENT_ID"


def test_failure_payload_maps_callback_invalid() -> None:
    p = ola.build_failure_payload(
        ola.OAuthLoginFailureContext(
            provider="github",
            reason="callback_invalid",
            state="bad-state",
            detail="state mismatch",
        )
    )

    assert p.action == "oauth_login_failed_callback_invalid"
    assert p.entity_id == f"github:{ola.fingerprint('bad-state')}"
    assert p.after["state_fp"] == ola.fingerprint("bad-state")
    assert "bad-state" not in str(p.after)


def test_failure_payload_rejects_unknown_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        ola.build_failure_payload(
            ola.OAuthLoginFailureContext(provider="github", reason="typo")
        )


def test_emit_routes_to_audit_log(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 42

    monkeypatch.setattr("backend.security.oauth_login_audit.audit.log", fake_log)
    rid = _run(ola.emit_success(
        ola.OAuthLoginSuccessContext(
            provider="github", user_id="u-7", state="state-7",
        )
    ))

    assert rid == 42
    assert captured["action"] == "oauth_login_success"
    assert captured["entity_kind"] == "oauth_login"
    assert captured["actor"] == "u-7"


def test_module_constants_stable_across_reload() -> None:
    snapshot = ola.ALL_OAUTH_LOGIN_EVENTS
    importlib.reload(ola)
    assert ola.ALL_OAUTH_LOGIN_EVENTS == snapshot


def test_module_uses_hashlib_not_secrets() -> None:
    src = pathlib.Path(ola.__file__).read_text(encoding="utf-8")
    assert "hashlib" in src
    assert "import secrets" not in src
    assert "from secrets" not in src


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        headers={"host": "app.example"},
        cookies={},
        url=SimpleNamespace(scheme="https", netloc="app.example"),
        client=SimpleNamespace(host="203.0.113.9"),
    )


def _flow(provider: str = "github") -> FlowSession:
    return FlowSession(
        provider=provider,
        state="state-router",
        code_verifier="v" * 50,
        nonce=None,
        redirect_uri=f"https://app.example/api/v1/auth/oauth/{provider}/callback",
        scope=("read:user",),
        created_at=1000.0,
        expires_at=1600.0,
    )


@pytest.mark.asyncio
async def test_router_authorize_emits_oauth_login_initiated(monkeypatch) -> None:
    emitted: list[ola.OAuthLoginInitiatedContext] = []

    def fake_safe(factory, *, label: str) -> None:
        if label == "oauth_login_initiated":
            factory()

    def fake_emit(ctx: ola.OAuthLoginInitiatedContext):
        emitted.append(ctx)
        return None

    monkeypatch.setattr(auth_router, "_oauth_log_audit_safe", fake_safe)
    monkeypatch.setattr(ola, "emit_initiated", fake_emit)
    monkeypatch.setattr(
        olh,
        "begin_oauth_login",
        lambda **_kwargs: SimpleNamespace(
            authorize_url="https://github.example/auth",
            flow_cookie="signed-flow",
            flow=_flow("github"),
        ),
    )

    resp = await auth_router.oauth_authorize(
        provider="github",
        request=_request(),
        response=Response(),
    )

    assert resp.status_code == 302
    assert [ctx.provider for ctx in emitted] == ["github"]
    assert emitted[0].state == "state-router"


@pytest.mark.asyncio
async def test_router_authorize_emits_provider_not_configured(
    monkeypatch,
) -> None:
    emitted: list[ola.OAuthLoginFailureContext] = []

    def fake_safe(factory, *, label: str) -> None:
        if label == "oauth_login_provider_not_configured":
            factory()

    def fake_emit(ctx: ola.OAuthLoginFailureContext):
        emitted.append(ctx)
        return None

    def raise_unconfigured(**_kwargs):
        raise olh.ProviderNotConfiguredError("missing discord client secret")

    monkeypatch.setattr(auth_router, "_oauth_log_audit_safe", fake_safe)
    monkeypatch.setattr(ola, "emit_failure", fake_emit)
    monkeypatch.setattr(olh, "begin_oauth_login", raise_unconfigured)

    with pytest.raises(HTTPException) as excinfo:
        await auth_router.oauth_authorize(
            provider="discord",
            request=_request(),
            response=Response(),
        )

    assert excinfo.value.status_code == 501
    assert len(emitted) == 1
    assert emitted[0].provider == "discord"
    assert emitted[0].reason == "provider_not_configured"


@pytest.mark.asyncio
async def test_router_callback_missing_code_emits_callback_invalid(
    monkeypatch,
) -> None:
    emitted: list[ola.OAuthLoginFailureContext] = []

    def fake_safe(factory, *, label: str) -> None:
        if label == "oauth_login_callback_invalid":
            factory()

    def fake_emit(ctx: ola.OAuthLoginFailureContext):
        emitted.append(ctx)
        return None

    monkeypatch.setattr(auth_router, "_oauth_log_audit_safe", fake_safe)
    monkeypatch.setattr(ola, "emit_failure", fake_emit)

    with pytest.raises(HTTPException) as excinfo:
        await auth_router.oauth_callback(
            provider="github",
            request=_request(),
            response=Response(),
            code=None,
            state="state-router",
        )

    assert excinfo.value.status_code == 400
    assert len(emitted) == 1
    assert emitted[0].provider == "github"
    assert emitted[0].reason == "callback_invalid"
    assert emitted[0].state == "state-router"
