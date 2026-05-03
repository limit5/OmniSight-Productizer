"""AB.2.4 — Tool dispatcher contract tests.

Locks:
  - ToolResult serializes to Anthropic tool_result blocks
  - register() rejects unknown schemas and duplicate handlers
  - registered_tools() is deterministic
  - execute() handles async + sync handlers, including executor dispatch
  - execute() normalizes str / None / JSON / fallback-string content
  - missing handlers and raised exceptions return structured error results
  - register_handler() can be isolated to a fresh default dispatcher in tests

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §3
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

import pytest

from backend.agents import tool_dispatcher
from backend.agents.tool_dispatcher import ToolDispatcher, ToolResult, register_handler


# ─── ToolResult Anthropic block shape ────────────────────────────


def test_tool_result_block_omits_is_error_when_false():
    block = ToolResult(tool_use_id="tu_1", content="ok").to_anthropic_block()
    assert block == {
        "type": "tool_result",
        "tool_use_id": "tu_1",
        "content": "ok",
    }


def test_tool_result_block_includes_is_error_only_when_true():
    block = ToolResult(
        tool_use_id="tu_2", content='{"error":"boom"}', is_error=True
    ).to_anthropic_block()
    assert block == {
        "type": "tool_result",
        "tool_use_id": "tu_2",
        "content": '{"error":"boom"}',
        "is_error": True,
    }


# ─── Registration mechanics ─────────────────────────────────────


def test_register_rejects_unknown_schema_name():
    dispatcher = ToolDispatcher()

    with pytest.raises(ValueError, match="unknown tool"):
        dispatcher.register("DefinitelyNotASchema", lambda _: "unused")


def test_register_rejects_duplicate_handler():
    dispatcher = ToolDispatcher()
    dispatcher.register("Read", lambda _: "first")

    with pytest.raises(ValueError, match="already registered"):
        dispatcher.register("Read", lambda _: "second")


def test_registered_tools_are_sorted():
    dispatcher = ToolDispatcher()
    dispatcher.register("Write", lambda _: "write")
    dispatcher.register("Read", lambda _: "read")
    dispatcher.register("Bash", lambda _: "bash")

    assert dispatcher.registered_tools() == ["Bash", "Read", "Write"]
    assert dispatcher.has_handler("Read") is True
    assert dispatcher.has_handler("Edit") is False


def test_register_handler_decorator_can_use_isolated_default(monkeypatch):
    fresh = ToolDispatcher()
    monkeypatch.setattr(tool_dispatcher, "_default_dispatcher", fresh)

    @register_handler("Glob")
    def glob_handler(_payload):
        return "ok"

    assert glob_handler({"pattern": "*.py"}) == "ok"
    assert tool_dispatcher.get_default_dispatcher() is fresh
    assert tool_dispatcher.get_default_dispatcher().registered_tools() == ["Glob"]


# ─── Execute success paths ───────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_awaits_async_handler():
    dispatcher = ToolDispatcher()

    async def handler(payload):
        return f"got {payload['value']}"

    dispatcher.register("Read", handler)
    result = await dispatcher.execute(
        tool_use_id="tu_async",
        tool_name="Read",
        tool_input={"value": 7},
    )

    assert result == ToolResult(tool_use_id="tu_async", content="got 7")


@pytest.mark.asyncio
async def test_execute_runs_sync_handler_in_executor():
    dispatcher = ToolDispatcher()
    caller_thread = threading.get_ident()

    def handler(payload):
        return {"worker_thread": threading.get_ident(), "value": payload["value"]}

    dispatcher.register("Bash", handler)
    result = await dispatcher.execute(
        tool_use_id="tu_sync",
        tool_name="Bash",
        tool_input={"value": 11},
    )

    payload = json.loads(result.content)
    assert payload["value"] == 11
    assert payload["worker_thread"] != caller_thread
    assert result.is_error is False


@pytest.mark.asyncio
async def test_execute_normalizes_none_to_empty_content():
    dispatcher = ToolDispatcher()
    dispatcher.register("Edit", lambda _: None)

    result = await dispatcher.execute("tu_none", "Edit", {})

    assert result.content == ""
    assert result.is_error is False


@pytest.mark.asyncio
async def test_execute_serializes_json_with_unicode_unescaped():
    dispatcher = ToolDispatcher()
    dispatcher.register("Read", lambda _: {"message": "完成", "ok": True})

    result = await dispatcher.execute("tu_json", "Read", {})

    assert result.content == json.dumps(
        {"message": "完成", "ok": True}, ensure_ascii=False
    )
    assert "完成" in result.content
    assert result.is_error is False


@dataclass
class _FallbackOnly:
    value: str


@pytest.mark.asyncio
async def test_execute_falls_back_to_default_str_for_non_json_values():
    dispatcher = ToolDispatcher()
    dispatcher.register("Read", lambda _: {"object": _FallbackOnly("x")})

    result = await dispatcher.execute("tu_fallback", "Read", {})

    assert result.content == json.dumps({"object": "_FallbackOnly(value='x')"})
    assert result.is_error is False


# ─── Execute error paths ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_missing_handler_returns_structured_error():
    dispatcher = ToolDispatcher()
    dispatcher.register("Write", lambda _: "write")
    dispatcher.register("Read", lambda _: "read")

    result = await dispatcher.execute(
        tool_use_id="tu_missing",
        tool_name="Edit",
        tool_input={"file_path": "x"},
    )

    payload = json.loads(result.content)
    assert result.tool_use_id == "tu_missing"
    assert result.is_error is True
    assert payload == {
        "error": "no_handler_registered",
        "tool_name": "Edit",
        "registered": ["Read", "Write"],
    }


@pytest.mark.asyncio
async def test_execute_missing_handler_caps_registered_names():
    dispatcher = ToolDispatcher()
    for name in [
        "Read",
        "Write",
        "Edit",
        "Bash",
        "Grep",
        "Glob",
        "Agent",
        "WebFetch",
        "ToolSearch",
        "Skill",
        "WebSearch",
    ]:
        dispatcher.register(name, lambda _: "ok")

    result = await dispatcher.execute("tu_missing", "Task", {})

    payload = json.loads(result.content)
    assert payload["error"] == "no_handler_registered"
    assert payload["tool_name"] == "Task"
    assert payload["registered"] == dispatcher.registered_tools()[:10]
    assert len(payload["registered"]) == 10


@pytest.mark.asyncio
async def test_execute_handler_exception_returns_structured_error():
    dispatcher = ToolDispatcher()

    def handler(_payload):
        raise RuntimeError("kaboom")

    dispatcher.register("Read", handler)

    result = await dispatcher.execute("tu_boom", "Read", {})

    payload = json.loads(result.content)
    assert result.is_error is True
    assert payload["error"] == "tool_raised"
    assert payload["tool_name"] == "Read"
    assert payload["exception_type"] == "RuntimeError"
    assert payload["message"] == "kaboom"


@pytest.mark.asyncio
async def test_execute_handler_exception_message_is_capped():
    dispatcher = ToolDispatcher()

    def handler(_payload):
        raise ValueError("x" * 1200)

    dispatcher.register("Bash", handler)

    result = await dispatcher.execute("tu_long_error", "Bash", {})

    payload = json.loads(result.content)
    assert payload["error"] == "tool_raised"
    assert payload["exception_type"] == "ValueError"
    assert payload["message"] == "x" * 1000
