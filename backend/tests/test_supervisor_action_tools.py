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
    def __init__(self, labels=None, assignee="bot-1", fail_clear=False,
                 status="To Do", read_status=200):
        self.labels = list(labels or [])
        self.assignee = assignee
        self.fail_clear = fail_clear
        self.status = status
        self.read_status = read_status   # HTTP status for label GETs (rank 2)
        self.comments = []

    async def _api(self, method, path, body=None):
        if method == "PUT" and path.endswith("/assignee"):
            if self.fail_clear:
                return (500, {})
            self.assignee = None
            return (204, {})
        if method == "PUT" and "?" not in path:  # full-issue edit (labels)
            body = body or {}
            update = body.get("update") or {}
            if "labels" in update:  # incremental add/remove ops (atomic path)
                for op in update["labels"]:
                    if "add" in op and op["add"] not in self.labels:
                        self.labels.append(op["add"])
                    if "remove" in op and op["remove"] in self.labels:
                        self.labels.remove(op["remove"])
            elif "labels" in (body.get("fields") or {}):  # full replacement
                self.labels = list(body["fields"]["labels"] or [])
            return (204, {})
        if method == "GET" and "fields=assignee" in path:
            # requeue reads assignee,status,labels together
            return (200, {"fields": {"assignee": self.assignee,
                                     "status": {"name": self.status},
                                     "labels": self.labels}})
        if method == "GET" and "fields=labels" in path:
            if not (200 <= self.read_status < 300):
                return (self.read_status, {})
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


def test_save_solution_not_model_callable_in_sora_action_set():
    # U6-0 step-0 containment (2026-07-11). The model-callable save_solution L3
    # write was REMOVED from Sora's live action set: it mints quality_score=1.0
    # from an UNVERIFIED, model-supplied gerrit_change_id into a GLOBAL,
    # tenant-less episodic_memory that is read back into prompts
    # (rag_prefetch / search_past_solutions) — a forgeable cross-user
    # memory-poisoning write-loop. The legitimate "remember a verified rescue"
    # write is unaffected: it is produced server-side on real Gerrit merge
    # (webhooks._save_merged_solution_to_l3). Re-binding a MODEL write here
    # requires the U6-0 provenance gate + fail-closed action-capability guard;
    # do NOT re-add save_solution to this set without them.
    assert "save_solution" not in {t.name for t in SORA_ACTION_TOOLS}


@pytest.mark.parametrize("bad", ["", "nope", "op- 5", "PROJ-1", "OP-", "drop table"])
def test_key_guard_refuses_non_op(bad):
    out = asyncio.run(supervisor_requeue_ticket.ainvoke({"ticket_key": bad}))
    assert out.startswith("[SUPERVISOR] refused")


def test_requeue_success_verified(monkeypatch):
    # To Do + a class:* label + assignee cleared → genuinely pickable
    _patch(monkeypatch, _FakeAdapter(assignee="bot-1", status="To Do",
                                     labels=["class:subscription-claude"]))
    out = asyncio.run(supervisor_requeue_ticket.ainvoke({"ticket_key": "OP-2530"}))
    assert out.startswith("[OK]") and "verified" in out and "can re-pick" in out
    assert "STILL NOT PICKABLE" not in out


def test_requeue_reports_still_not_pickable_on_residual_gate(monkeypatch):
    # audit r2 rank 3: assignee cleared but no class:* label (GATED) + wrong
    # status → must NOT claim 'runner can re-pick'; names the residual blockers.
    _patch(monkeypatch, _FakeAdapter(assignee="bot-1", status="In Progress", labels=[]))
    out = asyncio.run(supervisor_requeue_ticket.ainvoke({"ticket_key": "OP-2530"}))
    assert out.startswith("[OK]") and "STILL NOT PICKABLE" in out
    assert "status" in out.lower() and "class:*" in out   # both residual gates named


def test_requeue_reports_circuit_trip_as_blocker(monkeypatch):
    # audit r2 rank 3: a circuit-tripped ticket is still not pickable after requeue
    _patch(monkeypatch, _FakeAdapter(
        assignee="bot-1", status="To Do",
        labels=["class:subscription-claude", "runner-stoploss:circuit-tripped-20260101T0"]))
    out = asyncio.run(supervisor_requeue_ticket.ainvoke({"ticket_key": "OP-2530"}))
    assert "STILL NOT PICKABLE" in out and "circuit-tripped" in out


def test_requeue_fail_closed_on_bad_http(monkeypatch):
    _patch(monkeypatch, _FakeAdapter(assignee="bot-1", fail_clear=True))
    out = asyncio.run(supervisor_requeue_ticket.ainvoke({"ticket_key": "OP-2530"}))
    assert out.startswith("[FAILED]")


def test_strip_read_error_is_failed_not_false_ok(monkeypatch):
    # audit r2 rank 2: a 404/429/5xx on the label read must be [FAILED], never a
    # false '[OK] nothing to strip' on the exact wedged ticket this exists to fix.
    _patch(monkeypatch, _FakeAdapter(labels=["claim:x:1"], read_status=404))
    out = asyncio.run(supervisor_strip_stale_labels.ainvoke({"ticket_key": "OP-2530"}))
    assert out.startswith("[FAILED]") and "HTTP 404" in out
    assert "nothing to strip" not in out


def test_strip_labels_idempotent_noop(monkeypatch):
    _patch(monkeypatch, _FakeAdapter(labels=["area:backend", "type:feature"]))
    out = asyncio.run(supervisor_strip_stale_labels.ainvoke({"ticket_key": "OP-2530"}))
    assert out.startswith("[OK]") and "nothing to strip" in out


def test_strip_labels_removes_and_verifies(monkeypatch):
    # The REAL wedge labels: circuit-trip + revert history + stale claim fencing.
    # The coordinator's auto-managed runner-blocked:* markers are NOT stripped.
    fake = _FakeAdapter(labels=[
        "area:backend", "claim:claude-1:123",
        "runner-stoploss:circuit-tripped-20260101T000000",
        "runner-stoploss:revert-20260101T000000",
        "runner-blocked:waiting-OP-9",   # auto-managed → must survive
    ])
    _patch(monkeypatch, fake)
    out = asyncio.run(supervisor_strip_stale_labels.ainvoke({"ticket_key": "OP-2530"}))
    assert out.startswith("[OK]") and "verified" in out
    assert "circuit-trip" in out.lower() or "re-trip" in out.lower()   # trip caution shown
    assert sorted(fake.labels) == sorted(["area:backend", "runner-blocked:waiting-OP-9"])


def test_comment_requires_text(monkeypatch):
    _patch(monkeypatch, _FakeAdapter())
    out = asyncio.run(supervisor_comment_ticket.ainvoke({"ticket_key": "OP-2530", "text": "   "}))
    assert out.startswith("[SUPERVISOR] refused")


def test_comment_success_verified(monkeypatch):
    _patch(monkeypatch, _FakeAdapter())
    out = asyncio.run(supervisor_comment_ticket.ainvoke({"ticket_key": "OP-2530", "text": "rescued"}))
    assert out.startswith("[OK]") and "id=" in out
