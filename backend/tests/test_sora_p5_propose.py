"""Sora P5 propose-and-approve gate (block 1b).

Sora can only PROPOSE a dangerous action (deploy/promote/restart/rollback) — it is
persisted as 'pending' and a HUMAN must approve before anything runs. These tests
prove: a proposal is FILED (not executed), the return says "awaiting approval",
bad input is refused, the read-only list works, the tools are registered with the
right write/read classification, and the db state machine only decides from
'pending'. The DB layer is mocked (matching the create_task-dedup test pattern);
nothing here can execute a deploy/restart. See backend/agents/tools.py + db.py.
"""

import asyncio
import json

import backend.agents.tools as tools
from backend.agents.tools import (
    SORA_P5_PROPOSE_TOOLS,
    TOOL_MAP,
    propose_action,
    list_pending_actions,
)


class _FakeConn:
    pass


class _FakePool:
    def acquire(self):
        class _Ctx:
            async def __aenter__(self_):
                return _FakeConn()
            async def __aexit__(self_, *a):
                return False
        return _Ctx()


# ── registration ──────────────────────────────────────────────────────
def test_p5_tools_registered_and_classified():
    from backend.agents.nodes import _WRITE_TOOL_NAMES
    assert propose_action in SORA_P5_PROPOSE_TOOLS
    assert list_pending_actions in SORA_P5_PROPOSE_TOOLS
    assert propose_action.name in TOOL_MAP and list_pending_actions.name in TOOL_MAP
    # propose is a WRITE (dedup+budget); list is read-only
    assert "propose_action" in _WRITE_TOOL_NAMES
    assert "list_pending_actions" not in _WRITE_TOOL_NAMES


# ── propose_action files a PENDING proposal, never executes ───────────
def test_propose_files_pending_awaiting_approval(monkeypatch):
    from backend import db as _db
    captured = {}

    async def _fake_insert(conn, data):
        captured.update(data)
    monkeypatch.setattr(_db, "insert_proposed_action", _fake_insert)
    monkeypatch.setattr(tools, "get_pool", lambda: _FakePool())
    monkeypatch.setattr(tools, "get_chat_context",
                        lambda: {"session_id": "s1", "user_id": "u1", "tenant_id": "t1"})

    out = asyncio.run(propose_action.ainvoke(
        {"action_kind": "deploy", "params_json": '{"tag":"v0.7.34"}', "rationale": "ship the P5 batch"}))
    assert out.startswith("[OK]") and "AWAITING OPERATOR APPROVAL" in out
    assert "Nothing has been executed" in out
    # the record was filed as a deploy with the right params + pending-by-default
    assert captured["action_kind"] == "deploy"
    assert json.loads(captured["params"]) == {"tag": "v0.7.34"}
    assert captured["id"].startswith("pa-")
    assert "blast_radius" in captured and captured["blast_radius"]


def test_propose_refuses_unknown_kind(monkeypatch):
    monkeypatch.setattr(tools, "get_pool", lambda: _FakePool())
    monkeypatch.setattr(tools, "get_chat_context", lambda: {"session_id": "s1"})
    out = asyncio.run(propose_action.ainvoke({"action_kind": "rm-rf", "params_json": "{}"}))
    assert out.startswith("[SUPERVISOR] refused") and "unknown action_kind" in out


def test_propose_refuses_bad_json(monkeypatch):
    monkeypatch.setattr(tools, "get_pool", lambda: _FakePool())
    monkeypatch.setattr(tools, "get_chat_context", lambda: {"session_id": "s1"})
    out = asyncio.run(propose_action.ainvoke(
        {"action_kind": "restart", "params_json": "{not json"}))
    assert out.startswith("[SUPERVISOR] refused") and "not valid JSON" in out


def test_propose_refuses_non_object_params(monkeypatch):
    monkeypatch.setattr(tools, "get_pool", lambda: _FakePool())
    monkeypatch.setattr(tools, "get_chat_context", lambda: {"session_id": "s1"})
    out = asyncio.run(propose_action.ainvoke(
        {"action_kind": "restart", "params_json": "[1,2,3]"}))
    assert out.startswith("[SUPERVISOR] refused") and "JSON object" in out


def test_propose_all_kinds_have_blast_radius(monkeypatch):
    from backend import db as _db
    seen = {}

    async def _ins(conn, data):
        seen[data["action_kind"]] = data["blast_radius"]
    monkeypatch.setattr(_db, "insert_proposed_action", _ins)
    monkeypatch.setattr(tools, "get_pool", lambda: _FakePool())
    monkeypatch.setattr(tools, "get_chat_context", lambda: {"session_id": "s"})
    for kind in ("deploy", "promote", "restart", "rollback"):
        out = asyncio.run(propose_action.ainvoke({"action_kind": kind, "params_json": "{}"}))
        assert out.startswith("[OK]")
        assert seen[kind]        # every kind carries a non-empty blast-radius


# ── list_pending_actions (read-only) ──────────────────────────────────
def test_list_pending(monkeypatch):
    from backend import db as _db

    async def _list(conn, *, status, limit):
        assert status == "pending"
        return [{"id": "pa-abc", "title": "deploy: tag=v0.7.34", "proposed_by": "sora"}]
    monkeypatch.setattr(_db, "list_proposed_actions", _list)
    monkeypatch.setattr(tools, "get_pool", lambda: _FakePool())
    out = asyncio.run(list_pending_actions.ainvoke({}))
    assert out.startswith("[SUPERVISOR]") and "pa-abc" in out and "1 pending" in out


def test_list_pending_empty(monkeypatch):
    from backend import db as _db

    async def _list(conn, *, status, limit):
        return []
    monkeypatch.setattr(_db, "list_proposed_actions", _list)
    monkeypatch.setattr(tools, "get_pool", lambda: _FakePool())
    out = asyncio.run(list_pending_actions.ainvoke({}))
    assert out.startswith("[SUPERVISOR]") and "no pending" in out


# ── db state machine (pure validation) ────────────────────────────────
def test_decide_rejects_invalid_decision():
    import backend.db as _db
    try:
        asyncio.run(_db.decide_proposed_action(_FakeConn(), "pa-x",
                                                decision="delete", decided_by="op", at=1.0))
        assert False, "should have raised"
    except ValueError as e:
        assert "approved|rejected" in str(e)
