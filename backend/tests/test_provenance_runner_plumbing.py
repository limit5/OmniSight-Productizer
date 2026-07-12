"""OP-2617 — U6-0 T5b-1a: runner-path provenance capture plumbing (dormant).

``run_with_tools`` seals one per-turn ProvenanceSnapshot immediately
before each model call and threads its id to the dispatcher (→ the T7
action guard's ``provenance_snapshot_ids`` passthrough). Locks:

  * per-turn seal + thread-to-dispatcher (a ``psnap-`` id per dispatch,
    distinct per turn, resolvable in the process-local cache);
  * the §2.E temporal rule — turn N's tool result enters turn N+1's
    snapshot, never turn N's own;
  * stale-refresh capture — an injected refresh frame is recorded so the
    next seal carries it;
  * per-invocation scope isolation across concurrent asyncio tasks;
  * the HARD RULE — provenance is best-effort: a seal failure degrades to
    ``provenance_snapshot_ids=()`` and never aborts the run.

Still dormant end-to-end: nothing CONSUMES the ids until T9/T10.
Offline — mirrors ``test_execution_context_plumbing.py``'s stub-SDK
harness.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from backend.agents import action_guard, tool_dispatcher
from backend.agents.provenance import (
    STALE_REFRESH,
    TOOL_RESULT,
    ProvenanceCollector,
    ProvenanceSnapshot,
    active_collector,
)
from backend.agents.tool_dispatcher import ToolDispatcher, ToolResult


# ─── Stub Anthropic SDK shape (per-client response iterators) ─────────


class _Resp:
    def __init__(self, content: list[dict[str, Any]], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = None


def _tool_use_resp(
    name: str = "Read", tool_input: dict[str, Any] | None = None, tid: str = "tu_1"
) -> _Resp:
    return _Resp(
        content=[
            {"type": "tool_use", "id": tid, "name": name, "input": tool_input or {}}
        ],
        stop_reason="tool_use",
    )


def _end_resp(text: str = "done") -> _Resp:
    return _Resp(content=[{"type": "text", "text": text}], stop_reason="end_turn")


class _Messages:
    def __init__(self, responses: list[_Resp]) -> None:
        self._it = iter(responses)

    def create(self, **kwargs: Any) -> _Resp:
        del kwargs
        return next(self._it)


class _FakeSDKClient:
    def __init__(self, responses: list[_Resp]) -> None:
        self.messages = _Messages(responses)
        self.beta = None


class _RecordingDispatcher:
    """Duck-typed dispatcher: records the provenance_snapshot_ids kwarg
    (paired with the tool_use_id) for each dispatch."""

    def __init__(self, result_content: str = "ok") -> None:
        self._result_content = result_content
        self.prov_ids: list[tuple[str, ...]] = []
        self.tool_use_ids: list[str] = []

    async def execute(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        execution_context: Any = None,
        provenance_snapshot_ids: tuple[str, ...] = (),
    ) -> ToolResult:
        del tool_name, tool_input, execution_context
        self.prov_ids.append(provenance_snapshot_ids)
        self.tool_use_ids.append(tool_use_id)
        await asyncio.sleep(0)  # yield so concurrent tasks interleave
        return ToolResult(tool_use_id=tool_use_id, content=self._result_content)


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_Resp],
    dispatcher: Any,
    **client_kwargs: Any,
):
    """Construct a real AnthropicClient against a stub SDK, then swap in a
    per-client response iterator (so concurrent clients don't share one)."""
    fake = types.ModuleType("anthropic")

    class _Anthropic:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

    fake.Anthropic = _Anthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub")

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient(dispatcher=dispatcher, **client_kwargs)
    client._client = _FakeSDKClient(responses)
    return client


def _cache():
    from backend.agents.anthropic_native_client import _SNAPSHOT_CACHE

    return _SNAPSHOT_CACHE


# ─── 1. threaded to dispatcher ────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_id_is_threaded_to_dispatcher(monkeypatch):
    dispatcher = _RecordingDispatcher()
    client = _make_client(monkeypatch, [_tool_use_resp(), _end_resp()], dispatcher)
    result = await client.run_with_tools(prompt="go")
    assert result.stop_reason == "end_turn"
    assert len(dispatcher.prov_ids) == 1
    ids = dispatcher.prov_ids[0]
    assert isinstance(ids, tuple) and len(ids) == 1
    assert ids[0].startswith("psnap-")


# ─── 2. per-turn distinct + resolvable in the process-local cache ─────


@pytest.mark.asyncio
async def test_per_turn_ids_are_distinct_and_resolvable(monkeypatch):
    dispatcher = _RecordingDispatcher()
    client = _make_client(
        monkeypatch,
        [_tool_use_resp(tid="tu_a"), _tool_use_resp(tid="tu_b"), _end_resp()],
        dispatcher,
    )
    await client.run_with_tools(prompt="go")
    assert len(dispatcher.prov_ids) == 2
    (id_turn1,), (id_turn2,) = dispatcher.prov_ids
    assert id_turn1 != id_turn2
    for sid in (id_turn1, id_turn2):
        snap = _cache().get(sid)
        assert isinstance(snap, ProvenanceSnapshot)
        assert snap.snapshot_id == sid


# ─── 3. temporal rule: turn N's result enters turn N+1's snapshot ─────


@pytest.mark.asyncio
async def test_tool_result_is_carried_into_next_turn_snapshot(monkeypatch):
    dispatcher = _RecordingDispatcher(result_content="turn1 output")
    client = _make_client(
        monkeypatch,
        [_tool_use_resp(tid="tu_first"), _tool_use_resp(tid="tu_second"), _end_resp()],
        dispatcher,
    )
    await client.run_with_tools(prompt="go")
    (id_turn1,), (id_turn2,) = dispatcher.prov_ids
    snap1 = _cache().get(id_turn1)
    snap2 = _cache().get(id_turn2)
    tr_records_2 = [
        r
        for r in snap2.records
        if r.source_kind == TOOL_RESULT and r.source_id == "tu_first"
    ]
    assert len(tr_records_2) == 1, "turn 2's snapshot must carry turn 1's result"
    assert not any(
        r.source_kind == TOOL_RESULT for r in snap1.records
    ), "turn 1's snapshot must NOT contain its own (not-yet-produced) result"


# ─── 4. stale-refresh capture ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_refresh_is_captured_in_next_seal(monkeypatch):
    dispatcher = _RecordingDispatcher()
    client = _make_client(
        monkeypatch,
        [_tool_use_resp(tid="tu_a"), _tool_use_resp(tid="tu_b"), _end_resp()],
        dispatcher,
    )

    # The config-driven path needs a real on-disk touched file + a mutating
    # tool result, which never happens under the stub SDK — monkeypatch the
    # injection to "fire" on iteration 2 only.
    calls = {"n": 0}

    def _fake_refresh(**kwargs: Any) -> tuple[str, str] | None:
        del kwargs
        calls["n"] += 1
        if calls["n"] == 2:
            return ("f.c", "<content>")
        return None

    monkeypatch.setattr(client, "_maybe_inject_stale_refresh", _fake_refresh)

    await client.run_with_tools(prompt="go")
    (id_turn1,), (id_turn2,) = dispatcher.prov_ids
    snap1 = _cache().get(id_turn1)
    snap2 = _cache().get(id_turn2)
    refresh_records = [
        r
        for r in snap2.records
        if r.source_kind == STALE_REFRESH and r.source_id == "f.c"
    ]
    assert len(refresh_records) == 1, "seal after the refresh must carry it"
    assert not any(r.source_kind == STALE_REFRESH for r in snap1.records)


# ─── 5. task isolation across concurrent runs ─────────────────────────


@pytest.mark.asyncio
async def test_concurrent_runs_do_not_leak_snapshot_ids(monkeypatch):
    dispatcher_a = _RecordingDispatcher()
    dispatcher_b = _RecordingDispatcher()
    # Several tool rounds each so the two loops genuinely interleave.
    client_a = _make_client(
        monkeypatch,
        [_tool_use_resp(tid=f"a{i}") for i in range(3)] + [_end_resp("A")],
        dispatcher_a,
    )
    client_b = _make_client(
        monkeypatch,
        [_tool_use_resp(tid=f"b{i}") for i in range(3)] + [_end_resp("B")],
        dispatcher_b,
    )
    await asyncio.gather(
        asyncio.create_task(client_a.run_with_tools(prompt="go")),
        asyncio.create_task(client_b.run_with_tools(prompt="go")),
    )
    ids_a = {sid for ids in dispatcher_a.prov_ids for sid in ids}
    ids_b = {sid for ids in dispatcher_b.prov_ids for sid in ids}
    assert len(dispatcher_a.prov_ids) == 3 and len(ids_a) == 3
    assert len(dispatcher_b.prov_ids) == 3 and len(ids_b) == 3
    assert ids_a.isdisjoint(ids_b), "no cross-task snapshot-id leak"
    # No collector leaked into the test task after both loops finished.
    assert active_collector() is None


# ─── 6. fail-safe: a seal failure never aborts the run ────────────────


@pytest.mark.asyncio
async def test_seal_failure_degrades_to_empty_ids_and_run_completes(monkeypatch):
    dispatcher = _RecordingDispatcher()
    client = _make_client(monkeypatch, [_tool_use_resp(), _end_resp()], dispatcher)

    def _boom(self, cache, *, extra_omissions=()):  # noqa: ANN001
        raise RuntimeError("seal exploded")

    monkeypatch.setattr(ProvenanceCollector, "seal", _boom)

    result = await client.run_with_tools(prompt="go")
    assert result.stop_reason == "end_turn", "provenance must never break the turn"
    assert dispatcher.prov_ids == [()]


# ─── 7. dispatcher forwards the kwarg to the guard ────────────────────


def _install_guard_recorder(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def _recorder(**kwargs):
        calls.append(kwargs)
        return action_guard.GuardOutcome(
            proceed=True, decision=None, family="code_write", mode="shadow"
        )

    monkeypatch.setattr(tool_dispatcher, "guard_tool_dispatch", _recorder)
    return calls


def test_dispatcher_forwards_provenance_ids_to_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _install_guard_recorder(monkeypatch)
    d = ToolDispatcher()

    async def _handler(args: dict) -> str:
        del args
        return "ok"

    # Register a handler for the tool used — the no-handler branch returns
    # BEFORE the guard runs.
    d.register("Read", _handler)
    res = asyncio.run(
        d.execute(
            tool_use_id="tu1",
            tool_name="Read",
            tool_input={},
            provenance_snapshot_ids=("psnap-x",),
        )
    )
    assert not res.is_error
    assert len(recorded) == 1
    assert recorded[0]["provenance_snapshot_ids"] == ("psnap-x",)


def test_dispatcher_default_is_dormant_empty_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _install_guard_recorder(monkeypatch)
    d = ToolDispatcher()

    async def _handler(args: dict) -> str:
        del args
        return "ok"

    d.register("Read", _handler)
    asyncio.run(d.execute(tool_use_id="tu1", tool_name="Read", tool_input={}))
    assert recorded[0]["provenance_snapshot_ids"] == ()
