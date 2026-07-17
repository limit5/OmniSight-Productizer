"""OP-2671 hardened human action-challenge router tests.

Offline cases exercise the real ``auth.current_user`` dependency for every
principal mode that must be refused, then call the gate directly for precise
cookie, CSRF, denylist, and membership coverage.  PostgreSQL cases mint real
sessions and durable challenges through the standard test pool.

No new mutable module-global state is introduced.  Test constants are
identical per worker, while all decision state and cross-worker coordination
remain in PostgreSQL.  Requests in each integration case are sequential, so
the change introduces no read-after-write timing assumption.
"""
from __future__ import annotations

import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from backend import auth, db
from backend.routers import action_challenges


_API_PREFIX = "/api/v1"
_USER_AGENT = "op-2671-test"


class _AcquireContext:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_args):
        return False


class _MembershipConn:
    def __init__(self, row):
        self.row = row
        self.calls: list[tuple] = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, *args))
        return self.row


class _MembershipPool:
    def __init__(self, row):
        self.conn = _MembershipConn(row)
        self.acquire_count = 0

    def acquire(self):
        self.acquire_count += 1
        return _AcquireContext(self.conn)


class _ForbiddenPool:
    def acquire(self):
        raise AssertionError("membership pool must not be acquired")


def _user(
    *,
    user_id: str = "human-operator",
    email: str = "operator@example.com",
    role: str = "viewer",
    tenant_id: str = "tenant-a",
) -> auth.User:
    return auth.User(
        id=user_id,
        email=email,
        name="Human Operator",
        role=role,
        tenant_id=tenant_id,
    )


def _session(
    user_id: str = "human-operator",
    *,
    token: str = "real-cookie-token",
    csrf_token: str = "csrf-token",
) -> auth.Session:
    return auth.Session(
        token=token,
        user_id=user_id,
        csrf_token=csrf_token,
        created_at=1.0,
        expires_at=2.0,
        user_agent=_USER_AGENT,
    )


def _request(
    *,
    method: str = "GET",
    session: auth.Session | None = None,
    headers: dict[str, str] | None = None,
) -> Request:
    raw_headers = [
        (key.lower().encode(), value.encode())
        for key, value in (headers or {}).items()
    ]
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/api/v1/action-challenges/challenge-1",
            "raw_path": b"/api/v1/action-challenges/challenge-1",
            "query_string": b"",
            "headers": raw_headers,
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }
    )
    request.state.session = session
    return request


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(action_challenges.router, prefix=_API_PREFIX)
    return app


def _url(suffix: str) -> str:
    return f"{_API_PREFIX}/action-challenges/{suffix}"


def _assert_http_error(exc: pytest.ExceptionInfo[HTTPException], status: int):
    assert exc.value.status_code == status
    return exc.value.detail


def test_gate_depends_on_current_user_not_require_admin() -> None:
    dependency = inspect.signature(
        action_challenges._require_human_admin
    ).parameters["user"].default

    assert dependency.dependency is auth.current_user
    assert dependency.dependency is not auth.require_admin


def test_router_does_not_call_write_audit() -> None:
    source = Path(action_challenges.__file__).read_text(encoding="utf-8")

    assert "write_audit" not in source
    assert "backend.audit" not in source


def test_reason_blank_returns_400() -> None:
    with pytest.raises(HTTPException) as exc:
        action_challenges._require_reason("   ")

    assert _assert_http_error(exc, 400) == "reason text is required (audit)."


def test_reason_over_1024_is_trimmed() -> None:
    reason = "  " + ("r" * 1200) + "  "

    assert action_challenges._require_reason(reason) == "r" * 1024


@pytest.mark.asyncio
async def test_gate_authorization_header_is_forbidden(monkeypatch) -> None:
    monkeypatch.setattr(
        action_challenges,
        "get_pool",
        lambda: _ForbiddenPool(),
    )
    request = _request(
        session=_session(),
        headers={"Authorization": "Bearer stale-token"},
    )

    with pytest.raises(HTTPException) as exc:
        await action_challenges._require_human_admin(request, _user())

    assert _assert_http_error(exc, 403) == {
        "error": "cookie_session_required"
    }


@pytest.mark.asyncio
async def test_gate_empty_authorization_header_is_forbidden(monkeypatch) -> None:
    monkeypatch.setattr(
        action_challenges,
        "get_pool",
        lambda: _ForbiddenPool(),
    )
    request = _request(
        session=_session(),
        headers={"Authorization": ""},
    )

    with pytest.raises(HTTPException) as exc:
        await action_challenges._require_human_admin(request, _user())

    assert _assert_http_error(exc, 403) == {
        "error": "cookie_session_required"
    }


@pytest.mark.asyncio
async def test_gate_missing_session_is_forbidden(monkeypatch) -> None:
    monkeypatch.setattr(
        action_challenges,
        "get_pool",
        lambda: _ForbiddenPool(),
    )

    with pytest.raises(HTTPException) as exc:
        await action_challenges._require_human_admin(
            _request(session=None),
            _user(),
        )

    assert _assert_http_error(exc, 403) == {
        "error": "human_session_required"
    }


@pytest.mark.asyncio
async def test_gate_cookie_user_mismatch_is_forbidden(monkeypatch) -> None:
    monkeypatch.setattr(
        action_challenges,
        "get_pool",
        lambda: _ForbiddenPool(),
    )

    with pytest.raises(HTTPException) as exc:
        await action_challenges._require_human_admin(
            _request(session=_session("different-user")),
            _user(),
        )

    assert _assert_http_error(exc, 403) == {
        "error": "session_user_mismatch"
    }


@pytest.mark.asyncio
async def test_gate_anonymous_user_is_forbidden(monkeypatch) -> None:
    monkeypatch.setattr(
        action_challenges,
        "get_pool",
        lambda: _ForbiddenPool(),
    )
    anonymous = _user(
        user_id="anonymous",
        email="anonymous@local",
        role="super_admin",
    )

    with pytest.raises(HTTPException) as exc:
        await action_challenges._require_human_admin(
            _request(session=_session("anonymous")),
            anonymous,
        )

    assert _assert_http_error(exc, 403) == {
        "error": "human_operator_required"
    }


@pytest.mark.asyncio
async def test_gate_bot_principal_is_forbidden(monkeypatch) -> None:
    monkeypatch.setattr(
        action_challenges,
        "get_pool",
        lambda: _ForbiddenPool(),
    )
    bot = _user(
        user_id="ci-reviewer",
        email="ci-reviewer@example.com",
        role="admin",
    )

    with pytest.raises(HTTPException) as exc:
        await action_challenges._require_human_admin(
            _request(session=_session("ci-reviewer")),
            bot,
        )

    detail = _assert_http_error(exc, 403)
    assert detail["error"] == "auth_refused"


@pytest.mark.asyncio
async def test_gate_missing_membership_is_forbidden(monkeypatch) -> None:
    pool = _MembershipPool(None)
    monkeypatch.setattr(action_challenges, "get_pool", lambda: pool)

    with pytest.raises(HTTPException) as exc:
        await action_challenges._require_human_admin(
            _request(session=_session()),
            _user(role="admin"),
        )

    assert _assert_http_error(exc, 403) == {
        "error": "tenant_admin_required"
    }
    assert pool.acquire_count == 1


@pytest.mark.asyncio
async def test_gate_suspended_membership_is_forbidden(monkeypatch) -> None:
    pool = _MembershipPool({"role": "owner", "status": "suspended"})
    monkeypatch.setattr(action_challenges, "get_pool", lambda: pool)

    with pytest.raises(HTTPException) as exc:
        await action_challenges._require_human_admin(
            _request(session=_session()),
            _user(role="admin"),
        )

    assert _assert_http_error(exc, 403) == {
        "error": "tenant_admin_required"
    }


@pytest.mark.asyncio
async def test_gate_split_role_active_owner_passes(monkeypatch) -> None:
    pool = _MembershipPool({"role": "owner", "status": "active"})
    monkeypatch.setattr(action_challenges, "get_pool", lambda: pool)
    user = _user(role="viewer")

    result = await action_challenges._require_human_admin(
        _request(session=_session()),
        user,
    )

    assert result is user
    assert pool.acquire_count == 1


@pytest.mark.asyncio
async def test_gate_active_admin_membership_passes(monkeypatch) -> None:
    pool = _MembershipPool({"role": "admin", "status": "active"})
    monkeypatch.setattr(action_challenges, "get_pool", lambda: pool)
    user = _user(role="admin")

    result = await action_challenges._require_human_admin(
        _request(session=_session()),
        user,
    )

    assert result is user


@pytest.mark.asyncio
async def test_gate_super_admin_passes_without_membership_lookup(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        action_challenges,
        "get_pool",
        lambda: _ForbiddenPool(),
    )
    user = _user(role="super_admin")

    result = await action_challenges._require_human_admin(
        _request(session=_session()),
        user,
    )

    assert result is user


@pytest.mark.asyncio
async def test_gate_post_without_csrf_is_forbidden(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    monkeypatch.setattr(
        action_challenges,
        "get_pool",
        lambda: _ForbiddenPool(),
    )

    with pytest.raises(HTTPException) as exc:
        await action_challenges._require_human_admin(
            _request(method="POST", session=_session()),
            _user(),
        )

    assert _assert_http_error(exc, 403) == "CSRF token missing or invalid"


@pytest.mark.asyncio
async def test_gate_post_with_matching_csrf_passes(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    pool = _MembershipPool({"role": "owner", "status": "active"})
    monkeypatch.setattr(action_challenges, "get_pool", lambda: pool)
    user = _user(role="viewer")

    result = await action_challenges._require_human_admin(
        _request(
            method="POST",
            session=_session(),
            headers={auth.CSRF_HEADER: "csrf-token"},
        ),
        user,
    )

    assert result is user


def test_full_stack_open_anonymous_is_forbidden(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")

    with TestClient(_app()) as client:
        response = client.get(_url("challenge-open"))

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "human_session_required"


def test_full_stack_session_unauthenticated_get_is_forbidden(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")

    with TestClient(_app()) as client:
        response = client.get(_url("challenge-unauthenticated"))

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "human_session_required"


def test_full_stack_per_key_bearer_is_forbidden(monkeypatch) -> None:
    from backend import api_keys

    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")

    async def _valid_key(_raw, *, ip=""):
        del ip
        return SimpleNamespace(id="key-1", name="automation")

    monkeypatch.setattr(api_keys, "validate_bearer", _valid_key)

    with TestClient(_app()) as client:
        response = client.get(
            _url("challenge-api-key"),
            headers={"Authorization": "Bearer per-key-secret"},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "cookie_session_required"


def test_full_stack_legacy_bearer_is_forbidden(monkeypatch) -> None:
    from backend import api_keys

    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    monkeypatch.setenv("OMNISIGHT_DECISION_BEARER", "legacy-secret")

    async def _no_key(_raw, *, ip=""):
        del ip
        return None

    monkeypatch.setattr(api_keys, "validate_bearer", _no_key)

    with TestClient(_app()) as client:
        response = client.get(
            _url("challenge-legacy"),
            headers={"Authorization": "Bearer legacy-secret"},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "cookie_session_required"


def test_full_stack_invalid_bearer_is_forbidden(monkeypatch) -> None:
    from backend import api_keys

    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    monkeypatch.delenv("OMNISIGHT_DECISION_BEARER", raising=False)

    async def _no_key(_raw, *, ip=""):
        del ip
        return None

    monkeypatch.setattr(api_keys, "validate_bearer", _no_key)

    with TestClient(_app()) as client:
        response = client.get(
            _url("challenge-invalid"),
            headers={"Authorization": "Bearer invalid-secret"},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "cookie_session_required"


def test_full_stack_cookie_with_stale_bearer_is_forbidden(
    monkeypatch,
) -> None:
    from backend import api_keys

    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    monkeypatch.delenv("OMNISIGHT_DECISION_BEARER", raising=False)
    session = _session()
    user = _user()

    async def _no_key(_raw, *, ip=""):
        del ip
        return None

    async def _get_session(token):
        return session if token == session.token else None

    async def _get_user(user_id):
        return user if user_id == user.id else None

    async def _ua_matches(_session_value, _current_ua):
        return True

    monkeypatch.setattr(api_keys, "validate_bearer", _no_key)
    monkeypatch.setattr(auth, "get_session", _get_session)
    monkeypatch.setattr(auth, "get_user", _get_user)
    monkeypatch.setattr(auth, "check_ua_binding", _ua_matches)

    with TestClient(_app()) as client:
        client.cookies.set(auth.SESSION_COOKIE, session.token)
        response = client.get(
            _url("challenge-cookie-stale"),
            headers={"Authorization": "Bearer stale-secret"},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "cookie_session_required"


async def _seed_tenant(pool, tenant_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free') "
            "ON CONFLICT (id) DO NOTHING",
            tenant_id,
            tenant_id,
        )


async def _seed_operator(
    pool,
    *,
    tenant_id: str,
    membership_role: str = "owner",
    user_role: str = "viewer",
) -> tuple[auth.User, auth.Session]:
    await _seed_tenant(pool, tenant_id)
    suffix = uuid.uuid4().hex
    async with pool.acquire() as conn:
        user = await auth.create_user(
            email=f"operator-{suffix}@example.com",
            name="OP-2671 Operator",
            role=user_role,
            tenant_id=tenant_id,
            conn=conn,
        )
        await conn.execute(
            "INSERT INTO user_tenant_memberships "
            "(user_id, tenant_id, role, status) "
            "VALUES ($1, $2, $3, 'active')",
            user.id,
            tenant_id,
            membership_role,
        )
        session = await auth.create_session(
            user.id,
            user_agent=_USER_AGENT,
            conn=conn,
        )
    return user, session


async def _seed_challenge(pool, *, tenant_id: str) -> dict:
    await _seed_tenant(pool, tenant_id)
    suffix = uuid.uuid4().hex
    action_instance_id = f"action-{suffix}"
    challenge_id = f"challenge-{suffix}"
    executable_args = {
        "content": "server-stored content",
        "path": f"{suffix}.txt",
    }
    human_rendering = {"summary": "Write the prepared output"}
    prepared_digest = uuid.uuid4().hex * 2
    async with pool.acquire() as conn:
        assert await db.put_prepared_action(
            conn,
            action_instance_id=action_instance_id,
            tenant_id=tenant_id,
            principal_type="service",
            actor_id=f"operation-actor-{suffix}",
            request_id=f"request-{suffix}",
            model_call_id="",
            adapter_namespace="workspace",
            tool_name="write_file",
            schema_version="v1",
            family="workspace.write",
            effect="mutating",
            canonical_target=f"/workspace/{suffix}.txt",
            args_hash="a" * 64,
            provenance_kind="no_model_input",
            model_snapshot_id=None,
            no_model_input_source="slash_command",
            executable_args_json=json.dumps(executable_args),
            human_rendering_json=json.dumps(human_rendering),
            prepared_action_digest=prepared_digest,
        ) is True
        assert await db.put_challenge(
            conn,
            challenge_id=challenge_id,
            tenant_id=tenant_id,
            action_instance_id=action_instance_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ) is True
    return {
        "tenant_id": tenant_id,
        "action_instance_id": action_instance_id,
        "challenge_id": challenge_id,
        "executable_args": executable_args,
        "human_rendering": human_rendering,
        "prepared_action_digest": prepared_digest,
    }


def _cookie_headers(session: auth.Session) -> dict[str, str]:
    return {
        auth.CSRF_HEADER: session.csrf_token,
        "User-Agent": _USER_AGENT,
    }


async def _challenge_counts(pool, tenant_id: str) -> tuple[int, int]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT "
            "(SELECT count(*) FROM action_grants WHERE tenant_id = $1) "
            "AS grants, "
            "(SELECT count(*) FROM resume_jobs WHERE tenant_id = $1) "
            "AS resumes",
            tenant_id,
        )
    return row["grants"], row["resumes"]


async def _move_challenge_expiry_to_past(pool, challenge_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE challenges "
            "SET created_at = clock_timestamp() - interval '2 hours', "
            "expires_at = clock_timestamp() - interval '1 hour' "
            "WHERE challenge_id = $1",
            challenge_id,
        )


@pytest.mark.asyncio
async def test_pg_full_stack_real_cookie_active_owner_passes(
    pg_test_pool,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    tenant_id = f"t-router-owner-{uuid.uuid4().hex}"
    _user_row, session = await _seed_operator(
        pg_test_pool,
        tenant_id=tenant_id,
    )
    transport = ASGITransport(app=_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            _url("absent-challenge"),
            cookies={auth.SESSION_COOKIE: session.token},
            headers={"User-Agent": _USER_AGENT},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "no challenge absent-challenge"


@pytest.mark.asyncio
async def test_pg_confirm_mints_grant_resume_and_transactional_attribution(
    pg_test_pool,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    tenant_id = f"t-router-confirm-{uuid.uuid4().hex}"
    user, session = await _seed_operator(pg_test_pool, tenant_id=tenant_id)
    case = await _seed_challenge(pg_test_pool, tenant_id=tenant_id)
    transport = ASGITransport(app=_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            _url(f"{case['challenge_id']}/confirm"),
            data={"reason": "Reviewed and approved"},
            cookies={auth.SESSION_COOKIE: session.token},
            headers=_cookie_headers(session),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["confirmed_by"] == user.email
    assert body["grant_id"].startswith("grant-")
    assert body["resume_id"].startswith("resume-")

    async with pg_test_pool.acquire() as conn:
        challenge = await db.get_challenge(
            conn,
            case["challenge_id"],
            tenant_id=tenant_id,
        )
        grant = await db.get_action_grant(
            conn,
            body["grant_id"],
            tenant_id=tenant_id,
        )
        resume = await db.get_resume_job(
            conn,
            body["resume_id"],
            tenant_id=tenant_id,
        )
        execution_count = await conn.fetchval(
            "SELECT count(*) FROM execution_results WHERE tenant_id = $1",
            tenant_id,
        )

    expected_auth_event_id = auth.session_id_from_token(session.token)
    assert challenge["state"] == "confirmed"
    assert challenge["confirmer_actor"] == user.email
    assert challenge["confirmer_principal_type"] == "human"
    assert challenge["confirmer_auth_event_id"] == expected_auth_event_id
    assert challenge["confirmer_auth_event_id"] != session.token
    assert challenge["confirm_reason"] == "Reviewed and approved"
    assert challenge["confirmed_at"] is not None
    assert grant["grant_issuer_source"] == "ui_confirm"
    assert grant["state"] == "pending"
    assert resume["state"] == "queued"
    assert resume["grant_id"] == grant["grant_id"]
    assert execution_count == 0


@pytest.mark.asyncio
async def test_pg_double_confirm_returns_409_and_single_grant_resume(
    pg_test_pool,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    tenant_id = f"t-router-double-{uuid.uuid4().hex}"
    _user_row, session = await _seed_operator(
        pg_test_pool,
        tenant_id=tenant_id,
    )
    case = await _seed_challenge(pg_test_pool, tenant_id=tenant_id)
    transport = ASGITransport(app=_app())
    request_kwargs = {
        "data": {"reason": "Approved once"},
        "cookies": {auth.SESSION_COOKIE: session.token},
        "headers": _cookie_headers(session),
    }

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            _url(f"{case['challenge_id']}/confirm"),
            **request_kwargs,
        )
        second = await client.post(
            _url(f"{case['challenge_id']}/confirm"),
            **request_kwargs,
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert await _challenge_counts(pg_test_pool, tenant_id) == (1, 1)


@pytest.mark.asyncio
async def test_pg_reject_then_confirm_returns_409_without_work(
    pg_test_pool,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    tenant_id = f"t-router-reject-{uuid.uuid4().hex}"
    user, session = await _seed_operator(pg_test_pool, tenant_id=tenant_id)
    case = await _seed_challenge(pg_test_pool, tenant_id=tenant_id)
    transport = ASGITransport(app=_app())
    request_kwargs = {
        "cookies": {auth.SESSION_COOKIE: session.token},
        "headers": _cookie_headers(session),
    }

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        rejected = await client.post(
            _url(f"{case['challenge_id']}/reject"),
            data={"reason": "Unsafe target"},
            **request_kwargs,
        )
        confirmed = await client.post(
            _url(f"{case['challenge_id']}/confirm"),
            data={"reason": "Changed my mind"},
            **request_kwargs,
        )

    assert rejected.status_code == 200, rejected.text
    assert rejected.json() == {
        "challenge_id": case["challenge_id"],
        "status": "rejected",
        "rejected_by": user.email,
    }
    assert confirmed.status_code == 409, confirmed.text

    async with pg_test_pool.acquire() as conn:
        challenge = await db.get_challenge(
            conn,
            case["challenge_id"],
            tenant_id=tenant_id,
        )
    assert challenge["state"] == "rejected"
    assert challenge["confirmer_actor"] == user.email
    assert challenge["confirmer_principal_type"] == "human"
    assert challenge["confirmer_auth_event_id"] == auth.session_id_from_token(
        session.token
    )
    assert challenge["confirm_reason"] == "Unsafe target"
    assert await _challenge_counts(pg_test_pool, tenant_id) == (0, 0)


@pytest.mark.asyncio
async def test_pg_tenant_isolation_returns_409_and_leaves_source_pending(
    pg_test_pool,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    suffix = uuid.uuid4().hex
    tenant_a = f"t-router-isolation-a-{suffix}"
    tenant_b = f"t-router-isolation-b-{suffix}"
    case = await _seed_challenge(pg_test_pool, tenant_id=tenant_a)
    _user_row, session_b = await _seed_operator(
        pg_test_pool,
        tenant_id=tenant_b,
    )
    transport = ASGITransport(app=_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            _url(f"{case['challenge_id']}/confirm"),
            data={"reason": "Cross-tenant attempt"},
            cookies={auth.SESSION_COOKIE: session_b.token},
            headers=_cookie_headers(session_b),
        )

    assert response.status_code == 409, response.text
    async with pg_test_pool.acquire() as conn:
        challenge = await db.get_challenge(
            conn,
            case["challenge_id"],
            tenant_id=tenant_a,
        )
    assert challenge["state"] == "pending"
    assert challenge["confirmer_actor"] is None
    assert await _challenge_counts(pg_test_pool, tenant_a) == (0, 0)


@pytest.mark.asyncio
async def test_pg_expired_challenge_returns_409(
    pg_test_pool,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    tenant_id = f"t-router-expired-{uuid.uuid4().hex}"
    _user_row, session = await _seed_operator(
        pg_test_pool,
        tenant_id=tenant_id,
    )
    case = await _seed_challenge(pg_test_pool, tenant_id=tenant_id)
    await _move_challenge_expiry_to_past(
        pg_test_pool,
        case["challenge_id"],
    )
    transport = ASGITransport(app=_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            _url(f"{case['challenge_id']}/confirm"),
            data={"reason": "Too late"},
            cookies={auth.SESSION_COOKIE: session.token},
            headers=_cookie_headers(session),
        )

    assert response.status_code == 409, response.text
    assert await _challenge_counts(pg_test_pool, tenant_id) == (0, 0)


@pytest.mark.asyncio
async def test_pg_get_returns_decoded_action_and_iso_datetimes(
    pg_test_pool,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    tenant_id = f"t-router-get-{uuid.uuid4().hex}"
    _user_row, session = await _seed_operator(
        pg_test_pool,
        tenant_id=tenant_id,
    )
    case = await _seed_challenge(pg_test_pool, tenant_id=tenant_id)
    transport = ASGITransport(app=_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            _url(case["challenge_id"]),
            cookies={auth.SESSION_COOKIE: session.token},
            headers={"User-Agent": _USER_AGENT},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["prepared_action"]["executable_args"] == case[
        "executable_args"
    ]
    assert body["prepared_action"]["human_rendering"] == case[
        "human_rendering"
    ]
    assert body["prepared_action"]["prepared_action_digest"] == case[
        "prepared_action_digest"
    ]
    assert isinstance(body["prepared_action"]["executable_args"], dict)
    assert isinstance(body["prepared_action"]["human_rendering"], dict)
    assert datetime.fromisoformat(body["created_at"]).tzinfo is not None
    assert datetime.fromisoformat(body["expires_at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_pg_get_missing_prepared_action_fails_closed_500(
    pg_test_pool,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    tenant_id = f"t-router-missing-{uuid.uuid4().hex}"
    _user_row, session = await _seed_operator(
        pg_test_pool,
        tenant_id=tenant_id,
    )
    case = await _seed_challenge(pg_test_pool, tenant_id=tenant_id)

    async def _missing_prepared(_conn, _action_instance_id, *, tenant_id):
        del tenant_id
        return None

    monkeypatch.setattr(db, "get_prepared_action", _missing_prepared)
    transport = ASGITransport(app=_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            _url(case["challenge_id"]),
            cookies={auth.SESSION_COOKIE: session.token},
            headers={"User-Agent": _USER_AGENT},
        )

    assert response.status_code == 500, response.text
    assert response.json()["detail"] == "challenge integrity error"


@pytest.mark.asyncio
async def test_pg_confirm_runtime_error_returns_generic_500(
    pg_test_pool,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    tenant_id = f"t-router-runtime-{uuid.uuid4().hex}"
    _user_row, session = await _seed_operator(
        pg_test_pool,
        tenant_id=tenant_id,
    )
    case = await _seed_challenge(pg_test_pool, tenant_id=tenant_id)

    async def _integrity_error(_conn, **_kwargs):
        raise RuntimeError("sensitive internal integrity detail")

    monkeypatch.setattr(db, "confirm_challenge", _integrity_error)
    transport = ASGITransport(app=_app(), raise_app_exceptions=False)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            _url(f"{case['challenge_id']}/confirm"),
            data={"reason": "Reviewed"},
            cookies={auth.SESSION_COOKIE: session.token},
            headers=_cookie_headers(session),
        )

    assert response.status_code == 500, response.text
    assert response.json()["detail"] == "confirm failed"
    assert "sensitive internal integrity detail" not in response.text


def test_main_registers_versioned_action_challenge_routes() -> None:
    from backend.main import app

    routes = {
        (route.path, method)
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
    }

    assert (_url("{challenge_id}"), "GET") in routes
    assert (_url("{challenge_id}/confirm"), "POST") in routes
    assert (_url("{challenge_id}/reject"), "POST") in routes
    assert ("/api/v2/action-challenges/{challenge_id}", "GET") in routes
    assert (
        "/api/v2/action-challenges/{challenge_id}/confirm",
        "POST",
    ) in routes
    assert (
        "/api/v2/action-challenges/{challenge_id}/reject",
        "POST",
    ) in routes
