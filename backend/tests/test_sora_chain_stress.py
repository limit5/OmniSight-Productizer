"""Sora supervisor CHAIN + RUNAWAY stress tests (audit round-2, 2026-07-06).

Unlike test_conversation_tool_rounds (fake stub tools), this drives the REAL
supervisor_* tools (from nodes.TOOL_MAP) through the real `_run_tool_rounds`
loop against a stateful in-memory fake JIRA adapter. It proves that a full
multi-step rescue keeps ticket state consistent AND that adversarial /
runaway turn shapes stay bounded (no 暴走) on the metered API.

Scenarios:
  * full rescue, model acting one-tool-per-round  → correct end state, verified
  * full rescue, model batching all tools in one round → same end state
  * a failing READ the model re-tries every round → bounded by the round cap
  * transition ping-pong → bounded, each move self-verified
  * write budget survives distinct create_task churn (cross-check)
See backend/agents/{nodes,tools}.py.
"""

import asyncio

import backend.jira_adapter as _ja
from backend.agents import nodes
from backend.agents.tools import supervisor_rescue_ticket


# ── stateful fake JIRA ────────────────────────────────────────────────
class _FakeJira:
    """Models ONE ticket's mutable state across the whole rescue chain."""

    _TRANSITIONS = {  # name -> (id, resulting status)  (this project's real flow)
        "進行中": ("21", "進行中"),
        "作業開始": ("31", "進行中"),
        "Won't Do": ("41", "Archived"),
        "Force Close": ("51", "Archived"),
    }

    def __init__(self, *, status="To Do", assignee="claude-bot",
                 labels=None, summary="stuck ticket"):
        self.status = status
        self.assignee = assignee
        self.labels = list(labels or [])
        self.summary = summary
        self.comments = []
        self.calls = []          # audit trail of (method, path)

    async def comment(self, ticket, body):
        cid = str(len(self.comments) + 1)
        self.comments.append({"id": cid, "author": {"displayName": "sora"}, "body": body})
        return {"id": cid}

    async def _api(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "GET" and path.endswith("/transitions"):
            return (200, {"transitions": [{"name": n, "id": i} for n, (i, _s) in self._TRANSITIONS.items()]})
        if method == "POST" and path.endswith("/transitions"):
            tid = ((body or {}).get("transition") or {}).get("id")
            for _n, (i, result) in self._TRANSITIONS.items():
                if i == tid:
                    self.status = result
                    return (204, {})
            return (400, {})
        if method == "GET" and "fields=status" in path and "assignee" not in path:
            return (200, {"fields": {"status": {"name": self.status}}})
        if method == "GET" and "fields=assignee" in path:
            # requeue now reads assignee,status,labels together — return all three.
            a = ({"displayName": self.assignee} if self.assignee else None)
            return (200, {"fields": {"assignee": a, "status": {"name": self.status},
                                     "labels": self.labels}})
        if method == "GET" and "fields=labels" in path:
            return (200, {"fields": {"labels": self.labels}})
        if method == "GET":  # big detail fetch
            a = ({"displayName": self.assignee} if self.assignee else None)
            return (200, {"fields": {
                "summary": self.summary, "status": {"name": self.status},
                "assignee": a, "labels": self.labels, "issuelinks": [],
            }})
        if method == "PUT" and path.endswith("/assignee"):
            self.assignee = None
            return (204, {})
        if method == "PUT":  # label edit (incremental update.labels ops)
            update = (body or {}).get("update") or {}
            for op in update.get("labels", []):
                if "add" in op and op["add"] not in self.labels:
                    self.labels.append(op["add"])
                if "remove" in op and op["remove"] in self.labels:
                    self.labels.remove(op["remove"])
            return (204, {})
        return (200, {})


class _Reply:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _ScriptLLM:
    """Tool-bound LLM: replays a scripted list of replies, one per invoke."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        return self._script.pop(0) if self._script else _Reply(content="done")


class _PlainLLM:
    def __init__(self):
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        return _Reply(content="（最終回覆）")


def _tc(name, args, cid):
    return {"name": name, "args": args, "id": cid}


def _drive(monkeypatch, fake, first, script, max_rounds=5):
    """Run the REAL tools through the real loop; only the JIRA adapter is faked."""
    monkeypatch.setattr(_ja, "build_default_jira_adapter", lambda: fake)
    monkeypatch.setattr(nodes, "emit_pipeline_phase", lambda *a, **k: None)
    monkeypatch.setattr(nodes, "emit_tool_progress", lambda *a, **k: None)
    llm_tools = _ScriptLLM(script)
    plain = _PlainLLM()
    resp = asyncio.run(nodes._run_tool_rounds(first, ["sys", "user"], llm_tools, plain, max_rounds))
    return resp, plain


# The REAL circuit-trip wedge label (runner_stoploss.TRIPPED_LABEL_PREFIX + ts).
TRIP = "runner-stoploss:circuit-tripped-20260101T000000"


# ── 1. full rescue, one tool per round ────────────────────────────────
def test_full_rescue_sequential_consistent_end_state(monkeypatch):
    fake = _FakeJira(status="To Do", assignee="claude-bot",
                     labels=["area:backend", "class:subscription-claude", TRIP, "claim:claude-1:123"])
    first = _Reply(tool_calls=[_tc("supervisor_ticket_detail", {"ticket_key": "OP-2600"}, "c1")])
    script = [
        _Reply(tool_calls=[_tc("supervisor_strip_stale_labels", {"ticket_key": "OP-2600"}, "c2")]),
        _Reply(tool_calls=[_tc("supervisor_requeue_ticket", {"ticket_key": "OP-2600"}, "c3")]),
        _Reply(tool_calls=[_tc("supervisor_comment_ticket",
                               {"ticket_key": "OP-2600", "text": "已解卡並重新排隊"}, "c4")]),
        _Reply(content="OP-2600 已解卡：清掉 circuit-trip/claim、清空指派、留言完成 ✅"),
    ]
    resp, _ = _drive(monkeypatch, fake, first, script)
    # end state fully consistent
    assert TRIP not in fake.labels and not any(l.startswith("claim:") for l in fake.labels)
    assert "area:backend" in fake.labels            # non-wedge label untouched
    assert "class:subscription-claude" in fake.labels  # class:* NOT stripped
    assert fake.assignee is None                    # re-queued
    assert len(fake.comments) == 1                  # exactly one comment
    assert "✅" in resp.content


# ── 2. full rescue, all tools batched in ONE round ────────────────────
def test_full_rescue_batched_one_round(monkeypatch):
    fake = _FakeJira(status="To Do", labels=["class:subscription-codex", TRIP])
    first = _Reply(tool_calls=[
        _tc("supervisor_strip_stale_labels", {"ticket_key": "OP-2601"}, "b1"),
        _tc("supervisor_requeue_ticket", {"ticket_key": "OP-2601"}, "b2"),
        _tc("supervisor_comment_ticket", {"ticket_key": "OP-2601", "text": "rescued"}, "b3"),
    ])
    script = [_Reply(content="OP-2601 一輪完成三步 ✅")]
    resp, _ = _drive(monkeypatch, fake, first, script)
    assert TRIP not in fake.labels                  # wedge label gone
    assert fake.labels == ["class:subscription-codex"]  # class:* kept (auto-managed markers untouched)
    assert fake.assignee is None
    assert len(fake.comments) == 1


# ── 3. a failing READ re-tried every round → bounded, no infinite loop ─
def test_failing_read_bounded_by_round_cap(monkeypatch):
    fake = _FakeJira()
    # model fixates on an invalid key → tool returns a [SUPERVISOR] refusal each
    # time; the loop must NOT run forever — it stops at the round cap and forces
    # a tool-free answer (paying at most max_rounds LLM calls, never infinite).
    bad = _tc("supervisor_ticket_detail", {"ticket_key": "NOT-A-KEY"}, "x")
    first = _Reply(tool_calls=[bad])
    script = [_Reply(tool_calls=[bad]) for _ in range(20)]
    resp, plain = _drive(monkeypatch, fake, first, script, max_rounds=4)
    assert resp.content == "（最終回覆）"           # forced tool-free answer
    assert plain.calls == 1                         # exactly one forced escape
    # the invalid key never even reached JIRA (guarded before any _api call)
    assert fake.calls == []


# ── 4. transition ping-pong → bounded + each move self-verified ───────
def test_transition_pingpong_bounded(monkeypatch):
    fake = _FakeJira(status="To Do")
    mv = _tc("supervisor_transition_ticket", {"ticket_key": "OP-2602", "target": "in_progress"}, "t")
    first = _Reply(tool_calls=[mv])
    # model keeps asking to transition every round → bounded by round cap
    script = [_Reply(tool_calls=[mv]) for _ in range(10)]
    resp, plain = _drive(monkeypatch, fake, first, script, max_rounds=3)
    assert resp.content == "（最終回覆）"
    assert fake.status == "進行中"                  # transition applied + verified
    # bounded: transitions POSTed at most once per round, not unbounded
    posts = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/transitions")]
    assert len(posts) <= 3


# ── 5. verify-integrity: a real requeue actually clears + confirms ────
def test_requeue_reports_ok_only_when_assignee_actually_cleared(monkeypatch):
    fake = _FakeJira(assignee="codex-bot")
    first = _Reply(tool_calls=[_tc("supervisor_requeue_ticket", {"ticket_key": "OP-2603"}, "r")])
    captured = {}

    class _Cap:
        def invoke(self, messages):
            captured["tool_msg"] = messages[-1].content
            return _Reply(content="done")

    monkeypatch.setattr(_ja, "build_default_jira_adapter", lambda: fake)
    monkeypatch.setattr(nodes, "emit_pipeline_phase", lambda *a, **k: None)
    monkeypatch.setattr(nodes, "emit_tool_progress", lambda *a, **k: None)
    asyncio.run(nodes._run_tool_rounds(first, ["sys", "user"], _Cap(), _PlainLLM(), 3))
    assert fake.assignee is None
    assert "[OK]" in captured["tool_msg"] and "verified" in captured["tool_msg"]


# ── 6. compound supervisor_rescue_ticket: whole rescue in ONE call ────
def test_compound_rescue_one_call_full_chain(monkeypatch):
    fake = _FakeJira(status="To Do", assignee="claude-bot",
                     labels=["area:backend", "class:subscription-claude", TRIP, "claim:codex-1:99"])
    monkeypatch.setattr(_ja, "build_default_jira_adapter", lambda: fake)
    out = asyncio.run(supervisor_rescue_ticket.ainvoke(
        {"ticket_key": "OP-2610", "comment_text": "operator rescue"}))
    assert out.startswith("[OK]")                      # one combined verdict
    assert "STILL NOT PICKABLE" not in out             # class:* present + To Do → pickable
    assert TRIP not in fake.labels
    assert not any(l.startswith("claim:") for l in fake.labels)
    assert "area:backend" in fake.labels               # non-wedge kept
    assert "class:subscription-claude" in fake.labels  # class:* kept
    assert fake.assignee is None                       # re-queued
    assert len(fake.comments) == 1                      # audit note posted


def test_compound_rescue_no_comment_when_empty(monkeypatch):
    fake = _FakeJira(status="To Do", assignee="bot", labels=["class:subscription-codex", TRIP])
    monkeypatch.setattr(_ja, "build_default_jira_adapter", lambda: fake)
    out = asyncio.run(supervisor_rescue_ticket.ainvoke({"ticket_key": "OP-2611"}))
    assert out.startswith("[OK]")
    assert fake.labels == ["class:subscription-codex"] and fake.assignee is None
    assert len(fake.comments) == 0                      # no comment requested


def test_compound_rescue_warns_still_not_pickable(monkeypatch):
    # Wedge cleared + assignee cleared, BUT no class:* label (GATED Story) → the
    # compound must honestly report STILL NOT PICKABLE, not a clean "rescued".
    fake = _FakeJira(status="To Do", assignee="bot", labels=[TRIP])   # no class:*
    monkeypatch.setattr(_ja, "build_default_jira_adapter", lambda: fake)
    out = asyncio.run(supervisor_rescue_ticket.ainvoke({"ticket_key": "OP-2613"}))
    assert out.startswith("[OK]") and "STILL NOT PICKABLE" in out
    assert "class:*" in out                             # names the residual gate
    assert TRIP not in fake.labels and fake.assignee is None  # un-wedge still succeeded


def test_compound_rescue_partial_failure_reports_failed(monkeypatch):
    # requeue fails (assignee clear returns 500) → the compound verdict is
    # [FAILED] and names the incomplete state, never a false [OK].
    class _RequeueFails(_FakeJira):
        async def _api(self, method, path, body=None):
            if method == "PUT" and path.endswith("/assignee"):
                return (500, {})
            return await super()._api(method, path, body)
    fake = _RequeueFails(status="To Do", assignee="bot", labels=[TRIP])
    monkeypatch.setattr(_ja, "build_default_jira_adapter", lambda: fake)
    out = asyncio.run(supervisor_rescue_ticket.ainvoke({"ticket_key": "OP-2612"}))
    assert out.startswith("[FAILED]") and "INCOMPLETE" in out
    assert fake.labels == []                            # strip still happened
    assert fake.assignee == "bot"                       # requeue did NOT stick


def test_compound_rescue_op_guard():
    out = asyncio.run(supervisor_rescue_ticket.ainvoke({"ticket_key": "bad"}))
    assert out.startswith("[SUPERVISOR] refused")


def test_compound_rescue_registered():
    from backend.agents.tools import SORA_ACTION_TOOLS, TOOL_MAP
    from backend.agents.nodes import _WRITE_TOOL_NAMES
    assert supervisor_rescue_ticket in SORA_ACTION_TOOLS
    assert supervisor_rescue_ticket.name in TOOL_MAP
    # must be budgeted/deduped as a WRITE
    assert supervisor_rescue_ticket.name in _WRITE_TOOL_NAMES
