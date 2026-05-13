"""SP-B-X-006 / OP-1064 — integration: refresh markers appear at iter 10/20/30.

Drives :func:`AnthropicClient.run_with_tools` against a stub Anthropic SDK
that returns ``tool_use`` indefinitely so the loop runs long enough to
trip the C5 refresh trigger three times. The Edit tool handler writes
into a tmp file each turn, populating ``touched_files`` so the picker
has something to pick.

Two integration scenarios:

  - long session → ``[stale-refresh-injected]`` markers logged at iter
    10, 20, 30 (and the injected text actually shows up in the next API
    call's ``messages`` payload)
  - cost gate → with a 1-token max budget, every refresh is skipped with
    ``[stale-refresh-skipped] cost-budget-exceeded`` instead
"""

from __future__ import annotations

import logging
import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from backend.agents.tool_dispatcher import ToolDispatcher


# ─── Stub Anthropic SDK shape (mirrors backend/tests fixture) ────


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

    def create(self, **kwargs: Any) -> _StubResponse:
        import copy

        self.calls.append(copy.deepcopy(kwargs))
        return next(self._responses)


def _install_stub_sdk(
    monkeypatch: pytest.MonkeyPatch, responses: list[_StubResponse]
) -> None:
    fake = types.ModuleType("anthropic")
    iterator = iter(responses)

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            self.messages = _StubMessages(iterator)
            self.beta = type("_Beta", (), {"messages": _StubMessages(iterator)})()

    fake.Anthropic = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub")


def _make_edit_tool_use_chain(
    target_path: str, n: int
) -> list[_StubResponse]:
    """Build N stub responses, each a tool_use of Edit on the same path."""
    return [
        _StubResponse(
            content=[
                _StubBlock(
                    type="tool_use",
                    id=f"tu_{i}",
                    name="Edit",
                    input={"file_path": target_path, "old": "x", "new": "y"},
                )
            ],
            stop_reason="tool_use",
            usage=_StubUsage(input_tokens=1, output_tokens=1),
        )
        for i in range(n)
    ]


# ─── Long-session: markers at iter 10 / 20 / 30 ──────────────────


@pytest.mark.asyncio
async def test_long_session_emits_refresh_markers_at_iter_10_20_30(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    target = tmp_path / "hot.py"
    target.write_text("# small file content\n", encoding="utf-8")

    _install_stub_sdk(monkeypatch, _make_edit_tool_use_chain(str(target), 40))

    monkeypatch.setenv("OMNISIGHT_RUNNER_STALE_REFRESH_EVERY_N_ITER", "10")
    monkeypatch.setenv("OMNISIGHT_RUNNER_STALE_REFRESH_STRATEGY", "most-edited")
    monkeypatch.setenv("OMNISIGHT_RUNNER_STALE_REFRESH_MAX_TOKENS", "1000")

    dispatcher = ToolDispatcher()

    def edit_handler(payload: dict[str, Any]) -> str:
        return f"edited {payload['file_path']}"

    dispatcher.register("Edit", edit_handler)

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient(dispatcher=dispatcher)
    caplog.set_level(logging.INFO, logger="backend.agents.anthropic_native_client")

    result = await client.run_with_tools(
        prompt="edit it many times",
        tools=["Edit"],
        max_iterations=35,
        enable_cache=False,
    )

    assert result.iterations == 35
    assert result.stop_reason == "max_iterations_exceeded"

    refresh_lines = [
        rec.message
        for rec in caplog.records
        if rec.message.startswith("[stale-refresh-injected]")
    ]
    assert len(refresh_lines) == 3, refresh_lines
    iters_seen = sorted(
        int(line.split("at iter ")[1].split(" ")[0]) for line in refresh_lines
    )
    assert iters_seen == [10, 20, 30]
    for line in refresh_lines:
        assert str(target) in line
        assert "strategy=most-edited" in line


@pytest.mark.asyncio
async def test_refresh_payload_appended_to_user_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """At iter 10 the API call must carry the injected text block."""
    target = tmp_path / "watched.py"
    target.write_text("ALPHA_BETA_GAMMA\n", encoding="utf-8")

    _install_stub_sdk(monkeypatch, _make_edit_tool_use_chain(str(target), 12))

    monkeypatch.setenv("OMNISIGHT_RUNNER_STALE_REFRESH_EVERY_N_ITER", "10")
    monkeypatch.setenv("OMNISIGHT_RUNNER_STALE_REFRESH_STRATEGY", "most-edited")
    monkeypatch.setenv("OMNISIGHT_RUNNER_STALE_REFRESH_MAX_TOKENS", "1000")

    dispatcher = ToolDispatcher()
    dispatcher.register("Edit", lambda payload: f"edited {payload['file_path']}")

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient(dispatcher=dispatcher)
    await client.run_with_tools(
        prompt="go",
        tools=["Edit"],
        max_iterations=11,
        enable_cache=False,
    )

    # Tenth API call (index 9) is where the injection lands.
    tenth_call = client._client.messages.calls[9]  # type: ignore[attr-defined]
    last_user_msg = tenth_call["messages"][-1]
    assert last_user_msg["role"] == "user"
    # The last user message is the tool_results from iter 9; the injection
    # appended an additional text block carrying the file content + marker.
    text_blocks = [
        block
        for block in last_user_msg["content"]
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    assert text_blocks, last_user_msg["content"]
    injected = text_blocks[-1]["text"]
    assert "[stale-refresh-injected]" in injected
    assert "ALPHA_BETA_GAMMA" in injected


# ─── Cost gate skip ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cost_gate_skips_when_estimated_tokens_exceed_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    target = tmp_path / "big.py"
    # ~10k characters → ~2501 tokens, well above a 1-token budget.
    target.write_text("a" * 10_000, encoding="utf-8")

    _install_stub_sdk(monkeypatch, _make_edit_tool_use_chain(str(target), 15))

    monkeypatch.setenv("OMNISIGHT_RUNNER_STALE_REFRESH_EVERY_N_ITER", "10")
    monkeypatch.setenv("OMNISIGHT_RUNNER_STALE_REFRESH_STRATEGY", "most-edited")
    monkeypatch.setenv("OMNISIGHT_RUNNER_STALE_REFRESH_MAX_TOKENS", "1")

    dispatcher = ToolDispatcher()
    dispatcher.register("Edit", lambda payload: f"edited {payload['file_path']}")

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient(dispatcher=dispatcher)
    caplog.set_level(logging.INFO, logger="backend.agents.anthropic_native_client")

    await client.run_with_tools(
        prompt="big file edits",
        tools=["Edit"],
        max_iterations=11,
        enable_cache=False,
    )

    skip_lines = [
        rec.message
        for rec in caplog.records
        if rec.message.startswith("[stale-refresh-skipped]")
    ]
    inject_lines = [
        rec.message
        for rec in caplog.records
        if rec.message.startswith("[stale-refresh-injected]")
    ]
    assert len(skip_lines) == 1, (skip_lines, inject_lines)
    assert "cost-budget-exceeded" in skip_lines[0]
    assert not inject_lines


# ─── No-op when no files touched yet ─────────────────────────────


@pytest.mark.asyncio
async def test_refresh_skipped_when_no_files_touched(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If only read-only tools run, touched_files stays empty → no refresh."""
    responses = [
        _StubResponse(
            content=[
                _StubBlock(
                    type="tool_use",
                    id=f"tu_{i}",
                    name="Read",
                    input={"file_path": "/tmp/whatever"},
                )
            ],
            stop_reason="tool_use",
            usage=_StubUsage(),
        )
        for i in range(15)
    ]
    _install_stub_sdk(monkeypatch, responses)

    monkeypatch.setenv("OMNISIGHT_RUNNER_STALE_REFRESH_EVERY_N_ITER", "10")

    dispatcher = ToolDispatcher()
    dispatcher.register("Read", lambda _: "x")

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient(dispatcher=dispatcher)
    caplog.set_level(logging.INFO, logger="backend.agents.anthropic_native_client")

    await client.run_with_tools(
        prompt="just look",
        tools=["Read"],
        max_iterations=11,
        enable_cache=False,
    )

    assert not [
        rec.message
        for rec in caplog.records
        if rec.message.startswith("[stale-refresh-injected]")
        or rec.message.startswith("[stale-refresh-skipped]")
    ]
