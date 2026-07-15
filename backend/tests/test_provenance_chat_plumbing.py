"""Chat-path provenance capture plumbing.

The LangGraph conversation tool loop seals the model-input frame once per
response, threads the whole ModelSnapshot to every chat action guard call in
that round, and records tool results for the next round's frame. Provenance
remains best-effort: an absent scope or seal failure degrades to
CaptureUnavailable without breaking the chat turn.

OP-2664 records classification-gated RAG snippets as untrusted input provenance
without changing the retrieved context or offline answer.

Offline — the LLM/tool fakes mirror ``test_nodes_action_guard.py`` and cache
resolution mirrors ``test_provenance_runner_plumbing.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend import rag
from backend.agents import nodes, provenance
from backend.agents.action_guard import GuardOutcome
from backend.agents.provenance import (
    Attestation,
    CaptureUnavailable,
    ModelSnapshot,
    RAG_DOC,
    TOOL_RESULT,
    ProvenanceCollector,
    ProvenanceSnapshot,
    active_collector,
    digest,
    provenance_scope,
)
from backend.agents.state import GraphState
from backend.llm_adapter import HumanMessage


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


def _patch_offline_rag(monkeypatch: pytest.MonkeyPatch) -> list[rag.Hit]:
    hits = [
        rag.Hit(
            doc_path="docs/operator-guide.md",
            title="Operator guide",
            snippet="Use the guarded operator workflow.",
            score=2.0,
        ),
        rag.Hit(
            doc_path="docs/recovery.md",
            title="Recovery",
            snippet="Verify the rollback before reporting success.",
            score=1.0,
        ),
    ]
    monkeypatch.setattr(nodes, "_get_llm", lambda **kw: None)
    monkeypatch.setattr(nodes, "_build_state_summary", lambda: "stable state")
    monkeypatch.setattr(nodes, "emit_pipeline_phase", lambda *a, **k: None)
    monkeypatch.setattr(rag, "retrieve", lambda *a, **k: hits)
    return hits


def _rag_state() -> GraphState:
    return GraphState(messages=[HumanMessage(content="How do I recover?")])


@pytest.mark.asyncio
async def test_chat_whole_model_snapshot_is_threaded_to_guard(monkeypatch) -> None:
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
    turn_provenance = recorded[0]["turn_provenance"]
    assert isinstance(turn_provenance, ModelSnapshot)
    snapshot_id = turn_provenance.snapshot.snapshot_id
    assert snapshot_id.startswith("psnap-")
    assert isinstance(_cache().get(snapshot_id), ProvenanceSnapshot)


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
    round1, round2 = [call["turn_provenance"] for call in recorded]
    assert isinstance(round1, ModelSnapshot)
    assert isinstance(round2, ModelSnapshot)
    round1_id = round1.snapshot.snapshot_id
    round2_id = round2.snapshot.snapshot_id
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
async def test_chat_without_provenance_scope_passes_capture_unavailable(
    monkeypatch,
) -> None:
    recorded = _patch_chat_harness(monkeypatch)

    result = await nodes._run_tool_rounds(
        _tool_reply("c1"),
        ["sys", "user"],
        _ScriptedLLM([_Reply(content="done")]),
        _ScriptedLLM(),
    )

    assert result.content == "done"
    assert recorded[0]["turn_provenance"] == CaptureUnavailable("no_scope")


@pytest.mark.asyncio
async def test_chat_seal_failure_passes_capture_unavailable_and_turn_completes(
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
    assert recorded[0]["turn_provenance"] == CaptureUnavailable("seal_failed")


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


@pytest.mark.asyncio
async def test_conversation_rag_records_each_retrieved_doc_as_unattested(
    monkeypatch,
) -> None:
    hits = _patch_offline_rag(monkeypatch)

    with provenance_scope() as col:
        result = await nodes.conversation_node(_rag_state())

    assert result["answer"].startswith("[OFFLINE]")
    assert len(col._records) == 2
    for record, hit in zip(col._records, hits, strict=True):
        assert record.source_kind == RAG_DOC
        assert record.source_id == hit.doc_path
        assert record.attestation == Attestation.UNATTESTED
        assert record.content_digest == digest(hit.snippet)


@pytest.mark.asyncio
async def test_conversation_rag_without_provenance_scope_is_noop(monkeypatch) -> None:
    hits = _patch_offline_rag(monkeypatch)
    observed_collectors = []
    original_record_content = nodes.record_content

    def _observe_collector(collector, *args, **kwargs):  # noqa: ANN001
        observed_collectors.append(collector)
        return original_record_content(collector, *args, **kwargs)

    monkeypatch.setattr(nodes, "record_content", _observe_collector)

    assert active_collector() is None
    result = await nodes.conversation_node(_rag_state())

    assert result["answer"].startswith("[OFFLINE]")
    assert observed_collectors == [None] * len(hits)
    assert active_collector() is None


@pytest.mark.asyncio
async def test_conversation_rag_capture_is_behavior_neutral(monkeypatch) -> None:
    hits = _patch_offline_rag(monkeypatch)
    expected_block = rag.format_hits_for_prompt(hits)

    without_scope = await nodes.conversation_node(_rag_state())
    with provenance_scope():
        with_scope = await nodes.conversation_node(_rag_state())

    assert expected_block in without_scope["answer"]
    assert with_scope["answer"] == without_scope["answer"]


@pytest.mark.asyncio
async def test_conversation_rag_capture_failure_is_latched_and_turn_completes(
    monkeypatch,
) -> None:
    hits = _patch_offline_rag(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("injected RAG record failure")

    monkeypatch.setattr(provenance, "untrusted_record", _boom)

    with provenance_scope() as col:
        result = await nodes.conversation_node(_rag_state())

    assert rag.format_hits_for_prompt(hits) in result["answer"]
    assert col._failed is True
    assert "record_error" in col._omissions
