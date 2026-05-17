from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute
import pytest
from starlette.requests import Request
from starlette.responses import Response

from backend import auth
from backend.routers import auth as auth_router
from backend.routers.auth import LoginRequest, router


def _auth_routes() -> list[APIRoute]:
    return [route for route in router.routes if isinstance(route, APIRoute)]


def test_login_route_has_response_model() -> None:
    login_routes = [
        route
        for route in _auth_routes()
        if route.path == "/auth/login" and route.methods == {"POST"}
    ]

    assert len(login_routes) == 1
    assert login_routes[0].response_model is not None
    assert login_routes[0].response_model_exclude_none is True


def test_auth_login_openapi_schema_renders() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    schema = app.openapi()

    assert "/api/v1/auth/login" in schema["paths"]
    assert "LoginResponse" in schema["components"]["schemas"]


@pytest.mark.asyncio
async def test_login_policy_required_mfa_short_circuits_session(monkeypatch) -> None:
    user = auth.User(
        id="u-policy-mfa",
        email="operator@example.com",
        name="Operator",
        role="operator",
    )
    calls: list[str] = []

    class _Allow:
        def allow(self, _key):
            return True, 0

    async def _noop(*_args, **_kwargs):
        return None

    async def _authenticate_password(_email, _password):
        return user

    async def _account_unlocked(_email):
        return False, 0

    async def _false(_user_id):
        return False

    async def _true(_user_id):
        return True

    async def _empty_methods(_user_id):
        return []

    async def _create_mfa_challenge(_user_id, **_kwargs):
        calls.append("create_mfa_challenge")
        return "mfa-token-policy"

    async def _create_session(*_args, **_kwargs):
        calls.append("create_session")
        raise AssertionError("policy-required MFA must not create a session")

    monkeypatch.setattr(auth_router, "_check_login_rate_limit", lambda _request: None)
    monkeypatch.setattr(auth_router, "ip_limiter", lambda: _Allow())
    monkeypatch.setattr(auth_router, "email_limiter", lambda: _Allow())
    monkeypatch.setattr(auth_router._audit, "log", _noop)
    monkeypatch.setattr(auth, "is_account_locked", _account_unlocked)
    monkeypatch.setattr(auth, "authenticate_password", _authenticate_password)
    monkeypatch.setattr(auth, "create_session", _create_session)

    from backend import mfa
    from backend.security import auth_audit_bridge
    from backend.security import honeypot_form_verifier
    from backend.security import turnstile_form_verifier

    monkeypatch.setattr(honeypot_form_verifier, "verify_form_honeypot_or_reject", _noop)
    monkeypatch.setattr(turnstile_form_verifier, "verify_form_token_or_reject", _noop)
    monkeypatch.setattr(auth_audit_bridge, "emit_login_fail_event", _noop)
    monkeypatch.setattr(mfa, "has_verified_mfa", _false)
    monkeypatch.setattr(mfa, "require_mfa_for_user", _true)
    monkeypatch.setattr(mfa, "create_mfa_challenge", _create_mfa_challenge)
    monkeypatch.setattr(mfa, "get_user_mfa_methods", _empty_methods)

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/login",
        "headers": [(b"user-agent", b"pytest")],
        "client": ("127.0.0.1", 12345),
    })
    response = Response()

    body = await auth_router.login(
        LoginRequest(email=user.email, password="correct-password"),
        request,
        response,
    )

    assert body == {
        "mfa_required": True,
        "mfa_token": "mfa-token-policy",
        "mfa_methods": [],
        "user": {"email": user.email},
    }
    assert calls == ["create_mfa_challenge"]
