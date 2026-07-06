"""Sora supervisor Line-A tools (supervisor roadmap).

Covers the four new tools + the enhanced failure hints:
  * supervisor_ticket_detail   — read + compact-format one ticket's state
  * supervisor_transition_ticket — act -> verify status; bad-target reject
  * supervisor_set_labels      — add/remove idempotent + verify
  * supervisor_guild_capabilities — non-empty guild listing
Plus the OP-* key guard on the action tools. Uses a stateful fake JIRA
adapter (no network). See backend/agents/tools.py.
"""

import asyncio

import pytest

import backend.jira_adapter as _ja
from backend.jira_adapter import DEFAULT_JIRA_STATUS_MAP
from backend.intent_source import IntentStatus
from backend.agents.tools import (
    SORA_ACTION_TOOLS,
    SUPERVISOR_OBSERVE_TOOLS,
    TOOL_MAP,
    supervisor_ticket_detail,
    supervisor_transition_ticket,
    supervisor_set_labels,
    supervisor_guild_capabilities,
)


class _FakeAdapter:
    def __init__(self, *, status="To Do", assignee="bot-1", labels=None,
                 issuelinks=None, comments=None, summary="Do the thing"):
        self.status = status
        self.assignee = assignee
        self.labels = list(labels or [])
        self.issuelinks = list(issuelinks or [])
        self.comments = list(comments or [])
        self.summary = summary

    # ── the write path the tool exercises ──
    def _status_name(self, status):
        return DEFAULT_JIRA_STATUS_MAP.get(status, status.value)

    async def update_status(self, ticket, status, *, comment=""):
        self.status = self._status_name(status)
        return {"ok": True, "jira_status": self.status}

    async def comment(self, ticket, body):
        cid = str(len(self.comments) + 1)
        self.comments.append({"id": cid, "author": {"displayName": "sora"},
                              "body": body})
        return {"id": cid}

    async def _api(self, method, path, body=None):
        if method == "GET" and path.endswith("/comment"):
            return (200, {"comments": self.comments})
        if method == "GET" and "fields=status" in path and "assignee" not in path:
            return (200, {"fields": {"status": {"name": self.status}}})
        if method == "GET" and "fields=labels" in path:
            return (200, {"fields": {"labels": self.labels}})
        if method == "GET":  # the big detail fetch (status,assignee,labels,...)
            assignee = ({"displayName": self.assignee} if self.assignee else None)
            return (200, {"fields": {
                "summary": self.summary,
                "status": {"name": self.status},
                "assignee": assignee,
                "labels": self.labels,
                "issuelinks": self.issuelinks,
            }})
        if method == "PUT" and path.endswith("/assignee"):
            self.assignee = None
            return (204, {})
        if method == "PUT":  # full-issue edit (labels)
            self.labels = list((body.get("fields") or {}).get("labels") or [])
            return (204, {})
        return (200, {})


def _patch(monkeypatch, fake):
    monkeypatch.setattr(_ja, "build_default_jira_adapter", lambda: fake)


# ── registration ──────────────────────────────────────────────────────
def test_new_tools_registered():
    for t in (supervisor_ticket_detail, supervisor_guild_capabilities):
        assert t in SUPERVISOR_OBSERVE_TOOLS and t.name in TOOL_MAP
    for t in (supervisor_transition_ticket, supervisor_set_labels):
        assert t in SORA_ACTION_TOOLS and t.name in TOOL_MAP


# ── ticket_detail ─────────────────────────────────────────────────────
def test_ticket_detail_reads_and_formats(monkeypatch):
    fake = _FakeAdapter(
        status="In Progress", assignee="alice",
        labels=["area:backend", "type:feature"],
        issuelinks=[
            {"type": {"name": "Blocks"}, "outwardIssue": {"key": "OP-99"}},
            {"type": {"name": "Blocks"}, "inwardIssue": {"key": "OP-77"}},
        ],
        comments=[{"id": "1", "author": {"displayName": "bob"},
                   "body": "looks stuck"}],
        summary="Wire the widget",
    )
    _patch(monkeypatch, fake)
    out = asyncio.run(supervisor_ticket_detail.ainvoke({"ticket_key": "OP-2530"}))
    assert out.startswith("[SUPERVISOR] OP-2530")
    assert "Wire the widget" in out
    assert "In Progress" in out and "alice" in out
    assert "area:backend" in out
    assert "OP-99" in out and "OP-77" in out          # blocks / blocked-by
    assert "bob" in out and "looks stuck" in out       # last comment


def test_ticket_detail_unassigned_and_no_comments(monkeypatch):
    _patch(monkeypatch, _FakeAdapter(assignee=None, comments=[]))
    out = asyncio.run(supervisor_ticket_detail.ainvoke({"ticket_key": "OP-1"}))
    assert "unassigned" in out
    assert "last comment: none" in out


def test_ticket_detail_rejects_non_op():
    out = asyncio.run(supervisor_ticket_detail.ainvoke({"ticket_key": "PROJ-1"}))
    assert out.startswith("[SUPERVISOR]") and "not an OP" in out


# ── transition_ticket ─────────────────────────────────────────────────
@pytest.mark.parametrize("target,expected", [
    ("todo", "To Do"),
    ("in_progress", "In Progress"),
    ("review", "In Review"),
    ("done", "Done"),
])
def test_transition_acts_and_verifies(monkeypatch, target, expected):
    fake = _FakeAdapter(status="Backlog")
    _patch(monkeypatch, fake)
    out = asyncio.run(supervisor_transition_ticket.ainvoke(
        {"ticket_key": "OP-2530", "target": target}))
    assert out.startswith("[OK]") and "verified" in out
    assert fake.status == expected


def test_transition_bad_target(monkeypatch):
    _patch(monkeypatch, _FakeAdapter())
    out = asyncio.run(supervisor_transition_ticket.ainvoke(
        {"ticket_key": "OP-2530", "target": "archived"}))
    assert out.startswith("[SUPERVISOR] refused") and "unknown target" in out


def test_transition_op_guard():
    out = asyncio.run(supervisor_transition_ticket.ainvoke(
        {"ticket_key": "nope", "target": "done"}))
    assert out.startswith("[SUPERVISOR] refused")


def test_transition_fail_closed_when_status_wrong(monkeypatch):
    class _Stuck(_FakeAdapter):
        async def update_status(self, ticket, status, *, comment=""):
            return {"ok": True}  # pretend success but DON'T move status
    _patch(monkeypatch, _Stuck(status="To Do"))
    out = asyncio.run(supervisor_transition_ticket.ainvoke(
        {"ticket_key": "OP-2530", "target": "done"}))
    assert out.startswith("[FAILED]")


# ── set_labels ────────────────────────────────────────────────────────
def test_set_labels_add_and_remove(monkeypatch):
    fake = _FakeAdapter(labels=["area:backend", "stoploss"])
    _patch(monkeypatch, fake)
    out = asyncio.run(supervisor_set_labels.ainvoke(
        {"ticket_key": "OP-2530", "add": ["needs-human"], "remove": ["stoploss"]}))
    assert out.startswith("[OK]") and "verified" in out
    assert "needs-human" in fake.labels
    assert "stoploss" not in fake.labels
    assert "area:backend" in fake.labels          # untouched kept


def test_set_labels_idempotent(monkeypatch):
    # adding an existing + removing an absent label = no-op, still verifies OK
    fake = _FakeAdapter(labels=["area:backend"])
    _patch(monkeypatch, fake)
    out = asyncio.run(supervisor_set_labels.ainvoke(
        {"ticket_key": "OP-2530", "add": ["area:backend"], "remove": ["ghost"]}))
    assert out.startswith("[OK]")
    assert fake.labels == ["area:backend"]


def test_set_labels_refuses_empty(monkeypatch):
    _patch(monkeypatch, _FakeAdapter())
    out = asyncio.run(supervisor_set_labels.ainvoke({"ticket_key": "OP-2530"}))
    assert out.startswith("[SUPERVISOR] refused")


def test_set_labels_op_guard():
    out = asyncio.run(supervisor_set_labels.ainvoke(
        {"ticket_key": "bad", "add": ["x"]}))
    assert out.startswith("[SUPERVISOR] refused")


# ── guild_capabilities ────────────────────────────────────────────────
def test_guild_capabilities_non_empty():
    out = asyncio.run(supervisor_guild_capabilities.ainvoke({}))
    assert out.startswith("[SUPERVISOR]")
    assert "guilds" in out.lower()
    # at least a couple of the real guild descriptions surface
    assert "Backend" in out and "-" in out
    assert len(out) > 100
