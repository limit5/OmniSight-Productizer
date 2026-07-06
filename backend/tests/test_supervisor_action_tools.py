"""Sora supervisor safe-action tools (P3, supervisor roadmap).

Reversible, self-verifying rescue actions. Covers: OP-* key guard, act→verify
success, fail-closed when verification fails, idempotent no-op, and the empty
comment guard. Uses a stateful fake JIRA adapter (no network).
See backend/agents/tools.py + docs/design/rpg/sora-supervisor-roadmap.md.
"""

import asyncio

import pytest

import backend.jira_adapter as _ja
from backend.agents.tools import (
    SORA_ACTION_TOOLS,
    TOOL_MAP,
    supervisor_requeue_ticket,
    supervisor_strip_stale_labels,
    supervisor_comment_ticket,
)


class _FakeAdapter:
    def __init__(self, labels=None, assignee="bot-1", fail_clear=False):
        self.labels = list(labels or [])
        self.assignee = assignee
        self.fail_clear = fail_clear
        self.comments = []

    async def _api(self, method, path, body=None):
        if method == "PUT" and path.endswith("/assignee"):
            if self.fail_clear:
                return (500, {})
            self.assignee = None
            return (204, {})
        if method == "PUT" and "?" not in path:  # full-issue edit (labels)
            self.labels = list((body.get("fields") or {}).get("labels") or [])
            return (204, {})
        if method == "GET" and "fields=assignee" in path:
            return (200, {"fields": {"assignee": self.assignee}})
        if method == "GET" and "fields=labels" in path:
            return (200, {"fields": {"labels": self.labels}})
        return (200, {})

    async def comment(self, ticket, body):
        cid = str(len(self.comments) + 1)
        self.comments.append((cid, body))
        return {"id": cid}


def _patch(monkeypatch, fake):
    monkeypatch.setattr(_ja, "build_default_jira_adapter", lambda: fake)


def test_all_action_tools_registered():
    for t in SORA_ACTION_TOOLS:
        assert t.name in TOOL_MAP


@pytest.mark.parametrize("bad", ["", "nope", "op- 5", "PROJ-1", "OP-", "drop table"])
def test_key_guard_refuses_non_op(bad):
    out = asyncio.run(supervisor_requeue_ticket.ainvoke({"ticket_key": bad}))
    assert out.startswith("[SUPERVISOR] refused")


def test_requeue_success_verified(monkeypatch):
    _patch(monkeypatch, _FakeAdapter(assignee="bot-1"))
    out = asyncio.run(supervisor_requeue_ticket.ainvoke({"ticket_key": "OP-2530"}))
    assert out.startswith("[OK]") and "verified" in out


def test_requeue_fail_closed_on_bad_http(monkeypatch):
    _patch(monkeypatch, _FakeAdapter(assignee="bot-1", fail_clear=True))
    out = asyncio.run(supervisor_requeue_ticket.ainvoke({"ticket_key": "OP-2530"}))
    assert out.startswith("[FAILED]")


def test_strip_labels_idempotent_noop(monkeypatch):
    _patch(monkeypatch, _FakeAdapter(labels=["area:backend", "type:feature"]))
    out = asyncio.run(supervisor_strip_stale_labels.ainvoke({"ticket_key": "OP-2530"}))
    assert out.startswith("[OK]") and "nothing to strip" in out


def test_strip_labels_removes_and_verifies(monkeypatch):
    fake = _FakeAdapter(labels=["area:backend", "claim:claude-1:123", "stoploss", "runner-blocked:x"])
    _patch(monkeypatch, fake)
    out = asyncio.run(supervisor_strip_stale_labels.ainvoke({"ticket_key": "OP-2530"}))
    assert out.startswith("[OK]") and "verified" in out
    assert fake.labels == ["area:backend"]  # stale gone, real kept


def test_comment_requires_text(monkeypatch):
    _patch(monkeypatch, _FakeAdapter())
    out = asyncio.run(supervisor_comment_ticket.ainvoke({"ticket_key": "OP-2530", "text": "   "}))
    assert out.startswith("[SUPERVISOR] refused")


def test_comment_success_verified(monkeypatch):
    _patch(monkeypatch, _FakeAdapter())
    out = asyncio.run(supervisor_comment_ticket.ainvoke({"ticket_key": "OP-2530", "text": "rescued"}))
    assert out.startswith("[OK]") and "id=" in out
