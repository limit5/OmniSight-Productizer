"""Bounded multi-round tool loop for Sora's conversational turn (Line-A #1).

Covers `_run_tool_rounds`: observe→act→verify chaining across rounds,
create_task dedup (no double-file), the round cap + forced tool-free answer,
and that a no-tool reply passes straight through. Uses fake LLM/tool objects.
See backend/agents/nodes.py.
"""

import asyncio

import backend.agents.tools as _tools_mod
from backend.agents import nodes


class _Reply:
    """Stand-in for an LLM assistant message."""

    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _ToolsLLM:
    """Fake tool-bound LLM: returns a scripted sequence of replies per invoke."""

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
        return _Reply(content="final answer")


def _fake_tool(name, ret):
    async def _run(_args):
        _run.count += 1
        return ret
    _run.count = 0

    class _T:
        pass
    t = _T()
    t.name = name
    t.ainvoke = _run
    return t


def _run(resp, llm_tools, llm, tool_map, monkeypatch, max_rounds=3):
    monkeypatch.setattr(nodes, "TOOL_MAP", tool_map)
    monkeypatch.setattr(nodes, "emit_pipeline_phase", lambda *a, **k: None)
    monkeypatch.setattr(nodes, "emit_tool_progress", lambda *a, **k: None)
    return asyncio.run(nodes._run_tool_rounds(resp, ["sys", "user"], llm_tools, llm, max_rounds))


def test_no_tools_passes_through(monkeypatch):
    r = _run(_Reply(content="hi"), _ToolsLLM([]), _PlainLLM(), {}, monkeypatch)
    assert r.content == "hi"


def test_multi_round_observe_then_act_then_verify(monkeypatch):
    detail = _fake_tool("supervisor_ticket_detail", "[SUPERVISOR] OP-1 To Do unassigned")
    requeue = _fake_tool("supervisor_requeue_ticket", "[OK] OP-1 re-queued (verified)")
    tool_map = {"supervisor_ticket_detail": detail, "supervisor_requeue_ticket": requeue}
    # round1: observe → round2: act → round3: no tools (final)
    script = [
        _Reply(tool_calls=[{"name": "supervisor_requeue_ticket", "args": {"ticket_key": "OP-1"}, "id": "c2"}]),
        _Reply(content="OP-1 已重新排隊 ✅"),
    ]
    first = _Reply(tool_calls=[{"name": "supervisor_ticket_detail", "args": {"ticket_key": "OP-1"}, "id": "c1"}])
    r = _run(first, _ToolsLLM(script), _PlainLLM(), tool_map, monkeypatch)
    assert r.content == "OP-1 已重新排隊 ✅"
    assert detail.ainvoke.count == 1 and requeue.ainvoke.count == 1


def test_create_task_deduped_no_double_file(monkeypatch):
    ct = _fake_tool("create_task", "[OK] filed OP-999 (GATED)")
    tool_map = {"create_task": ct}
    # LLM re-emits the SAME create_task twice (rounds 1 & 2), then answers.
    same = {"name": "create_task", "args": {"title": "X"}, "id": "c"}
    script = [_Reply(tool_calls=[same]), _Reply(content="filed once")]
    first = _Reply(tool_calls=[same])
    r = _run(first, _ToolsLLM(script), _PlainLLM(), tool_map, monkeypatch)
    assert r.content == "filed once"
    assert ct.ainvoke.count == 1  # deduped — filed exactly once despite 2 requests


def test_round_cap_forces_tool_free_answer(monkeypatch):
    obs = _fake_tool("supervisor_quota_status", "[SUPERVISOR] all closed")
    tool_map = {"supervisor_quota_status": obs}
    call = {"name": "supervisor_quota_status", "args": {}, "id": "c"}
    # LLM ALWAYS asks for a tool → must hit the cap and get forced to answer.
    llm_tools = _ToolsLLM([_Reply(tool_calls=[call]) for _ in range(10)])
    plain = _PlainLLM()
    first = _Reply(tool_calls=[call])
    r = _run(first, llm_tools, plain, tool_map, monkeypatch, max_rounds=3)
    assert r.content == "final answer"       # forced tool-free summary
    assert plain.calls == 1                  # exactly one forced answer
    assert obs.ainvoke.count == 3            # ran once per capped round, no more


# ── audit hardening (2026-07-06): timeout / cap / malformed / LLM-fail ──

def test_tool_timeout_does_not_hang(monkeypatch):
    """A tool that hangs is aborted after TOOL_TIMEOUT_S, not forever."""
    async def _hang(_args):
        await asyncio.sleep(60)  # would hang the turn without the timeout
    class _T: pass
    t = _T(); t.name = "supervisor_quota_status"; t.ainvoke = _hang
    tool_map = {"supervisor_quota_status": t}
    monkeypatch.setattr(nodes, "TOOL_TIMEOUT_S", 0.05)
    call = {"name": "supervisor_quota_status", "args": {}, "id": "c"}
    first = _Reply(tool_calls=[call])
    # after the timeout, the model gives a plain answer
    r = _run(first, _ToolsLLM([_Reply(content="handled timeout")]), _PlainLLM(),
             tool_map, monkeypatch)
    assert r.content == "handled timeout"


def test_too_many_calls_per_round_capped(monkeypatch):
    obs = _fake_tool("supervisor_quota_status", "[SUPERVISOR] ok")
    tool_map = {"supervisor_quota_status": obs}
    # 20 tool calls in ONE round → only MAX_TOOL_CALLS_PER_ROUND (8) run.
    calls = [{"name": "supervisor_quota_status", "args": {"i": i}, "id": f"c{i}"}
             for i in range(20)]
    first = _Reply(tool_calls=calls)
    _run(first, _ToolsLLM([_Reply(content="done")]), _PlainLLM(), tool_map, monkeypatch)
    assert obs.ainvoke.count == 8            # capped, not 20


def test_malformed_tool_call_skipped(monkeypatch):
    obs = _fake_tool("supervisor_quota_status", "[SUPERVISOR] ok")
    tool_map = {"supervisor_quota_status": obs}
    # one good call + two malformed (no name / non-dict args) in the round.
    calls = [
        {"name": "supervisor_quota_status", "args": {}, "id": "good"},
        {"name": "", "args": {}, "id": "noname"},
        {"name": "supervisor_quota_status", "args": ["not", "a", "dict"], "id": "badargs"},
    ]
    first = _Reply(tool_calls=calls)
    r = _run(first, _ToolsLLM([_Reply(content="ok")]), _PlainLLM(), tool_map, monkeypatch)
    assert r.content == "ok"
    assert obs.ainvoke.count == 1            # only the well-formed call ran


def test_llm_invoke_failure_returns_graceful_message(monkeypatch):
    obs = _fake_tool("supervisor_quota_status", "[SUPERVISOR] ok")
    tool_map = {"supervisor_quota_status": obs}

    class _BoomLLM:
        def invoke(self, _m):
            raise RuntimeError("api 500")

    call = {"name": "supervisor_quota_status", "args": {}, "id": "c"}
    first = _Reply(tool_calls=[call])
    r = _run(first, _BoomLLM(), _PlainLLM(), tool_map, monkeypatch)
    # never raises out of the turn — returns a graceful assistant message
    assert obs.ainvoke.count == 1
    assert "停在這裡" in r.content or "問題" in r.content


def test_tool_output_truncated(monkeypatch):
    big = "x" * 20000
    obs = _fake_tool("supervisor_quota_status", big)
    tool_map = {"supervisor_quota_status": obs}
    seen = {}

    class _Capture:
        def invoke(self, messages):
            seen["last"] = messages[-1].content       # the ToolMessage we appended
            return _Reply(content="done")

    call = {"name": "supervisor_quota_status", "args": {}, "id": "c"}
    first = _Reply(tool_calls=[call])
    _run(first, _Capture(), _PlainLLM(), tool_map, monkeypatch)
    assert len(seen["last"]) < len(big)
    assert seen["last"].endswith("…[truncated]")


def test_write_budget_caps_distinct_writes(monkeypatch):
    """Distinct writes (not deduped) are still bounded by the write budget."""
    filed = _fake_tool("create_task", "[OK] filed")
    tool_map = {"create_task": filed}
    monkeypatch.setattr(nodes, "_WRITE_TOOL_NAMES", frozenset({"create_task"}), raising=False)
    # Each round files a DIFFERENT task (distinct args → not deduped). Budget=12.
    script = [_Reply(tool_calls=[{"name": "create_task", "args": {"n": i}, "id": f"c{i}"}])
              for i in range(1, 30)]
    first = _Reply(tool_calls=[{"name": "create_task", "args": {"n": 0}, "id": "c0"}])
    _run(first, _ToolsLLM(script), _PlainLLM(), tool_map, monkeypatch, max_rounds=30)
    assert filed.ainvoke.count == 12         # write budget enforced
