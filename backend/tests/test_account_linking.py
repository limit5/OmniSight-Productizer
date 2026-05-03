"""FX.5.1 — OAuth callback account-linking state machine tests.

This file complements ``test_account_linking_helper.py`` and
``test_oauth_login_handler.py`` by exercising the router-level OAuth
callback branches that compose both modules:

* existing ``oidc_provider`` + ``oidc_subject`` logs in directly;
* same email with a password method refuses silent OAuth takeover;
* OAuth-only same-email user gains the new provider method;
* brand-new OAuth identity creates a credential-less user and appends
  the OAuth method.

Module-global state audit (SOP Step 1):
Pure test-only fakes; no module-level mutable state is introduced.
The production callback state lives in the signed OAuth cookie and
user rows in PG, so cross-worker consistency is through the shared DB
and deterministic signing-key derivation.

Read-after-write timing audit (SOP Step 1):
These tests run one callback at a time against an in-memory fake of
the callback's single acquired connection.  They do not relax or
change any downstream read-after-write timing expectation.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import HTTPException, Response

from backend import auth
from backend.routers import auth as auth_router
from backend.security import oauth_login_handler as olh
from backend.security.oauth_client import FlowSession, TokenSet


class _FakeRow(dict):
    """Minimal ``asyncpg.Record``-compatible stand-in."""


def _user_row(
    *,
    user_id: str,
    email: str,
    name: str = "Test User",
    role: str = "viewer",
    oidc_provider: str = "",
    oidc_subject: str = "",
    auth_methods: list[str] | None = None,
) -> _FakeRow:
    return _FakeRow(
        id=user_id,
        email=email,
        name=name,
        role=role,
        enabled=True,
        must_change_password=False,
        tenant_id="t-default",
        oidc_provider=oidc_provider,
        oidc_subject=oidc_subject,
        auth_methods=json.dumps(auth_methods or [], separators=(",", ":")),
    )


class _FakeConn:
    """Small fake for the SQL used by ``oauth_callback``."""

    def __init__(self, users: dict[str, _FakeRow]) -> None:
        self.users = users
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRow | None:
        self.fetchrow_calls.append((sql, args))
        if "WHERE oidc_provider = $1 AND oidc_subject = $2" in sql:
            provider, subject = args
            for row in self.users.values():
                if (
                    row["oidc_provider"] == provider
                    and row["oidc_subject"] == subject
                ):
                    return row
            return None
        if "FROM users WHERE email = $1" in sql:
            email = str(args[0]).lower().strip()
            for row in self.users.values():
                if row["email"] == email:
                    return row
            return None
        if "SELECT auth_methods FROM users WHERE id = $1" in sql:
            return self.users.get(args[0])
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql: str, *args: Any) -> None:
        self.execute_calls.append((sql, args))
        if "UPDATE users SET auth_methods" in sql:
            payload, user_id = args
            self.users[user_id]["auth_methods"] = payload
            return
        if "INSERT INTO users" in sql:
            (
                user_id,
                email,
                name,
                role,
                _password_hash,
                oidc_provider,
                oidc_subject,
                tenant_id,
                auth_methods,
            ) = args
            self.users[user_id] = _FakeRow(
                id=user_id,
                email=email,
                name=name,
                role=role,
                enabled=True,
                must_change_password=False,
                tenant_id=tenant_id,
                oidc_provider=oidc_provider,
                oidc_subject=oidc_subject,
                auth_methods=auth_methods,
            )
            return
        raise AssertionError(f"unexpected execute SQL: {sql}")


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _CM:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _CM()


class _FakeRequest:
    def __init__(self) -> None:
        self.headers = {"user-agent": "pytest-oauth"}
        self.cookies = {olh.FLOW_COOKIE_NAME: "signed-flow"}

        class _Client:
            host = "203.0.113.10"

        self.client = _Client()


def _callback_result(
    *,
    provider: str,
    subject: str,
    email: str,
    name: str = "OAuth User",
) -> olh.CallbackResult:
    return olh.CallbackResult(
        flow=FlowSession(
            provider=provider,
            state="state-123",
            code_verifier="v" * 50,
            nonce=None,
            redirect_uri=f"https://example.test/auth/oauth/{provider}/callback",
            scope=("openid", "email"),
            created_at=1000.0,
            expires_at=1600.0,
        ),
        token=TokenSet(
            access_token="at",
            refresh_token="rt",
            token_type="Bearer",
            expires_at=4600.0,
            scope=("openid", "email"),
            id_token=None,
            raw={},
        ),
        identity=olh.OAuthUserIdentity(
            provider=provider,
            subject=subject,
            email=email,
            name=name,
        ),
    )


def _decoded_methods(row: _FakeRow) -> list[str]:
    return json.loads(row["auth_methods"])


@pytest.fixture()
def oauth_callback_harness(monkeypatch):
    sessions: list[tuple[str, str, str]] = []

    def install(users: dict[str, _FakeRow], result: olh.CallbackResult):
        conn = _FakeConn(users)
        monkeypatch.setattr(
            "backend.db_pool.get_pool", lambda: _FakePool(conn),
        )

        async def _complete_oauth_login(**_kwargs):
            return result

        async def _create_session(
            user_id: str,
            ip: str = "",
            user_agent: str = "",
            conn=None,
        ) -> auth.Session:
            sessions.append((user_id, ip, user_agent))
            return auth.Session(
                token=f"session-{user_id}",
                user_id=user_id,
                csrf_token=f"csrf-{user_id}",
                created_at=1.0,
                expires_at=2.0,
                ip=ip,
                user_agent=user_agent,
            )

        monkeypatch.setattr(olh, "complete_oauth_login", _complete_oauth_login)
        monkeypatch.setattr(auth, "create_session", _create_session)
        monkeypatch.setattr(
            auth_router, "_oauth_log_audit_safe", lambda *a, **k: None,
        )
        return conn

    return install, sessions


async def _run_callback(provider: str = "google"):
    return await auth_router.oauth_callback(
        provider=provider,
        request=_FakeRequest(),
        response=Response(),
        code="code-123",
        state="state-123",
    )


@pytest.mark.asyncio
async def test_oauth_callback_existing_subject_logs_in_without_link_write(
    oauth_callback_harness,
):
    users = {
        "u-linked": _user_row(
            user_id="u-linked",
            email="linked@example.com",
            oidc_provider="google",
            oidc_subject="google-sub-1",
            auth_methods=["oauth_google"],
        ),
    }
    install, sessions = oauth_callback_harness
    conn = install(
        users,
        _callback_result(
            provider="google",
            subject="google-sub-1",
            email="linked@example.com",
        ),
    )

    resp = await _run_callback("google")

    assert resp.status_code == 302
    assert sessions == [("u-linked", "203.0.113.10", "pytest-oauth")]
    assert _decoded_methods(users["u-linked"]) == ["oauth_google"]
    assert not any(
        "UPDATE users SET auth_methods" in sql
        for sql, _ in conn.execute_calls
    )


@pytest.mark.asyncio
async def test_oauth_callback_same_email_password_user_refuses_silent_link(
    oauth_callback_harness,
):
    users = {
        "u-password": _user_row(
            user_id="u-password",
            email="victim@example.com",
            auth_methods=["password"],
        ),
    }
    install, sessions = oauth_callback_harness
    conn = install(
        users,
        _callback_result(
            provider="google",
            subject="attacker-sub",
            email="victim@example.com",
        ),
    )

    with pytest.raises(HTTPException) as excinfo:
        await _run_callback("google")

    assert excinfo.value.status_code == 409
    assert "sign in with your password first" in str(excinfo.value.detail)
    assert sessions == []
    assert _decoded_methods(users["u-password"]) == ["password"]
    assert not any(
        "UPDATE users SET auth_methods" in sql
        for sql, _ in conn.execute_calls
    )


@pytest.mark.asyncio
async def test_oauth_callback_oauth_only_same_email_appends_new_provider(
    oauth_callback_harness,
):
    users = {
        "u-oauth": _user_row(
            user_id="u-oauth",
            email="oauth@example.com",
            auth_methods=["oauth_google"],
        ),
    }
    install, sessions = oauth_callback_harness
    install(
        users,
        _callback_result(
            provider="github",
            subject="github-sub-1",
            email="oauth@example.com",
        ),
    )

    resp = await _run_callback("github")

    assert resp.status_code == 302
    assert sessions == [("u-oauth", "203.0.113.10", "pytest-oauth")]
    assert _decoded_methods(users["u-oauth"]) == [
        "oauth_google", "oauth_github",
    ]


@pytest.mark.asyncio
async def test_oauth_callback_new_identity_creates_user_and_oauth_method(
    oauth_callback_harness,
):
    users: dict[str, _FakeRow] = {}
    install, sessions = oauth_callback_harness
    install(
        users,
        _callback_result(
            provider="google",
            subject="google-new-sub",
            email="new@example.com",
            name="New User",
        ),
    )

    resp = await _run_callback("google")

    assert resp.status_code == 302
    created = next(
        row for row in users.values() if row["email"] == "new@example.com"
    )
    assert created["name"] == "New User"
    assert created["oidc_provider"] == "google"
    assert created["oidc_subject"] == "google-new-sub"
    assert _decoded_methods(created) == ["oauth_google"]
    assert sessions == [(created["id"], "203.0.113.10", "pytest-oauth")]
