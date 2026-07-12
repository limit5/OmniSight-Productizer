"""OP-2601 — U6-0 P-ID-B: ExecutionContext threading through the runner
dispatch path (dormant plumbing).

Locks the two complementary channels set from ONE computed ``effective``
context per ``run_with_tools`` call:

  * the explicit keyword param: run_with_tools → dispatcher.execute
    (where the later T7b guard reads it), with precedence
    per-call PARAM > client ATTR > None, never mutating the attribute;
  * the task-local ContextVar published for the duration of the loop and
    always reset — read back via ``get_active_execution_context()`` inside
    a handler (the channel the nested sub-agent handler uses), including
    task isolation across two concurrent ``run_with_tools`` invocations
    (codex BLOCKER-1);
  * end-to-end: a parent loop dispatching the ``Agent`` tool hands a
    ``for_child``-derived context to the nested sub-agent run.

No launcher populates the context yet (P-ID-C) and nothing authorizes on
it (T7b) — everything here defaults to ``None``.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from backend.agents import execution_context as ec
from backend.agents.anthropic_native_client import (
    RunResult,
    TokenUsage,
    _active_execution_context,
    get_active_execution_context,
)
from backend.agents.sub_agent import make_agent_tool_handler
from backend.agents.tool_dispatcher import ToolResult


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
    """Duck-typed dispatcher: records the execution_context PARAM and the
    ACTIVE ContextVar value observed during each call."""

    def __init__(self) -> None:
        self.param_contexts: list[Any] = []
        self.active_contexts: list[Any] = []

    async def execute(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        execution_context: Any = None,
        provenance_snapshot_ids: tuple[str, ...] = (),
    ) -> ToolResult:
        del tool_name, tool_input, provenance_snapshot_ids
        self.param_contexts.append(execution_context)
        self.active_contexts.append(get_active_execution_context())
        await asyncio.sleep(0)  # yield so concurrent tasks interleave
        return ToolResult(tool_use_id=tool_use_id, content="ok")


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


def _ctx(request_id: str, tenant_id: str = "t-default") -> ec.ExecutionContext:
    return ec.for_service(
        service_name="runner",
        tenant_id=tenant_id,
        request_id=request_id,
        roles=["operator"],
        authorization_source="a2a",
    )


# ─── Param channel: run_with_tools → dispatcher.execute ──────────────


@pytest.mark.asyncio
async def test_param_context_is_forwarded_to_dispatcher_execute(monkeypatch):
    dispatcher = _RecordingDispatcher()
    client = _make_client(
        monkeypatch, [_tool_use_resp(), _end_resp()], dispatcher
    )
    ctx = _ctx("r-param")
    result = await client.run_with_tools(prompt="go", execution_context=ctx)
    assert result.stop_reason == "end_turn"
    assert dispatcher.param_contexts == [ctx]
    assert dispatcher.param_contexts[0] is ctx


@pytest.mark.asyncio
async def test_client_attr_context_used_when_no_param(monkeypatch):
    dispatcher = _RecordingDispatcher()
    attr_ctx = _ctx("r-attr")
    client = _make_client(
        monkeypatch,
        [_tool_use_resp(), _end_resp()],
        dispatcher,
        execution_context=attr_ctx,
    )
    await client.run_with_tools(prompt="go")
    assert dispatcher.param_contexts == [attr_ctx]


@pytest.mark.asyncio
async def test_param_beats_attr_and_attr_is_not_mutated(monkeypatch):
    """Precedence: per-call PARAM > client ATTR > None. The override is
    authoritative for the call only — self.execution_context is untouched,
    so a sub-agent override never clobbers the parent."""
    dispatcher = _RecordingDispatcher()
    attr_ctx = _ctx("r-attr", tenant_id="t-parent")
    param_ctx = _ctx("r-param", tenant_id="t-restricted")
    client = _make_client(
        monkeypatch,
        [_tool_use_resp(), _end_resp()],
        dispatcher,
        execution_context=attr_ctx,
    )
    await client.run_with_tools(prompt="go", execution_context=param_ctx)
    assert dispatcher.param_contexts == [param_ctx]
    assert client.execution_context is attr_ctx  # not mutated


@pytest.mark.asyncio
async def test_no_param_no_attr_dispatches_none_dormant_default(monkeypatch):
    dispatcher = _RecordingDispatcher()
    client = _make_client(
        monkeypatch, [_tool_use_resp(), _end_resp()], dispatcher
    )
    await client.run_with_tools(prompt="go")
    assert dispatcher.param_contexts == [None]


# ─── ContextVar channel: published during the loop, reset after ───────


@pytest.mark.asyncio
async def test_contextvar_is_active_during_tool_call_and_reset_after(monkeypatch):
    dispatcher = _RecordingDispatcher()
    client = _make_client(
        monkeypatch, [_tool_use_resp(), _end_resp()], dispatcher
    )
    ctx = _ctx("r-active")
    prior = _ctx("r-prior")
    token = _active_execution_context.set(prior)
    try:
        assert get_active_execution_context() is prior
        await client.run_with_tools(prompt="go", execution_context=ctx)
        # DURING the tool call the handler observed the effective ctx…
        assert dispatcher.active_contexts == [ctx]
        # …and after run_with_tools returns the PRIOR value is restored
        # (token reset, nesting-safe), not clobbered to None.
        assert get_active_execution_context() is prior
    finally:
        _active_execution_context.reset(token)
    assert get_active_execution_context() is None


@pytest.mark.asyncio
async def test_contextvar_task_isolation_two_concurrent_runs(monkeypatch):
    """codex BLOCKER-1: two run_with_tools invocations in separate asyncio
    tasks with different contexts each observe THEIR OWN active context
    inside the handler."""
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
    ctx_a = _ctx("r-task-a", tenant_id="t-a")
    ctx_b = _ctx("r-task-b", tenant_id="t-b")

    await asyncio.gather(
        asyncio.create_task(
            client_a.run_with_tools(prompt="go", execution_context=ctx_a)
        ),
        asyncio.create_task(
            client_b.run_with_tools(prompt="go", execution_context=ctx_b)
        ),
    )
    assert dispatcher_a.active_contexts == [ctx_a] * 3
    assert dispatcher_a.param_contexts == [ctx_a] * 3
    assert dispatcher_b.active_contexts == [ctx_b] * 3
    assert dispatcher_b.param_contexts == [ctx_b] * 3
    # Nothing leaked into the test task after both loops finished.
    assert get_active_execution_context() is None


# ─── End-to-end: parent loop → Agent handler → for_child sub-run ──────


class _SubStubClient:
    """Stands in for the nested AnthropicClient inside the Agent handler."""

    def __init__(self) -> None:
        self.captured_kwargs: dict[str, Any] = {}

    async def run_with_tools(self, **kwargs: Any) -> RunResult:
        self.captured_kwargs = kwargs
        return RunResult(
            final_text="sub done",
            iterations=1,
            stop_reason="end_turn",
            usage=TokenUsage(),
        )


@pytest.mark.asyncio
async def test_end_to_end_parent_ctx_reaches_dispatcher_and_child_sub_agent(
    monkeypatch,
):
    """AC #4: the plumbing carries a real context end-to-end — read back at
    the dispatcher PARAM and at get_active_execution_context() inside the
    sub-agent handler, which derives a for_child context for the nested
    run — even though production still passes None until P-ID-C."""
    sub_client = _SubStubClient()
    agent_handler = make_agent_tool_handler(client=sub_client)

    class _AgentDispatcher:
        def __init__(self) -> None:
            self.param_contexts: list[Any] = []

        async def execute(
            self,
            *,
            tool_use_id: str,
            tool_name: str,
            tool_input: dict[str, Any],
            execution_context: Any = None,
            provenance_snapshot_ids: tuple[str, ...] = (),
        ) -> ToolResult:
            del tool_name, provenance_snapshot_ids
            self.param_contexts.append(execution_context)
            content = await agent_handler(tool_input)
            return ToolResult(tool_use_id=tool_use_id, content=content)

    dispatcher = _AgentDispatcher()
    parent_ctx = _ctx("r-parent", tenant_id="t-parent")
    client = _make_client(
        monkeypatch,
        [
            _tool_use_resp(
                name="Agent",
                tool_input={"description": "explore", "prompt": "look around"},
            ),
            _end_resp(),
        ],
        dispatcher,
    )

    result = await client.run_with_tools(
        prompt="go", execution_context=parent_ctx
    )
    assert result.stop_reason == "end_turn"
    # Channel 1 — the dispatcher param carried the parent context.
    assert dispatcher.param_contexts == [parent_ctx]
    # Channel 2 — the Agent handler read the ACTIVE context and handed a
    # for_child-derived context to the nested sub-agent run.
    child = sub_client.captured_kwargs["execution_context"]
    assert child == ec.for_child(parent_ctx)
    assert child.principal_type == parent_ctx.principal_type
    assert child.tenant_id == parent_ctx.tenant_id
    assert child.actor_id == parent_ctx.actor_id
    # And the parent task's active context was reset afterwards.
    assert get_active_execution_context() is None
