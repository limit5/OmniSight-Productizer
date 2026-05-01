"""AB.2 — Native Anthropic client + tool dispatcher tests.

All tests use mocks against a stub `anthropic.Anthropic` SDK shape — no
real API calls. Locks:

  - `simple()` returns text + usage from a no-tool response
  - `simple_params()` produces a batch-ready params dict (matching
    `messages.create()` shape) with cache_control on system + tools
  - `run_with_tools()` loops over multiple tool_use rounds, executes
    each via dispatcher, feeds back tool_results, terminates on
    `end_turn`
  - max_iterations bail-out on runaway loops
  - tool execution errors surface as `is_error=True` tool_results
    without raising
  - cache_control marks added to last system block + last tool

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §3
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from backend.agents.tool_dispatcher import (
    ToolDispatcher,
    ToolResult,
    register_handler,
)


# ─── Stub Anthropic SDK shape ────────────────────────────────────


@dataclass
class _StubUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _StubBlock:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None

    def model_dump(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type}
        if self.text is not None:
            d["text"] = self.text
        if self.id is not None:
            d["id"] = self.id
        if self.name is not None:
            d["name"] = self.name
        if self.input is not None:
            d["input"] = self.input
        return d


@dataclass
class _StubResponse:
    content: list[_StubBlock]
    stop_reason: str
    usage: _StubUsage


class _StubMessages:
    def __init__(self, responses: Iterator[_StubResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        # Deep copy so post-call mutations to messages list don't retroactively
        # change earlier captured calls. The production code mutates the same
        # `messages` list across iterations; without copying here the
        # introspection-style tests below would see the final state on every
        # captured call.
        import copy

        self.calls.append(copy.deepcopy(kwargs))
        return next(self._responses)


class _StubAnthropic:
    """Minimal anthropic.Anthropic stand-in."""

    def __init__(self, *, api_key: str) -> None:  # noqa: ARG002
        self.messages: _StubMessages | None = None  # set by helper below


def _install_stub_sdk(monkeypatch: pytest.MonkeyPatch, responses: list[_StubResponse]) -> None:
    """Replace the `anthropic` module that AnthropicClient imports lazily."""
    import sys
    import types

    fake = types.ModuleType("anthropic")
    iterator = iter(responses)

    class _Client(_StubAnthropic):
        def __init__(self, **kwargs):  # noqa: ANN003
            super().__init__(api_key=kwargs.get("api_key", "stub"))
            self.messages = _StubMessages(iterator)

    fake.Anthropic = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    # Also overwrite ANTHROPIC_API_KEY so client construction succeeds.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub")


# ─── Tool dispatcher tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_executes_async_handler():
    d = ToolDispatcher()

    async def handler(input_dict):
        return f"got {input_dict['x']}"

    d.register("Read", handler)
    result = await d.execute(tool_use_id="tu_1", tool_name="Read", tool_input={"x": 7})
    assert result.tool_use_id == "tu_1"
    assert result.content == "got 7"
    assert not result.is_error


@pytest.mark.asyncio
async def test_dispatcher_executes_sync_handler():
    d = ToolDispatcher()

    def handler(input_dict):
        return {"ok": input_dict["a"] + input_dict["b"]}

    d.register("Bash", handler)
    result = await d.execute(tool_use_id="tu_x", tool_name="Bash", tool_input={"a": 2, "b": 3})
    assert result.content == json.dumps({"ok": 5}, ensure_ascii=False)
    assert not result.is_error


@pytest.mark.asyncio
async def test_dispatcher_unknown_tool_returns_error():
    d = ToolDispatcher()
    result = await d.execute(tool_use_id="tu_z", tool_name="Read", tool_input={})
    assert result.is_error
    payload = json.loads(result.content)
    assert payload["error"] == "no_handler_registered"
    assert payload["tool_name"] == "Read"


@pytest.mark.asyncio
async def test_dispatcher_handler_exception_caught():
    d = ToolDispatcher()

    def boom(input_dict):  # noqa: ARG001
        raise RuntimeError("kaboom")

    d.register("Edit", boom)
    result = await d.execute(tool_use_id="tu_e", tool_name="Edit", tool_input={})
    assert result.is_error
    payload = json.loads(result.content)
    assert payload["error"] == "tool_raised"
    assert payload["exception_type"] == "RuntimeError"
    assert "kaboom" in payload["message"]


def test_dispatcher_rejects_unknown_tool_registration():
    d = ToolDispatcher()

    def handler(_):  # pragma: no cover
        return None

    with pytest.raises(ValueError, match="unknown tool"):
        d.register("DefinitelyNotASchema", handler)


def test_dispatcher_rejects_duplicate_registration():
    d = ToolDispatcher()
    d.register("Read", lambda _: "first")
    with pytest.raises(ValueError, match="already registered"):
        d.register("Read", lambda _: "second")


def test_tool_result_anthropic_block_shape_no_error():
    block = ToolResult(tool_use_id="t1", content="hello").to_anthropic_block()
    assert block == {"type": "tool_result", "tool_use_id": "t1", "content": "hello"}


def test_tool_result_anthropic_block_shape_with_error():
    block = ToolResult(tool_use_id="t1", content="oops", is_error=True).to_anthropic_block()
    assert block == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "oops",
        "is_error": True,
    }


def test_register_handler_decorator_uses_default_dispatcher():
    """The @register_handler decorator wires into the module-level dispatcher."""
    # Use a known schema name to avoid conflict.
    @register_handler("Glob")
    def glob_handler(_):  # noqa: ARG001
        return "ok"

    from backend.agents.tool_dispatcher import get_default_dispatcher

    assert get_default_dispatcher().has_handler("Glob")


# ─── AnthropicClient.simple() ────────────────────────────────────


def test_simple_returns_text_and_usage(monkeypatch):
    _install_stub_sdk(
        monkeypatch,
        [
            _StubResponse(
                content=[_StubBlock(type="text", text="hello world")],
                stop_reason="end_turn",
                usage=_StubUsage(input_tokens=10, output_tokens=2),
            )
        ],
    )

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    text, usage = client.simple(prompt="hi")
    assert text == "hello world"
    assert usage.input_tokens == 10
    assert usage.output_tokens == 2


def test_simple_passes_system(monkeypatch):
    _install_stub_sdk(
        monkeypatch,
        [
            _StubResponse(
                content=[_StubBlock(type="text", text="ok")],
                stop_reason="end_turn",
                usage=_StubUsage(),
            )
        ],
    )

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    client.simple(prompt="hi", system="Be terse.")
    call = client._client.messages.calls[0]  # type: ignore[attr-defined]
    assert call["system"] == "Be terse."


# ─── AnthropicClient.simple_params() ─────────────────────────────


def test_simple_params_shape_for_batch(monkeypatch):
    """simple_params builds a params dict with tools[] + cache_control marks."""
    _install_stub_sdk(monkeypatch, [])  # no API call expected

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    params = client.simple_params(
        prompt="parse this",
        tools=["Read", "Edit"],
        system="You are a code reviewer.",
        enable_cache=True,
    )
    assert params["model"]
    assert params["messages"] == [{"role": "user", "content": "parse this"}]
    # System should be wrapped in a list of blocks with cache_control on last.
    assert isinstance(params["system"], list)
    assert params["system"][-1]["cache_control"] == {"type": "ephemeral"}
    # Tools should have cache_control on the last entry.
    assert len(params["tools"]) == 2
    assert params["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_simple_params_no_cache(monkeypatch):
    _install_stub_sdk(monkeypatch, [])
    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    params = client.simple_params(
        prompt="hi", tools=["Read"], system="sys", enable_cache=False
    )
    assert params["system"] == "sys"  # raw string, no cache wrap
    assert "cache_control" not in params["tools"][0]


# ─── AnthropicClient.run_with_tools() ────────────────────────────


@pytest.mark.asyncio
async def test_run_with_tools_single_round_no_tool_use(monkeypatch):
    _install_stub_sdk(
        monkeypatch,
        [
            _StubResponse(
                content=[_StubBlock(type="text", text="done")],
                stop_reason="end_turn",
                usage=_StubUsage(input_tokens=5, output_tokens=1),
            )
        ],
    )

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    result = await client.run_with_tools(prompt="trivial", tools=None)
    assert result.iterations == 1
    assert result.stop_reason == "end_turn"
    assert result.final_text == "done"
    assert result.usage.input_tokens == 5


@pytest.mark.asyncio
async def test_run_with_tools_executes_tool_then_finalizes(monkeypatch):
    """Two-round loop: first call returns tool_use, second returns end_turn."""
    _install_stub_sdk(
        monkeypatch,
        [
            _StubResponse(
                content=[
                    _StubBlock(
                        type="tool_use",
                        id="tu_1",
                        name="Read",
                        input={"file_path": "/tmp/a"},
                    )
                ],
                stop_reason="tool_use",
                usage=_StubUsage(input_tokens=20, output_tokens=8),
            ),
            _StubResponse(
                content=[_StubBlock(type="text", text="file contents seen, all clear")],
                stop_reason="end_turn",
                usage=_StubUsage(input_tokens=30, output_tokens=12),
            ),
        ],
    )

    from backend.agents.anthropic_native_client import AnthropicClient

    dispatcher = ToolDispatcher()
    dispatcher.register("Read", lambda inp: f"contents of {inp['file_path']}")

    client = AnthropicClient(dispatcher=dispatcher)
    result = await client.run_with_tools(
        prompt="please read /tmp/a", tools=["Read"], enable_cache=False
    )
    assert result.iterations == 2
    assert result.stop_reason == "end_turn"
    assert "all clear" in result.final_text
    # Token usage aggregated across both turns.
    assert result.usage.input_tokens == 50
    assert result.usage.output_tokens == 20
    # Tool call recorded.
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "Read"
    assert "contents of /tmp/a" in result.tool_calls[0]["result"]
    assert not result.tool_calls[0]["is_error"]


@pytest.mark.asyncio
async def test_run_with_tools_tool_error_propagates_to_model(monkeypatch):
    """Tool execution failure surfaces as is_error tool_result, not raise."""
    _install_stub_sdk(
        monkeypatch,
        [
            _StubResponse(
                content=[
                    _StubBlock(type="tool_use", id="tu_x", name="Bash", input={"command": "x"})
                ],
                stop_reason="tool_use",
                usage=_StubUsage(),
            ),
            _StubResponse(
                content=[_StubBlock(type="text", text="recovered")],
                stop_reason="end_turn",
                usage=_StubUsage(),
            ),
        ],
    )

    from backend.agents.anthropic_native_client import AnthropicClient

    dispatcher = ToolDispatcher()

    def boom(_):  # noqa: ARG001
        raise OSError("disk full")

    dispatcher.register("Bash", boom)

    client = AnthropicClient(dispatcher=dispatcher)
    result = await client.run_with_tools(prompt="run something", tools=["Bash"])
    assert result.iterations == 2
    assert result.stop_reason == "end_turn"
    assert result.tool_calls[0]["is_error"]
    assert "disk full" in result.tool_calls[0]["result"]


@pytest.mark.asyncio
async def test_run_with_tools_max_iterations_bails(monkeypatch):
    """Runaway loop caps at max_iterations with explicit stop_reason."""
    # Always returns tool_use — would loop forever without guard.
    looper = [
        _StubResponse(
            content=[_StubBlock(type="tool_use", id=f"t{i}", name="Read", input={"file_path": "x"})],
            stop_reason="tool_use",
            usage=_StubUsage(),
        )
        for i in range(50)
    ]
    _install_stub_sdk(monkeypatch, looper)

    from backend.agents.anthropic_native_client import AnthropicClient

    dispatcher = ToolDispatcher()
    dispatcher.register("Read", lambda _: "ok")

    client = AnthropicClient(dispatcher=dispatcher)
    result = await client.run_with_tools(prompt="loop", tools=["Read"], max_iterations=3)
    assert result.iterations == 3
    assert result.stop_reason == "max_iterations_exceeded"


@pytest.mark.asyncio
async def test_run_with_tools_multiple_tools_in_one_turn(monkeypatch):
    """Single assistant turn with N tool_use blocks resolves all in one batch."""
    _install_stub_sdk(
        monkeypatch,
        [
            _StubResponse(
                content=[
                    _StubBlock(type="tool_use", id="t_a", name="Read", input={"file_path": "/a"}),
                    _StubBlock(type="tool_use", id="t_b", name="Read", input={"file_path": "/b"}),
                ],
                stop_reason="tool_use",
                usage=_StubUsage(),
            ),
            _StubResponse(
                content=[_StubBlock(type="text", text="both read")],
                stop_reason="end_turn",
                usage=_StubUsage(),
            ),
        ],
    )

    from backend.agents.anthropic_native_client import AnthropicClient

    dispatcher = ToolDispatcher()
    dispatcher.register("Read", lambda inp: f"r:{inp['file_path']}")

    client = AnthropicClient(dispatcher=dispatcher)
    result = await client.run_with_tools(prompt="read both", tools=["Read"])
    # Both tool calls in turn 1, single tool_results message, then turn 2 done.
    assert result.iterations == 2
    assert len(result.tool_calls) == 2
    # And critically, the second API call should have ONE user message with
    # both tool_result blocks bundled — Anthropic's required shape.
    second_call = client._client.messages.calls[1]  # type: ignore[attr-defined]
    last_msg = second_call["messages"][-1]
    assert last_msg["role"] == "user"
    assert isinstance(last_msg["content"], list)
    assert len(last_msg["content"]) == 2
    assert all(b["type"] == "tool_result" for b in last_msg["content"])


def test_anthropic_client_requires_api_key(monkeypatch):
    """Constructor raises if neither env var nor explicit key provided."""
    import sys
    import types

    fake = types.ModuleType("anthropic")
    fake.Anthropic = _StubAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from backend.agents.anthropic_native_client import AnthropicClient

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY not set"):
        AnthropicClient()
