"""OP-2620 — chat-path provenance capture plumbing (dormant).

The LangGraph conversation tool loop seals the model-input frame once per
response, threads its snapshot id to every chat action guard call in that
round, and records tool results for the next round's frame. Provenance remains
best-effort: an absent scope or seal failure degrades to empty ids without
breaking the chat turn.

Offline — the LLM/tool fakes mirror ``test_nodes_action_guard.py`` and cache
resolution mirrors ``test_provenance_runner_plumbing.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.agents import nodes
from backend.agents.action_guard import GuardOutcome
from backend.agents.provenance import (
    TOOL_RESULT,
    ProvenanceCollector,
    ProvenanceSnapshot,
    active_collector,
    provenance_scope,
)
from backend.agents.state import GraphState


class _Reply:
    def __init__(self, content: str = "", tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class _ScriptedLLM:
    """Return the next scripted reply per invoke, then a plain answer."""

    def __init__(self, script: list[_Reply] | None = None) -> None:
        self._script = list(script or [])

    def invoke(self, _convo):
        if self._script:
            return self._script.pop(0)
        return _Reply(content="done")


class _FakeTool:
    """Minimal TOOL_MAP entry that returns a canned result."""

    def __init__(self, result: str = "[OK] done") -> None:
        self.calls: list[dict] = []
        self._result = result

    async def ainvoke(self, args: dict) -> str:
        self.calls.append(args)
        return self._result


def _cache():
    return nodes._SNAPSHOT_CACHE


def _patch_chat_harness(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(nodes, "TOOL_MAP", {"dummy_tool": _FakeTool()})
    monkeypatch.setattr(nodes, "emit_pipeline_phase", lambda *a, **k: None)
    monkeypatch.setattr(nodes, "emit_tool_progress", lambda *a, **k: None)
    recorded: list[dict[str, Any]] = []

    def _recorder(**kwargs) -> GuardOutcome:
        recorded.append(kwargs)
        return GuardOutcome(
            proceed=True,
            decision=None,
            family="code_write",
            mode="shadow",
        )

    monkeypatch.setattr(nodes, "guard_tool_dispatch", _recorder)
    return recorded


def _tool_reply(call_id: str) -> _Reply:
    return _Reply(
        tool_calls=[
            {"name": "dummy_tool", "args": {"value": call_id}, "id": call_id}
        ]
    )


@pytest.mark.asyncio
async def test_chat_snapshot_id_is_threaded_to_guard(monkeypatch) -> None:
    recorded = _patch_chat_harness(monkeypatch)

    with provenance_scope():
        result = await nodes._run_tool_rounds(
            _tool_reply("c1"),
            ["sys", "user"],
            _ScriptedLLM([_Reply(content="done")]),
            _ScriptedLLM(),
        )

    assert result.content == "done"
    assert len(recorded) == 1
    ids = recorded[0]["provenance_snapshot_ids"]
    assert isinstance(ids, tuple) and len(ids) == 1
    assert ids[0].startswith("psnap-")
    assert isinstance(_cache().get(ids[0]), ProvenanceSnapshot)


@pytest.mark.asyncio
async def test_chat_per_round_snapshots_are_distinct_and_temporal(
    monkeypatch,
) -> None:
    recorded = _patch_chat_harness(monkeypatch)
    scripted = _ScriptedLLM([_tool_reply("c2"), _Reply(content="done")])

    with provenance_scope():
        result = await nodes._run_tool_rounds(
            _tool_reply("c1"), ["sys", "user"], scripted, _ScriptedLLM()
        )

    assert result.content == "done"
    assert len(recorded) == 2
    (round1_id,), (round2_id,) = [
        call["provenance_snapshot_ids"] for call in recorded
    ]
    assert round1_id != round2_id
    snap1 = _cache().get(round1_id)
    snap2 = _cache().get(round2_id)
    assert isinstance(snap1, ProvenanceSnapshot)
    assert isinstance(snap2, ProvenanceSnapshot)
    assert not any(record.source_kind == TOOL_RESULT for record in snap1.records)
    assert any(
        record.source_kind == TOOL_RESULT and record.source_id == "c1"
        for record in snap2.records
    )


@pytest.mark.asyncio
async def test_chat_without_provenance_scope_uses_empty_ids(monkeypatch) -> None:
    recorded = _patch_chat_harness(monkeypatch)

    result = await nodes._run_tool_rounds(
        _tool_reply("c1"),
        ["sys", "user"],
        _ScriptedLLM([_Reply(content="done")]),
        _ScriptedLLM(),
    )

    assert result.content == "done"
    assert recorded[0]["provenance_snapshot_ids"] == ()


@pytest.mark.asyncio
async def test_chat_seal_failure_degrades_to_empty_ids_and_turn_completes(
    monkeypatch,
) -> None:
    recorded = _patch_chat_harness(monkeypatch)

    def _boom(self, cache, *, extra_omissions=()):  # noqa: ANN001
        raise RuntimeError("seal exploded")

    monkeypatch.setattr(ProvenanceCollector, "seal", _boom)

    with provenance_scope():
        result = await nodes._run_tool_rounds(
            _tool_reply("c1"),
            ["sys", "user"],
            _ScriptedLLM([_Reply(content="done")]),
            _ScriptedLLM(),
        )

    assert result.content == "done"
    assert recorded[0]["provenance_snapshot_ids"] == ()


@pytest.mark.asyncio
async def test_chat_pipeline_establishes_and_resets_provenance_scope(
    monkeypatch,
) -> None:
    from backend.routers import chat as chat_router

    observed = {"active_during_run": False}

    async def _stub(user_msg: str, **kwargs) -> GraphState:
        del kwargs
        observed["active_during_run"] = active_collector() is not None
        return GraphState(
            user_command=user_msg,
            routed_to="general",
            answer="ok",
        )

    monkeypatch.setattr(chat_router, "run_graph", _stub)
    monkeypatch.setattr(chat_router, "emit_pipeline_phase", lambda *a, **k: None)
    monkeypatch.setattr(chat_router, "add_system_log", lambda *a, **k: None)

    assert active_collector() is None
    result = await chat_router._run_pipeline("hello")

    assert result.content == "ok"
    assert observed["active_during_run"]
    assert active_collector() is None
