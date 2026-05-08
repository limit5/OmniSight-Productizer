"""OP-112 — structured error envelopes for top-3 tools."""

from __future__ import annotations

import json

import pytest

from backend.agents.tool_dispatcher import ToolDispatcher


def _envelope(result):
    assert result.is_error is True
    payload = json.loads(result.content)
    assert {"error", "error_type", "retryable", "hint"} <= set(payload)
    assert isinstance(payload["error"], str)
    assert isinstance(payload["error_type"], str)
    assert isinstance(payload["retryable"], bool)
    assert isinstance(payload["hint"], str)
    return {key: payload[key] for key in ("error", "error_type", "retryable", "hint")}


@pytest.mark.asyncio
async def test_read_exception_returns_tool_error_envelope():
    dispatcher = ToolDispatcher()

    def boom(_payload):
        raise FileNotFoundError("missing input")

    dispatcher.register("Read", boom)

    result = await dispatcher.execute("tu_read", "Read", {"file_path": "missing"})

    payload = _envelope(result)
    assert payload == {
        "error": "tool_raised",
        "error_type": "FileNotFoundError",
        "retryable": False,
        "hint": "missing input",
    }


@pytest.mark.asyncio
async def test_edit_exception_returns_tool_error_envelope():
    dispatcher = ToolDispatcher()

    def boom(_payload):
        raise ValueError("old_string did not match")

    dispatcher.register("Edit", boom)

    result = await dispatcher.execute("tu_edit", "Edit", {})

    payload = _envelope(result)
    assert payload == {
        "error": "tool_raised",
        "error_type": "ValueError",
        "retryable": False,
        "hint": "old_string did not match",
    }


@pytest.mark.asyncio
async def test_bash_failures_return_tool_error_envelopes():
    cases = [
        (
            "timeout",
            "❌ command timed out after 1s",
            {
                "error": "bash_timeout",
                "error_type": "timeout",
                "retryable": True,
                "hint": "❌ command timed out after 1s",
            },
        ),
        (
            "nonzero",
            "STDOUT:\n\nSTDERR:\nfailed\nEXIT_CODE: 2",
            {
                "error": "bash_nonzero_exit",
                "error_type": "nonzero_exit",
                "retryable": False,
                "hint": "STDOUT:\n\nSTDERR:\nfailed\nEXIT_CODE: 2",
            },
        ),
        (
            "oversized",
            "(... stdout truncated to last 30KB ...)\nlarge\nEXIT_CODE: 0",
            {
                "error": "bash_output_oversized",
                "error_type": "oversized_output",
                "retryable": True,
                "hint": "Narrow the command or redirect large output to a file.",
            },
        ),
    ]
    for case_name, raw_output, expected in cases:
        dispatcher = ToolDispatcher()
        dispatcher.register("Bash", lambda _payload, out=raw_output: out)

        result = await dispatcher.execute(f"tu_bash_{case_name}", "Bash", {})

        assert _envelope(result) == expected
