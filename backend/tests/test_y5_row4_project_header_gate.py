"""Y5 (#281) row 4 — drift guard for ``_project_header_gate`` middleware.

Mirrors the I7 ``_tenant_header_gate`` validation pattern but for the
``X-Project-Id`` header that the frontend api client (``lib/api.ts``)
emits alongside ``X-Tenant-Id``. The middleware is a defence-in-depth
gate that runs BEFORE the route handler, so even routes that forget
to declare ``Depends(require_project_member)`` cannot be steered to a
foreign project via header injection.

Pure-unit + ASGI-mount tests run without PG by stubbing the asyncpg
pool with a fake that honours the same ``fetchrow`` signatures the
middleware uses. Live-PG tests would re-validate against real schema
but are not strictly necessary — the SQL constants are imported from
``backend.auth`` and exercised by the existing ``require_project_member``
PG suite.

Drift guard families:
  (a) Header pattern regex stays disjoint from ``t-…`` and unbounded
      free-form strings — drift here would let an attacker probe with
      ``../`` or SQL-meta characters.
  (b) Pass-through cases — no header, open mode, no session, unknown
      user — must NOT 4xx so unauthenticated public routes still work.
  (c) Hard rejection cases — malformed header (400), unknown project
      (404), cross-tenant header mismatch (403), no membership (403).
  (d) Membership resolution mirrors ``require_project_member``:
      super_admin → bypass; project_members direct hit → that role;
      active tenant owner/admin → contributor fallback; everything
      else → 403.
  (e) ContextVar pinning — on success ``set_tenant_id`` /
      ``set_project_id`` / ``set_user_role`` are all populated so the
      Y5 row 3 SQLAlchemy listener picks them up.
  (f) Self-fingerprint guard — middleware source has 0 hits on the
      compat fingerprint grep.
"""

from __future__ import annotations

import inspect
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (a) Header pattern + import surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_middleware_is_exported_from_main():
    from backend.main import _project_header_gate
    assert callable(_project_header_gate)


def test_middleware_pattern_matches_well_formed_project_id():
    from backend.main import _PROJECT_ID_HEADER_PATTERN
    assert _PROJECT_ID_HEADER_PATTERN.match("p-abc123")
    assert _PROJECT_ID_HEADER_PATTERN.match("p-" + "0" * 16)
    assert _PROJECT_ID_HEADER_PATTERN.match("p-a-b-c-d-1-2-3-4")


@pytest.mark.parametrize("bad", [
    "",                              # empty
    "t-acme",                        # tenant id, not a project id
    "P-ABC123",                      # uppercase
    "p-",                            # too short
    "p-x",                           # too short
    "p-../etc/passwd",               # path-traversal flavour
    "p-abc'; DROP TABLE projects--", # SQL-meta probe
    "p- abc",                        # whitespace
    "p-abc/extra",                   # slash
])
def test_middleware_pattern_rejects_malformed_or_hostile(bad):
    from backend.main import _PROJECT_ID_HEADER_PATTERN
    assert not _PROJECT_ID_HEADER_PATTERN.match(bad)


def test_middleware_pattern_aligned_with_tenant_projects_router():
    """The shape we accept in the header MUST be exactly the one the
    project-create handler mints. Drift would let the header pass
    something the DB layer would refuse."""
    from backend.main import _PROJECT_ID_HEADER_PATTERN
    from backend.routers.tenant_projects import PROJECT_ID_PATTERN
    assert _PROJECT_ID_HEADER_PATTERN.pattern == PROJECT_ID_PATTERN


def test_middleware_reuses_authz_sql_constants():
    """The middleware MUST share its SQL strings with
    ``require_project_member`` so the two layers can never disagree
    about which rows count as "this user has membership"."""
    from backend import auth, main
    src = inspect.getsource(main._project_header_gate)
    assert "_FETCH_PROJECT_BY_ID_FOR_AUTHZ_SQL" in src
    assert "_FETCH_PROJECT_MEMBER_FOR_AUTHZ_SQL" in src
    assert "_FETCH_TENANT_MEMBERSHIP_FOR_AUTHZ_SQL" in src
    assert "_TENANT_ROLE_DEFAULT_PROJECT_ROLE" in src
    # Sanity: those names actually exist on the auth module today.
    assert hasattr(auth, "_FETCH_PROJECT_BY_ID_FOR_AUTHZ_SQL")
    assert hasattr(auth, "_FETCH_PROJECT_MEMBER_FOR_AUTHZ_SQL")
    assert hasattr(auth, "_FETCH_TENANT_MEMBERSHIP_FOR_AUTHZ_SQL")
    assert hasattr(auth, "_TENANT_ROLE_DEFAULT_PROJECT_ROLE")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test infrastructure — fake asyncpg pool + harness app
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class _Row:
    """Minimal asyncpg.Record shim — supports ``row[key]`` lookups."""
    data: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]


class _FakeConn:
    """Routes ``fetchrow`` to a callable provided by the test."""

    def __init__(self, fetchrow_fn):
        self._fn = fetchrow_fn

    async def fetchrow(self, sql: str, *args: Any):
        return await self._fn(sql, *args)


class _FakePool:
    """``async with pool.acquire() as conn:`` emits a ``_FakeConn``."""

    def __init__(self, fetchrow_fn):
        self._fn = fetchrow_fn

    @asynccontextmanager
    async def acquire(self):
        yield _FakeConn(self._fn)


def _build_harness(monkeypatch, *, fetchrow_fn, fake_user, auth_mode="session"):
    """Build a tiny Starlette app with the project gate installed.

    ``fetchrow_fn(sql, *args) -> _Row | None`` decides what each DB
    lookup returns. ``fake_user`` is what ``auth.get_user`` resolves
    to (set to ``None`` to simulate an anonymous request).
    """
    from backend import auth as _auth
    from backend import db_context, main

    # Reset ContextVars at the harness boundary so a previous test's
    # leftover values can't paper over a missing ``set_*`` call in the
    # middleware.
    db_context.set_tenant_id(None)
    db_context.set_project_id(None)
    db_context.set_user_role(None)

    # Stub auth_mode + session lookup + user lookup. The default fake
    # session always resolves so tests focus on the membership logic.
    monkeypatch.setattr(_auth, "auth_mode", lambda: auth_mode)

    @dataclass
    class _Sess:
        user_id: str

    async def _fake_get_session(_cookie):
        return _Sess(user_id=fake_user.id) if fake_user else None

    async def _fake_get_user(_uid):
        return fake_user

    monkeypatch.setattr(_auth, "get_session", _fake_get_session)
    monkeypatch.setattr(_auth, "get_user", _fake_get_user)

    # Stub the asyncpg pool that the middleware imports lazily.
    fake_pool = _FakePool(fetchrow_fn)
    monkeypatch.setattr(
        "backend.db_pool.get_pool", lambda: fake_pool,
    )

    async def _probe(request: Request) -> JSONResponse:
        return JSONResponse({
            "ctx_tenant_id": db_context.current_tenant_id(),
            "ctx_project_id": db_context.current_project_id(),
            "ctx_user_role": db_context.current_user_role(),
        })

    app = Starlette(routes=[Route("/probe", _probe)])
    app.middleware("http")(main._project_header_gate)
    return app


def _user(role="viewer", *, uid="u-test", tid="t-test"):
    from backend import auth as _auth
    return _auth.User(
        id=uid, email=f"{uid}@x.com", name=uid,
        role=role, enabled=True, tenant_id=tid,
    )


def _client(app):
    return TestClient(app, cookies={"omnisight_session": "tok-xyz"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (b) Pass-through cases — middleware MUST NOT short-circuit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_pass_through_when_no_header(monkeypatch):
    async def _never_called(_sql, *_args):
        raise AssertionError("DB must not be touched without X-Project-Id")

    app = _build_harness(monkeypatch,
                         fetchrow_fn=_never_called, fake_user=_user())
    res = _client(app).get("/probe")
    assert res.status_code == 200
    body = res.json()
    assert body["ctx_project_id"] is None


def test_open_mode_pins_header_at_face_value_no_db(monkeypatch):
    async def _never_called(_sql, *_args):
        raise AssertionError("open-mode must not consult DB")

    app = _build_harness(monkeypatch,
                         fetchrow_fn=_never_called,
                         fake_user=_user(),
                         auth_mode="open")
    res = _client(app).get("/probe", headers={"X-Project-Id": "p-open12345"})
    assert res.status_code == 200
    assert res.json()["ctx_project_id"] == "p-open12345"


def test_session_mode_no_cookie_passes_through(monkeypatch):
    async def _never_called(_sql, *_args):
        raise AssertionError("anonymous request must not consult DB")

    app = _build_harness(monkeypatch,
                         fetchrow_fn=_never_called, fake_user=_user())
    # Cookie cleared explicitly — the test client default is set in
    # _client() but we want the no-cookie path here.
    res = TestClient(app).get(
        "/probe", headers={"X-Project-Id": "p-anon123456"},
    )
    assert res.status_code == 200
    # ContextVar must remain None — middleware refused to set it.
    assert res.json()["ctx_project_id"] is None


def test_session_mode_unknown_user_passes_through(monkeypatch):
    async def _never_called(_sql, *_args):
        raise AssertionError("unknown user must not consult DB")

    app = _build_harness(monkeypatch,
                         fetchrow_fn=_never_called, fake_user=None)
    res = _client(app).get(
        "/probe", headers={"X-Project-Id": "p-ghostuser01"},
    )
    assert res.status_code == 200
    assert res.json()["ctx_project_id"] is None


def test_pool_not_initialised_passes_through(monkeypatch):
    """If the asyncpg pool isn't ready (smoke test / dependency-light
    boot) the middleware must not 500 — pass through and let the
    handler decide. Mirrors the ``except RuntimeError`` branch."""
    from backend import auth as _auth, db_context, main

    db_context.set_tenant_id(None)
    db_context.set_project_id(None)
    db_context.set_user_role(None)

    monkeypatch.setattr(_auth, "auth_mode", lambda: "session")

    @dataclass
    class _Sess:
        user_id: str

    async def _fake_get_session(_cookie):
        return _Sess(user_id="u-test")

    async def _fake_get_user(_uid):
        return _user()

    monkeypatch.setattr(_auth, "get_session", _fake_get_session)
    monkeypatch.setattr(_auth, "get_user", _fake_get_user)

    def _raise_pool():
        raise RuntimeError("pool not initialised")

    monkeypatch.setattr("backend.db_pool.get_pool", _raise_pool)

    async def _probe(request: Request) -> JSONResponse:
        return JSONResponse({
            "ctx_project_id": db_context.current_project_id(),
        })

    app = Starlette(routes=[Route("/probe", _probe)])
    app.middleware("http")(main._project_header_gate)

    res = TestClient(app, cookies={"omnisight_session": "tok"}).get(
        "/probe", headers={"X-Project-Id": "p-poolnotready"},
    )
    assert res.status_code == 200
    assert res.json()["ctx_project_id"] is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (c) Hard rejection cases — middleware MUST 4xx
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("bad_header", [
    "P-UPPERCASE",
    "../etc/passwd",
    "t-acme",
    "p-",
    "x" * 200,
    "p-bad'; DROP TABLE projects--",
])
def test_400_on_malformed_header(monkeypatch, bad_header):
    async def _never_called(_sql, *_args):
        raise AssertionError("malformed header must not consult DB")

    app = _build_harness(monkeypatch,
                         fetchrow_fn=_never_called, fake_user=_user())
    res = _client(app).get("/probe", headers={"X-Project-Id": bad_header})
    assert res.status_code == 400
    assert "malformed" in res.json()["detail"].lower()


def test_404_when_project_unknown(monkeypatch):
    async def _no_project(_sql, *_args):
        return None  # PROJECT_BY_ID returns nothing

    app = _build_harness(monkeypatch,
                         fetchrow_fn=_no_project, fake_user=_user())
    res = _client(app).get(
        "/probe", headers={"X-Project-Id": "p-doesnotexist"},
    )
    assert res.status_code == 404
    assert "p-doesnotexist" in res.json()["detail"]


def test_403_on_cross_tenant_header_mismatch(monkeypatch):
    """If X-Tenant-Id is also present but points at a different tenant
    than the project belongs to, refuse — otherwise the listener would
    silently mask rows by pinning the wrong tenant filter."""
    async def _project_in_acme(sql, *args):
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-acme"})
        raise AssertionError(f"unexpected SQL: {sql!r}")

    app = _build_harness(monkeypatch,
                         fetchrow_fn=_project_in_acme,
                         fake_user=_user(role="viewer", tid="t-acme"))
    res = _client(app).get(
        "/probe",
        headers={
            "X-Project-Id": "p-acme01",
            "X-Tenant-Id": "t-other",
        },
    )
    assert res.status_code == 403
    assert "different" in res.json()["detail"]


def test_403_when_no_membership_at_all(monkeypatch):
    """Authenticated user with neither a project_members row nor an
    active tenant_membership owner/admin — must 403."""
    async def _orchestrate(sql, *args):
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-acme"})
        if "FROM project_members" in sql:
            return None
        if "FROM user_tenant_memberships" in sql:
            return None
        raise AssertionError(f"unexpected SQL: {sql!r}")

    app = _build_harness(monkeypatch,
                         fetchrow_fn=_orchestrate,
                         fake_user=_user(role="viewer", tid="t-acme"))
    res = _client(app).get(
        "/probe", headers={"X-Project-Id": "p-acme01"},
    )
    assert res.status_code == 403


def test_403_when_tenant_membership_suspended(monkeypatch):
    """Suspended tenant memberships do NOT confer the contributor
    fallback — even an admin role row that is not ``status='active'``
    must 403."""
    async def _suspended(sql, *args):
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-acme"})
        if "FROM project_members" in sql:
            return None
        if "FROM user_tenant_memberships" in sql:
            return _Row({"role": "admin", "status": "suspended"})
        raise AssertionError(f"unexpected SQL: {sql!r}")

    app = _build_harness(monkeypatch,
                         fetchrow_fn=_suspended,
                         fake_user=_user(role="viewer", tid="t-acme"))
    res = _client(app).get(
        "/probe", headers={"X-Project-Id": "p-acme02"},
    )
    assert res.status_code == 403


@pytest.mark.parametrize("tenant_role", ["member", "viewer"])
def test_403_when_tenant_role_not_owner_or_admin(monkeypatch, tenant_role):
    """Active ``member`` / ``viewer`` tenant roles intentionally do
    NOT fall through to ``contributor`` — they need an explicit
    project_members grant to access any project."""
    async def _active_low_role(sql, *args):
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-acme"})
        if "FROM project_members" in sql:
            return None
        if "FROM user_tenant_memberships" in sql:
            return _Row({"role": tenant_role, "status": "active"})
        raise AssertionError(f"unexpected SQL: {sql!r}")

    app = _build_harness(monkeypatch,
                         fetchrow_fn=_active_low_role,
                         fake_user=_user(role="viewer", tid="t-acme"))
    res = _client(app).get(
        "/probe", headers={"X-Project-Id": "p-acme03"},
    )
    assert res.status_code == 403


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) + (e) Happy paths — membership resolves, ContextVars pinned
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_super_admin_bypasses_membership_lookup(monkeypatch):
    """``super_admin`` short-circuits — only the project lookup runs;
    project_members / tenant_membership are never consulted."""
    seen_sql = []

    async def _record(sql, *args):
        seen_sql.append(sql)
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-platform"})
        raise AssertionError(
            "super_admin must not trigger membership lookups"
        )

    app = _build_harness(
        monkeypatch,
        fetchrow_fn=_record,
        fake_user=_user(role="super_admin", uid="u-sa", tid="t-foo"),
    )
    res = _client(app).get(
        "/probe", headers={"X-Project-Id": "p-platform0001"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ctx_tenant_id"] == "t-platform"
    assert body["ctx_project_id"] == "p-platform0001"
    assert body["ctx_user_role"] == "super_admin"
    assert all("FROM projects" in s for s in seen_sql)


@pytest.mark.parametrize("project_role", ["viewer", "contributor", "owner"])
def test_project_member_direct_hit_uses_pm_role(monkeypatch, project_role):
    """A ``project_members`` row wins over the tenant fallback —
    ContextVar reflects the project-scoped role exactly."""
    async def _has_pm(sql, *args):
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-acme"})
        if "FROM project_members" in sql:
            return _Row({"role": project_role})
        raise AssertionError(
            "tenant_membership must not be consulted when pm row exists"
        )

    app = _build_harness(monkeypatch,
                         fetchrow_fn=_has_pm,
                         fake_user=_user(role="viewer", tid="t-acme"))
    res = _client(app).get(
        "/probe", headers={"X-Project-Id": "p-acme04"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ctx_project_id"] == "p-acme04"
    assert body["ctx_tenant_id"] == "t-acme"
    assert body["ctx_user_role"] == project_role


@pytest.mark.parametrize("tenant_role", ["owner", "admin"])
def test_tenant_owner_admin_falls_back_to_contributor(monkeypatch, tenant_role):
    """No ``project_members`` row + active tenant ``owner``/``admin``
    membership → effective project role is ``contributor`` (alembic
    0034 default-resolution rule)."""
    async def _fallback(sql, *args):
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-acme"})
        if "FROM project_members" in sql:
            return None
        if "FROM user_tenant_memberships" in sql:
            return _Row({"role": tenant_role, "status": "active"})
        raise AssertionError(f"unexpected SQL: {sql!r}")

    app = _build_harness(monkeypatch,
                         fetchrow_fn=_fallback,
                         fake_user=_user(role="viewer", tid="t-acme"))
    res = _client(app).get(
        "/probe", headers={"X-Project-Id": "p-acme05"},
    )
    assert res.status_code == 200
    assert res.json()["ctx_user_role"] == "contributor"


def test_tenant_id_header_matching_project_tenant_passes(monkeypatch):
    """When X-Tenant-Id agrees with the project's tenant the gate
    must pass and pin all three ContextVars."""
    async def _ok(sql, *args):
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-acme"})
        if "FROM project_members" in sql:
            return _Row({"role": "owner"})
        raise AssertionError(f"unexpected SQL: {sql!r}")

    app = _build_harness(monkeypatch,
                         fetchrow_fn=_ok,
                         fake_user=_user(role="viewer", tid="t-acme"))
    res = _client(app).get(
        "/probe",
        headers={
            "X-Project-Id": "p-acme06",
            "X-Tenant-Id": "t-acme",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ctx_tenant_id"] == "t-acme"
    assert body["ctx_project_id"] == "p-acme06"
    assert body["ctx_user_role"] == "owner"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (f) Self-fingerprint guard — pre-commit compat-grep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_self_fingerprint_clean():
    """The middleware function body must NOT contain the four compat
    fingerprints from implement_phase_step.md Step 3 — they are the
    most common forms of ported-from-SQLite cruft and have ambushed
    earlier ports (SP-5.6a). Mirrors the guard in
    test_db_rls_listener.py."""
    from backend import main
    src = inspect.getsource(main._project_header_gate)
    fingerprint = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    hits = fingerprint.findall(src)
    assert hits == [], f"compat fingerprint leaked into middleware: {hits}"
