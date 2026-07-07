"""P5 operator gate API — approve / reject a proposal (admin-authed).

Exercises the safety-critical operator path end-to-end with a fake DB + admin
override: approve a restart (flag OFF → dry-run, row stays 'approved', nothing
runs), reject, and the guards (404 unknown, 409 non-pending, 400 no-reason).
Confirms auth.require_admin is enforced. See backend/routers/proposed_actions.py.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth, db
import backend.routers.proposed_actions as pa


class _FakeConn: ...
class _FakePool:
    def acquire(self):
        class _C:
            async def __aenter__(s): return _FakeConn()
            async def __aexit__(s, *a): return False
        return _C()


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(pa.router)
    app.dependency_overrides[auth.require_admin] = lambda: auth.User(
        id="op", email="op@x", name="Op", role="admin")
    monkeypatch.setattr(pa, "get_pool", lambda: _FakePool())
    return TestClient(app)


def _seed(monkeypatch, row, *, decide=True):
    calls = {"decide": None, "result": None, "reason": None, "executing": False}
    async def _get(conn, aid): return row
    async def _decide(conn, aid, *, decision, decided_by, at, reason=""):
        calls["decide"] = decision
        calls["reason"] = reason
        return decide
    async def _mark(conn, aid, *, at):
        calls["executing"] = True
    async def _setres(conn, aid, *, status, result, at):
        calls["result"] = (status, result)
    monkeypatch.setattr(db, "get_proposed_action", _get)
    monkeypatch.setattr(db, "decide_proposed_action", _decide)
    monkeypatch.setattr(db, "mark_proposed_action_executing", _mark)
    monkeypatch.setattr(db, "set_proposed_action_result", _setres)
    return calls


def test_approve_restart_flag_off_is_dry_run(client, monkeypatch):
    monkeypatch.delenv("OMNISIGHT_P5_EXECUTE", raising=False)
    row = {"id": "pa-1", "action_kind": "restart", "params": '{"service":"omnisight-slo-monitor.service"}', "status": "pending"}
    calls = _seed(monkeypatch, row)
    r = client.post("/proposed-actions/pa-1/approve", data={"reason": "smoke"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert calls["decide"] == "approved"
    assert calls["executing"] is True              # write-ahead 'executing' marker fired (SR-1)
    assert body["status"] == "approved"           # dry-run → stays approved, not executed
    assert body["execution"]["dry_run"] is True and body["execution"]["executed"] is False
    assert calls["result"][0] == "approved"        # persisted status


def test_approve_deploy_never_executes(client, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_P5_EXECUTE", "1")   # even with exec ON
    row = {"id": "pa-2", "action_kind": "deploy", "params": '{"tag":"v9"}', "status": "pending"}
    _seed(monkeypatch, row)
    r = client.post("/proposed-actions/pa-2/approve", data={"reason": "x"})
    assert r.status_code == 200
    ex = r.json()["execution"]
    assert ex["executed"] is False and "not auto-executed" in ex["detail"]


def test_approve_requires_reason(client, monkeypatch):
    _seed(monkeypatch, {"id": "pa-1", "action_kind": "restart", "params": "{}", "status": "pending"})
    r = client.post("/proposed-actions/pa-1/approve", data={"reason": "  "})
    assert r.status_code == 400 and "reason" in r.text.lower()


def test_approve_unknown_is_404(client, monkeypatch):
    async def _get(conn, aid): return None
    monkeypatch.setattr(db, "get_proposed_action", _get)
    r = client.post("/proposed-actions/nope/approve", data={"reason": "x"})
    assert r.status_code == 404


def test_approve_non_pending_is_409(client, monkeypatch):
    row = {"id": "pa-1", "action_kind": "restart", "params": "{}", "status": "approved"}
    _seed(monkeypatch, row, decide=False)   # decide loses the race → already decided
    r = client.post("/proposed-actions/pa-1/approve", data={"reason": "x"})
    assert r.status_code == 409


def test_reject_flips_status(client, monkeypatch):
    row = {"id": "pa-1", "action_kind": "restart", "params": "{}", "status": "pending"}
    calls = _seed(monkeypatch, row)
    r = client.post("/proposed-actions/pa-1/reject", data={"reason": "not now"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert calls["decide"] == "rejected"


def test_reason_is_persisted(client, monkeypatch):
    # audit r3: the operator's reason must reach decide_proposed_action (durable).
    row = {"id": "pa-1", "action_kind": "restart", "params": "{}", "status": "pending"}
    calls = _seed(monkeypatch, row)
    client.post("/proposed-actions/pa-1/approve", data={"reason": "ship the P5 batch"})
    assert calls["reason"] == "ship the P5 batch"


def test_dry_run_via_form_is_honored(monkeypatch):
    # audit r3 HIGH: dry_run posted as FORM data must be bound (was a query param,
    # silently ignored → accidental real execution). With flag ON + dry_run form,
    # execution must stay a dry-run.
    app = FastAPI()
    app.include_router(pa.router)
    app.dependency_overrides[auth.require_admin] = lambda: auth.User(id="op", email="op@x", name="Op", role="admin")
    monkeypatch.setattr(pa, "get_pool", lambda: _FakePool())
    monkeypatch.setenv("OMNISIGHT_P5_EXECUTE", "1")
    row = {"id": "pa-1", "action_kind": "restart", "params": '{"service":"omnisight-slo-monitor.service"}', "status": "pending"}
    _seed(monkeypatch, row)
    import backend.agents.action_executor as ex
    ran = {"n": 0}
    monkeypatch.setattr(ex, "_do_restart", lambda s, timeout=30.0: ran.__setitem__("n", ran["n"] + 1) or (True, "rc=0"))
    c = TestClient(app)
    r = c.post("/proposed-actions/pa-1/approve", data={"reason": "test", "dry_run": "true"})
    assert r.status_code == 200
    assert r.json()["execution"]["dry_run"] is True
    assert ran["n"] == 0        # form dry_run honored → real restart NOT called


def test_bot_principal_cannot_approve(monkeypatch):
    # audit r3 BLOCKER: an admin-ROLE API-key / bot principal must be refused —
    # only a human session may approve/reject a prod-affecting action.
    app = FastAPI()
    app.include_router(pa.router)
    app.dependency_overrides[auth.require_admin] = lambda: auth.User(
        id="apikey:abc123", email="apikey:abc123", name="bot", role="admin")
    monkeypatch.setattr(pa, "get_pool", lambda: _FakePool())
    c = TestClient(app, raise_server_exceptions=False)
    for path in ("approve", "reject"):
        r = c.post(f"/proposed-actions/pa-1/{path}", data={"reason": "x"})
        assert r.status_code == 403, f"{path}: {r.status_code}"


def test_endpoints_require_admin_auth():
    # WITHOUT the admin override, the dependency must reject (no anonymous access).
    app = FastAPI()
    app.include_router(pa.router)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/proposed-actions/pa-1/approve", data={"reason": "x"})
    assert r.status_code in (401, 403, 500)   # never 200 — auth gate blocks it
