"""Y5 (#281) row 5 — end-to-end project isolation acceptance tests.

This is the *capstone* test row for the Y5 milestone.  Rows 1-4 each
landed a separate piece of the project-aware multi-tenant authorisation
stack:

  * row 1 — ``backend/db_context.py`` extends the ContextVar triple
    to ``(tenant_id, project_id, user_role)``.
  * row 2 — ``backend.auth.require_project_member`` factory pins the
    triple from URL-path ``{project_id}`` after a membership lookup.
  * row 3 — ``backend.db_rls_listener`` rewrites every executed SQL
    statement to inject ``WHERE tenant_id = '<t>' AND
    (project_id = '<p>' OR project_id IS NULL)`` and auto-fills
    ``(tenant_id, project_id)`` on INSERTs.
  * row 4 — ``_project_header_gate`` middleware in ``backend.main``
    plus the ``X-Project-Id`` header in ``lib/api.ts`` provide a
    second, defence-in-depth gate at the HTTP boundary.

Row 5 is the *acceptance suite* — it stitches those four pieces
together and pins the three headline contracts the Y5 milestone is
meant to deliver:

  (1) **Cross-project isolation within the same tenant**
      A ``project A`` user (or an actor whose ContextVar is pinned
      to project A) MUST NOT see ``project B`` rows even if they
      share a tenant.  This is the row 3 listener doing its job.

  (2) **Super-admin cross-project escape valve + audit trail**
      A ``super_admin`` actor bypasses the project filter (the
      ``BYPASS_SUPER_ADMIN`` token in the listener), AND when the
      operator declares cross-project intent via the
      ``X-Admin-Cross-Project: 1`` header the middleware
      writes a single ``audit_log`` row scoped to the *target*
      tenant's chain.  The header is the operator's "I know I'm
      crossing project boundaries" signal; the audit row gives
      compliance an immutable record of every such crossing.

  (3) **NULL ``project_id`` legacy fallthrough**
      Rows inserted before alembic 0038 backfilled ``project_id``
      may carry NULL in that column during the deliberately
      observed release window between 0038 landing and the future
      ``ALTER … SET NOT NULL`` revision.  A modern (project-aware)
      reader on the same tenant MUST still see those legacy rows
      so the cutover does not silently drop pre-Y1 data.

The tests run on in-memory SQLite (rows 1 + 3 + 5) and a
fake-pool ASGI harness (row 4 + row 5 audit).  No PG live needed —
the listener and middleware contracts are at the SQL-string and
HTTP-header layer respectively, both cross-dialect.
"""

from __future__ import annotations

import inspect
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures — wipe ContextVars between tests so cross-test pollution
#  cannot mask a missing-tenant bug.  Mirrors the row 3 / row 4 guard.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture(autouse=True)
def _reset_db_context_vars():
    from backend import db_context
    db_context.set_tenant_id(None)
    db_context.set_project_id(None)
    db_context.set_user_role(None)
    yield
    db_context.set_tenant_id(None)
    db_context.set_project_id(None)
    db_context.set_user_role(None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared in-memory SQLite engine seeded with cross-project /
#  cross-tenant / NULL-project-id artefact rows.  The three contracts
#  below all assert against this fixture's row set.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
def isolated_artifacts_engine():
    """In-memory SQLite + listener attached + ``artifacts`` rows that
    cover every Y5 row 5 acceptance angle:

      tenant   project          payload
      ──────  ──────────────── ────────────────────────────────────
      t-acme  p-acme-frontend  acme-frontend-secret   ← project A
      t-acme  p-acme-backend   acme-backend-secret    ← project B (same tenant)
      t-acme  NULL             acme-legacy-pre-0038   ← legacy fallthrough
      t-acme  p-acme-frontend  acme-frontend-other    ← second project A row
      t-other p-other-default  other-tenant-secret    ← cross-tenant blast radius
      t-other NULL             other-tenant-legacy    ← legacy is tenant-scoped too

    The full table is 6 rows.  Each contract's "expected payloads"
    set is a deliberate subset — that's the whole point of the test:
    pin which rows leak across which boundary.
    """
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import create_engine, text
    from backend.db_rls_listener import install_project_rls_listener

    engine = create_engine("sqlite://")
    install_project_rls_listener(engine)

    seed = (
        ("acme-frontend-secret",  "t-acme",  "p-acme-frontend"),
        ("acme-backend-secret",   "t-acme",  "p-acme-backend"),
        ("acme-legacy-pre-0038",  "t-acme",  None),
        ("acme-frontend-other",   "t-acme",  "p-acme-frontend"),
        ("other-tenant-secret",   "t-other", "p-other-default"),
        ("other-tenant-legacy",   "t-other", None),
    )

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE artifacts ("
            "  id INTEGER PRIMARY KEY,"
            "  payload TEXT,"
            "  tenant_id TEXT,"
            "  project_id TEXT"
            ")"
        ))
        for payload, tid, pid in seed:
            pid_lit = "NULL" if pid is None else f"'{pid}'"
            conn.execute(text(
                "INSERT INTO artifacts (payload, tenant_id, project_id) "
                f"VALUES ('{payload}', '{tid}', {pid_lit})"
            ))
    return engine


def _select_payloads(engine, *, tenant_id, project_id, user_role):
    """Pin the ContextVar triple, run a bare ``SELECT payload`` through
    the listener-wrapped engine, return payloads as a set."""
    from sqlalchemy import text
    from backend import db_context

    db_context.set_tenant_id(tenant_id)
    db_context.set_project_id(project_id)
    db_context.set_user_role(user_role)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT payload FROM artifacts")).fetchall()
    return {r[0] for r in rows}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Contract (1) — same-tenant cross-project isolation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_project_a_user_cannot_see_project_b_artifact_same_tenant(
    isolated_artifacts_engine,
):
    """The headline Y5 contract: a project-A actor's SELECT on
    ``artifacts`` MUST NOT return any row whose ``project_id`` matches
    a different project, even when the tenant column matches.  This is
    the row-3 listener appending ``AND project_id = 'p-acme-frontend'``
    to the WHERE."""
    payloads = _select_payloads(
        isolated_artifacts_engine,
        tenant_id="t-acme",
        project_id="p-acme-frontend",
        user_role="contributor",
    )
    # The two project-A rows + the legacy NULL-pid row come through.
    assert "acme-frontend-secret" in payloads
    assert "acme-frontend-other" in payloads
    # The same-tenant cross-project row MUST NOT.
    assert "acme-backend-secret" not in payloads


def test_project_a_user_does_not_see_other_tenant_rows(
    isolated_artifacts_engine,
):
    """Sanity follow-up: cross-tenant rows are also masked.  Rows 1-2
    of the listener already cover this but the row-5 capstone pins it
    so a future weakening of the listener cannot silently widen the
    blast radius."""
    payloads = _select_payloads(
        isolated_artifacts_engine,
        tenant_id="t-acme",
        project_id="p-acme-frontend",
        user_role="contributor",
    )
    assert "other-tenant-secret" not in payloads
    assert "other-tenant-legacy" not in payloads


def test_project_b_user_symmetrically_cannot_see_project_a_rows(
    isolated_artifacts_engine,
):
    """Mirror image of the headline test — pinning the same actor
    in the *other* project flips which rows are masked.  Rules out
    a hard-coded ``project_id == 'p-acme-frontend'`` short-circuit
    that would silently let project-A always see itself."""
    payloads = _select_payloads(
        isolated_artifacts_engine,
        tenant_id="t-acme",
        project_id="p-acme-backend",
        user_role="contributor",
    )
    assert "acme-backend-secret" in payloads
    assert "acme-frontend-secret" not in payloads
    assert "acme-frontend-other" not in payloads


def test_project_a_user_select_includes_project_filter_in_sql(
    isolated_artifacts_engine,
):
    """Belt-and-braces inspection of the rewritten SQL — even when the
    fixture rows happen to align with the listener's filter, the
    rewrite itself must contain both clauses.  Catches an accidental
    drop of one clause that would only surface as a leak in tests where
    the legacy NULL row happens to be empty."""
    from backend.db_rls_listener import apply_project_rls

    rew = apply_project_rls(
        "SELECT * FROM artifacts",
        tenant_id="t-acme",
        project_id="p-acme-frontend",
        user_role="contributor",
    )
    assert rew.applied is True
    assert rew.bypass_reason is None
    # Tenant filter present.
    assert "tenant_id = 't-acme'" in rew.rewritten_query
    # Project filter with the NULL fallthrough arm present.
    assert "project_id = 'p-acme-frontend'" in rew.rewritten_query
    assert "project_id IS NULL" in rew.rewritten_query


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Contract (2) — super-admin bypass + X-Admin-Cross-Project audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_super_admin_role_bypasses_listener_filter(
    isolated_artifacts_engine,
):
    """When the ``user_role`` ContextVar is pinned to ``super_admin``,
    the listener short-circuits with ``BYPASS_SUPER_ADMIN`` — the
    SELECT runs unfiltered and returns every row across every tenant
    and every project.  This is the existing row 3 contract; row 5
    pins it so future tightening (e.g. require X-Admin-Cross-Project
    at the listener layer) does not regress the bypass intent."""
    from backend.db_rls_listener import (
        BYPASS_SUPER_ADMIN,
        apply_project_rls,
    )

    rew = apply_project_rls(
        "SELECT * FROM artifacts",
        tenant_id="t-acme",
        project_id="p-acme-frontend",
        user_role="super_admin",
    )
    assert rew.applied is False
    assert rew.bypass_reason == BYPASS_SUPER_ADMIN
    # Sanity: the rewriter literally returns the original string.
    assert rew.rewritten_query == "SELECT * FROM artifacts"

    # And the e2e SELECT mirrors that — the super-admin sees every
    # payload, including the cross-tenant rows.
    payloads = _select_payloads(
        isolated_artifacts_engine,
        tenant_id="t-acme",
        project_id="p-acme-frontend",
        user_role="super_admin",
    )
    assert payloads == {
        "acme-frontend-secret",
        "acme-backend-secret",
        "acme-legacy-pre-0038",
        "acme-frontend-other",
        "other-tenant-secret",
        "other-tenant-legacy",
    }


# Test-side fakes for the middleware ASGI harness — same shape as
# row 4's harness so the contracts compose cleanly.

@dataclass
class _Row:
    data: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]


class _FakeConn:
    def __init__(self, fetchrow_fn):
        self._fn = fetchrow_fn

    async def fetchrow(self, sql: str, *args: Any):
        return await self._fn(sql, *args)


class _FakePool:
    def __init__(self, fetchrow_fn):
        self._fn = fetchrow_fn

    @asynccontextmanager
    async def acquire(self):
        yield _FakeConn(self._fn)


def _user(role="viewer", *, uid="u-test", tid="t-test"):
    from backend import auth as _auth
    return _auth.User(
        id=uid, email=f"{uid}@x.com", name=uid,
        role=role, enabled=True, tenant_id=tid,
    )


def _build_audit_harness(monkeypatch, *, fetchrow_fn, fake_user,
                         audit_capture):
    """Spin up a tiny Starlette app behind the row-4 middleware with
    ``backend.audit.log`` monkeypatched to append into ``audit_capture``
    instead of touching PG.  Returns the app and a ``TestClient``."""
    from backend import auth as _auth
    from backend import audit as _audit
    from backend import db_context, main

    db_context.set_tenant_id(None)
    db_context.set_project_id(None)
    db_context.set_user_role(None)

    monkeypatch.setattr(_auth, "auth_mode", lambda: "session")

    @dataclass
    class _Sess:
        token: str
        user_id: str

    async def _fake_get_session(_cookie):
        return _Sess(token="tok-fake", user_id=fake_user.id)

    async def _fake_get_user(_uid):
        return fake_user

    monkeypatch.setattr(_auth, "get_session", _fake_get_session)
    monkeypatch.setattr(_auth, "get_user", _fake_get_user)

    fake_pool = _FakePool(fetchrow_fn)
    monkeypatch.setattr("backend.db_pool.get_pool", lambda: fake_pool)

    async def _capture_audit_log(*args, **kwargs):
        audit_capture.append(kwargs)
        return 1

    monkeypatch.setattr(_audit, "log", _capture_audit_log)

    async def _probe(request: Request) -> JSONResponse:
        return JSONResponse({
            "ctx_tenant_id": db_context.current_tenant_id(),
            "ctx_project_id": db_context.current_project_id(),
            "ctx_user_role": db_context.current_user_role(),
        })

    app = Starlette(routes=[Route("/probe", _probe)])
    app.middleware("http")(main._project_header_gate)
    return app


def test_super_admin_with_intent_header_emits_audit_log(monkeypatch):
    """When a super-admin sends ``X-Project-Id`` together with the
    explicit ``X-Admin-Cross-Project: 1`` intent header the middleware
    pins the ContextVar triple AND writes a single audit row.  The
    audit row is what compliance / security review look at to confirm
    every cross-project access by a privileged actor is recorded."""
    captured: list[dict] = []

    async def _fetchrow(sql, *args):
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-target"})
        raise AssertionError(
            f"super-admin path must not consult membership: {sql!r}"
        )

    app = _build_audit_harness(
        monkeypatch,
        fetchrow_fn=_fetchrow,
        fake_user=_user(role="super_admin", uid="u-platform-sa", tid="t-platform"),
        audit_capture=captured,
    )

    res = TestClient(app, cookies={"omnisight_session": "tok"}).get(
        "/probe",
        headers={
            "X-Project-Id": "p-target-prod",
            "X-Admin-Cross-Project": "1",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ctx_tenant_id"] == "t-target"
    assert body["ctx_project_id"] == "p-target-prod"
    assert body["ctx_user_role"] == "super_admin"

    assert len(captured) == 1
    row = captured[0]
    assert row["action"] == "super_admin_cross_project_access"
    assert row["entity_kind"] == "project"
    assert row["entity_id"] == "p-target-prod"
    assert row["actor"] == "u-platform-sa"
    after = row["after"]
    assert after["tenant_id"] == "t-target"
    assert after["project_id"] == "p-target-prod"
    assert after["intent_header"] == "X-Admin-Cross-Project: 1"
    assert after["method"] == "GET"
    assert after["path"] == "/probe"


def test_super_admin_without_intent_header_skips_audit(monkeypatch):
    """The audit row is the *intent-declaration* receipt — without the
    ``X-Admin-Cross-Project: 1`` header the middleware MUST NOT fire
    audit.log (otherwise every health probe by a super-admin would
    spam the chain).  The bypass itself still runs at the listener
    layer for any subsequent SQL."""
    captured: list[dict] = []

    async def _fetchrow(sql, *args):
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-target"})
        raise AssertionError("unexpected SQL")

    app = _build_audit_harness(
        monkeypatch,
        fetchrow_fn=_fetchrow,
        fake_user=_user(role="super_admin", uid="u-sa", tid="t-platform"),
        audit_capture=captured,
    )

    res = TestClient(app, cookies={"omnisight_session": "tok"}).get(
        "/probe",
        headers={"X-Project-Id": "p-target-prod"},
    )
    assert res.status_code == 200
    assert res.json()["ctx_user_role"] == "super_admin"
    assert captured == []


def test_intent_header_without_super_admin_does_not_emit_audit(monkeypatch):
    """Defence in depth: a non-super-admin user sending the
    ``X-Admin-Cross-Project: 1`` header alone MUST NOT trigger an
    audit row.  The header is a *declaration* not an *authorisation*;
    the authorisation comes from being super-admin.  A regular user
    falls through the membership chain unchanged."""
    captured: list[dict] = []

    async def _fetchrow(sql, *args):
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-acme"})
        if "FROM project_members" in sql:
            return _Row({"role": "viewer"})
        raise AssertionError(f"unexpected SQL: {sql!r}")

    app = _build_audit_harness(
        monkeypatch,
        fetchrow_fn=_fetchrow,
        fake_user=_user(role="viewer", tid="t-acme"),
        audit_capture=captured,
    )

    res = TestClient(app, cookies={"omnisight_session": "tok"}).get(
        "/probe",
        headers={
            "X-Project-Id": "p-acme01",
            "X-Admin-Cross-Project": "1",
        },
    )
    assert res.status_code == 200
    assert res.json()["ctx_user_role"] == "viewer"
    assert captured == []


def test_audit_row_lands_in_target_tenant_chain(monkeypatch):
    """audit.log uses ``tenant_insert_value()`` which reads
    ``current_tenant_id()`` — so the audit row MUST land in the
    *target* tenant's chain, not the super-admin's home tenant.
    This is what lets a per-tenant compliance dashboard surface
    "all super-admin crossings into our tenant".

    The test pins this by checking that the ContextVar is set to the
    target tenant *before* audit.log fires — we capture the
    ``current_tenant_id()`` value at the moment of the audit call.
    """
    captured: list[dict] = []
    seen_tenant_at_audit: list[str | None] = []

    async def _fetchrow(sql, *args):
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-target"})
        raise AssertionError("unexpected SQL")

    from backend import auth as _auth
    from backend import audit as _audit
    from backend import db_context, main

    db_context.set_tenant_id(None)
    db_context.set_project_id(None)
    db_context.set_user_role(None)

    monkeypatch.setattr(_auth, "auth_mode", lambda: "session")

    @dataclass
    class _Sess:
        token: str
        user_id: str

    async def _fake_get_session(_cookie):
        return _Sess(token="tok-x", user_id="u-sa")

    async def _fake_get_user(_uid):
        return _user(role="super_admin", uid="u-sa", tid="t-platform")

    monkeypatch.setattr(_auth, "get_session", _fake_get_session)
    monkeypatch.setattr(_auth, "get_user", _fake_get_user)
    monkeypatch.setattr(
        "backend.db_pool.get_pool", lambda: _FakePool(_fetchrow),
    )

    async def _capture(*args, **kwargs):
        # Snapshot the contextvar at the moment audit.log was called.
        seen_tenant_at_audit.append(db_context.current_tenant_id())
        captured.append(kwargs)
        return 1

    monkeypatch.setattr(_audit, "log", _capture)

    async def _probe(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/probe", _probe)])
    app.middleware("http")(main._project_header_gate)

    res = TestClient(app, cookies={"omnisight_session": "tok"}).get(
        "/probe",
        headers={
            "X-Project-Id": "p-target-x",
            "X-Admin-Cross-Project": "1",
        },
    )
    assert res.status_code == 200
    assert seen_tenant_at_audit == ["t-target"]
    assert captured[0]["entity_id"] == "p-target-x"


@pytest.mark.parametrize("intent_value", [
    # Only the literal "1" triggers the audit.  Anything else (typoed,
    # falsey-looking, or a probe to enumerate the audit surface) falls
    # through silently.  Pinning this matrix prevents accidental
    # widening to e.g. "true" / "yes" — which would let a
    # value-injection probe trigger audit-log spam.
    "0",
    "true",
    "yes",
    "True",
    "1 ",     # trailing space — header values are matched verbatim
    " 1",
    "01",
    "",
])
def test_intent_header_only_literal_one_triggers_audit(
    monkeypatch, intent_value
):
    captured: list[dict] = []

    async def _fetchrow(sql, *args):
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-target"})
        raise AssertionError("unexpected SQL")

    app = _build_audit_harness(
        monkeypatch,
        fetchrow_fn=_fetchrow,
        fake_user=_user(role="super_admin", uid="u-sa", tid="t-platform"),
        audit_capture=captured,
    )

    res = TestClient(app, cookies={"omnisight_session": "tok"}).get(
        "/probe",
        headers={
            "X-Project-Id": "p-target-y",
            "X-Admin-Cross-Project": intent_value,
        },
    )
    assert res.status_code == 200
    assert captured == [], (
        f"intent_value {intent_value!r} should not trigger audit"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Contract (3) — NULL project_id legacy fallthrough
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_legacy_null_project_id_visible_to_modern_reader(
    isolated_artifacts_engine,
):
    """The headline NULL-project compatibility contract: a project-A
    actor's SELECT MUST return rows whose ``project_id`` is NULL on
    the same tenant — those are pre-0038 rows that haven't been
    backfilled yet, and silently dropping them would lose data
    during the cutover window.

    Mechanism: row 3 listener appends
    ``AND (project_id = '<p>' OR project_id IS NULL)`` not just
    ``project_id = '<p>'``.  This test pins the OR arm explicitly."""
    payloads = _select_payloads(
        isolated_artifacts_engine,
        tenant_id="t-acme",
        project_id="p-acme-frontend",
        user_role="contributor",
    )
    assert "acme-legacy-pre-0038" in payloads


def test_legacy_null_visibility_is_tenant_scoped(
    isolated_artifacts_engine,
):
    """A project-A actor on tenant ``t-acme`` MUST NOT see legacy
    NULL-project rows belonging to *another* tenant.  The OR arm
    relaxes the project filter, but the tenant filter is non-
    negotiable — otherwise a tenant-A user would see tenant-B's
    pre-0038 rows."""
    payloads = _select_payloads(
        isolated_artifacts_engine,
        tenant_id="t-acme",
        project_id="p-acme-frontend",
        user_role="contributor",
    )
    assert "other-tenant-legacy" not in payloads


def test_legacy_null_only_visible_to_correct_tenant(
    isolated_artifacts_engine,
):
    """Mirror image of the previous test — pinning the actor on
    ``t-other`` must surface ``other-tenant-legacy`` and mask
    ``acme-legacy-pre-0038``.  Pins the symmetry of the tenant filter
    so a hard-coded ``t-acme`` short-circuit cannot pass silently."""
    payloads = _select_payloads(
        isolated_artifacts_engine,
        tenant_id="t-other",
        project_id="p-other-default",
        user_role="contributor",
    )
    assert "other-tenant-legacy" in payloads
    assert "acme-legacy-pre-0038" not in payloads


def test_legacy_null_visible_even_in_a_fresh_project_with_no_named_rows(
    isolated_artifacts_engine,
):
    """Edge case: the actor is pinned to a *fresh* project on the
    tenant that has no named rows of its own (e.g. just provisioned).
    The legacy NULL-pid row of the same tenant must STILL be visible
    so the new project doesn't observe an artificially empty table
    on its first read.

    This is the exact "release window" scenario the row 3 docstring
    calls out — alembic 0038 is in production but the
    ``ALTER … SET NOT NULL`` revision hasn't landed yet."""
    payloads = _select_payloads(
        isolated_artifacts_engine,
        tenant_id="t-acme",
        project_id="p-acme-fresh-clean",
        user_role="contributor",
    )
    assert payloads == {"acme-legacy-pre-0038"}


def test_legacy_null_path_present_in_select_rewrite():
    """Pin the literal SQL fragment that delivers the NULL fallthrough
    so a future "tighten the rewrite" change can't silently drop the
    OR arm and observe the same payload set in tests where the legacy
    row happens to be empty."""
    from backend.db_rls_listener import apply_project_rls

    rew = apply_project_rls(
        "SELECT * FROM artifacts",
        tenant_id="t-acme",
        project_id="p-acme-frontend",
        user_role="contributor",
    )
    # The OR arm is the backward-compat clause for legacy rows.
    assert "OR project_id IS NULL" in rew.rewritten_query


def test_legacy_null_insert_does_not_resurface_after_listener_autofill(
    isolated_artifacts_engine,
):
    """The auto-fill INSERT (row 3 contract) populates ``project_id``
    from context — so a NEW row written through the listener never
    enters the legacy NULL bucket.  The legacy fallthrough is for
    pre-0038 rows ONLY; if a new caller forgets ``project_id`` the
    listener fixes it.  Pins this so a regression that disables
    auto-fill cannot silently re-create the NULL-pid bucket."""
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import text
    from backend import db_context

    db_context.set_tenant_id("t-acme")
    db_context.set_project_id("p-acme-frontend")
    db_context.set_user_role("contributor")

    with isolated_artifacts_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO artifacts (payload) VALUES ('post-0038-write')"
        ))

    # A reader on a *different* project of the same tenant MUST NOT
    # see this row (it would only show up if the listener silently
    # left project_id = NULL).
    db_context.set_tenant_id("t-acme")
    db_context.set_project_id("p-acme-backend")
    db_context.set_user_role("contributor")
    with isolated_artifacts_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT payload FROM artifacts")
        ).fetchall()
    payloads = {r[0] for r in rows}
    assert "post-0038-write" not in payloads


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cross-cutting: middleware + listener compose without conflict
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_middleware_pins_contextvar_for_listener_consumption(monkeypatch):
    """End-to-end wiring sanity: after row 4 middleware passes, all
    three ContextVars are pinned to values row 3 listener will read.
    A "headers received → contextvars set → listener filters" round-
    trip is the central Y5 contract; this test is the smoke test for
    that wiring at the middleware boundary.  No DB / audit involved."""
    async def _fetchrow(sql, *args):
        if "FROM projects" in sql:
            return _Row({"id": args[0], "tenant_id": "t-acme"})
        if "FROM project_members" in sql:
            return _Row({"role": "viewer"})
        raise AssertionError(f"unexpected SQL: {sql!r}")

    captured: list[dict] = []
    app = _build_audit_harness(
        monkeypatch,
        fetchrow_fn=_fetchrow,
        fake_user=_user(role="viewer", tid="t-acme"),
        audit_capture=captured,
    )

    res = TestClient(app, cookies={"omnisight_session": "tok"}).get(
        "/probe",
        headers={"X-Project-Id": "p-acme01"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ctx_tenant_id"] == "t-acme"
    assert body["ctx_project_id"] == "p-acme01"
    assert body["ctx_user_role"] == "viewer"
    # Regular viewer — no audit row.
    assert captured == []


def test_audit_action_token_matches_listener_bypass_reason():
    """The middleware-side audit ``action`` and the listener-side
    ``bypass_reason`` together form the cross-project access trail.
    Pin the literal token strings against each other so a one-sided
    rename can't silently desync the two layers (an investigator
    grepping ``audit_log`` for ``super_admin_cross_project_access``
    plus the structured log line emitting ``BYPASS_SUPER_ADMIN``
    must always converge on the same actor)."""
    from backend import main
    from backend.db_rls_listener import BYPASS_SUPER_ADMIN

    # The audit action literal lives inside the middleware source.
    src = inspect.getsource(main._project_header_gate)
    assert "super_admin_cross_project_access" in src
    # And the bypass token is the listener's correspondent.  They
    # share a "super_admin" stem on purpose — that's the breadcrumb
    # an SRE follows from the audit row to the structured log.
    assert "super_admin" in BYPASS_SUPER_ADMIN


def test_intent_header_literal_appears_in_middleware_source():
    """Pin the literal ``X-Admin-Cross-Project`` header name in the
    middleware so a future case-sensitivity or rename refactor MUST
    update the test side along with the prod side."""
    from backend import main
    src = inspect.getsource(main._project_header_gate)
    # Header name appears twice: once in the .get() call and once in
    # the audit row's ``intent_header`` field.
    assert src.count("x-admin-cross-project") >= 1
    assert "X-Admin-Cross-Project: 1" in src


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Self-fingerprint — pre-commit compat-grep clean
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_super_admin_audit_block_has_no_compat_fingerprint():
    """Per implement_phase_step.md Step 3 pre-commit grep: the
    super-admin / audit branch I just added in the middleware must
    not contain any of the four compat fingerprints (``_conn()`` /
    ``await conn.commit()`` / ``datetime('now')`` / ``VALUES (?,
    ...)``).  Mirrors row 4's self-fingerprint guard but scoped to
    just the super-admin audit hunk so we can extend the middleware
    elsewhere without retripping this test."""
    from backend import main

    src = inspect.getsource(main._project_header_gate)
    # Slice down to just the super-admin branch — that's the new
    # surface for row 5.  The branch starts at the
    # ``if _auth.role_at_least(user.role, "super_admin"):`` line and
    # ends before the next ``# Resolution chain`` comment.
    start = src.find('if _auth.role_at_least(user.role, "super_admin"):')
    end = src.find("# Resolution chain", start)
    assert start != -1 and end != -1, "super-admin branch markers moved"
    branch_src = src[start:end]
    fingerprint = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    hits = fingerprint.findall(branch_src)
    assert hits == [], (
        f"compat fingerprint leaked into super-admin audit branch: {hits}"
    )
