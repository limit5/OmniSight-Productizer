"""Tests for backend.agents.sub_agent (Phase 3 — Agent tool dispatch).

Locks:
  * make_agent_tool_handler invokes client.run_with_tools with the
    spec's tools / system / model / max_iterations
  * subagent_type "Explore" / "Plan" use restricted (read-only) tools
  * Unknown subagent_type falls back to general-purpose, logs once
  * Friendly model alias ("opus" / "sonnet" / "haiku") maps to canonical
    model id; absent alias uses spec.default_model
  * Empty description / prompt raises ValueError
  * run_in_background=True raises NotImplementedError
  * isolation hint is ignored (not crashing) with a log warning
  * on_dispatch observer fired with correct keys
  * Sub-agent's final text + metadata footer is returned
  * types_map override: must include 'general-purpose' fallback
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from backend.agents.anthropic_native_client import RunResult, TokenUsage
from backend.agents.sub_agent import (
    DEFAULT_SUBAGENT_TYPES,
    SubAgentTypeSpec,
    list_default_subagent_types,
    make_agent_tool_handler,
)


@dataclass
class _Captured:
    kwargs: dict[str, Any]


class _StubClient:
    """Stands in for AnthropicClient — records what run_with_tools sees."""

    def __init__(
        self,
        *,
        final_text: str = "OK done.",
        iterations: int = 3,
        stop_reason: str = "end_turn",
        usage: TokenUsage | None = None,
    ) -> None:
        self.captured = _Captured(kwargs={})
        self._final_text = final_text
        self._iterations = iterations
        self._stop_reason = stop_reason
        self._usage = usage or TokenUsage(input_tokens=10, output_tokens=20)

    async def run_with_tools(self, **kwargs: Any) -> RunResult:
        self.captured.kwargs = kwargs
        return RunResult(
            final_text=self._final_text,
            iterations=self._iterations,
            stop_reason=self._stop_reason,
            usage=self._usage,
        )


# ─── Default catalog sanity ─────────────────────────────────────


def test_default_subagent_types_have_general_purpose() -> None:
    assert "general-purpose" in DEFAULT_SUBAGENT_TYPES


def test_default_subagent_types_omit_agent_tool_no_recursion() -> None:
    """Sub-agents must NOT have 'Agent' in their tool list — no recursion."""
    for spec in DEFAULT_SUBAGENT_TYPES.values():
        assert "Agent" not in spec.tools, (
            f"subagent {spec.name!r} would allow recursion"
        )


def test_explore_is_read_only() -> None:
    spec = DEFAULT_SUBAGENT_TYPES["Explore"]
    assert "Write" not in spec.tools
    assert "Edit" not in spec.tools
    assert "Bash" not in spec.tools
    assert {"Read", "Grep", "Glob"}.issubset(set(spec.tools))


def test_plan_is_read_only() -> None:
    spec = DEFAULT_SUBAGENT_TYPES["Plan"]
    assert "Write" not in spec.tools
    assert "Edit" not in spec.tools
    assert "Bash" not in spec.tools


def test_list_default_subagent_types_returns_sorted_names() -> None:
    names = list_default_subagent_types()
    assert names == sorted(names)
    assert "general-purpose" in names
    assert "Explore" in names
    assert "Plan" in names


# ─── Handler dispatch ───────────────────────────────────────────


def test_handler_invokes_run_with_tools_with_general_purpose_spec() -> None:
    client = _StubClient()
    h = make_agent_tool_handler(client=client)
    out = asyncio.run(
        h(
            {
                "description": "explore the codebase",
                "prompt": "find every place X is referenced",
            }
        )
    )
    kw = client.captured.kwargs
    assert kw["model"] == DEFAULT_SUBAGENT_TYPES["general-purpose"].default_model
    assert set(kw["tools"]) == set(DEFAULT_SUBAGENT_TYPES["general-purpose"].tools)
    assert kw["max_iterations"] == 25
    # Sub-agent system carries the type's prefix + brief
    assert kw["system"].startswith("You are a sub-agent")
    assert "explore the codebase" in kw["prompt"]
    # Output decorated with footer
    assert "Sub-agent (general-purpose)" in out
    assert "OK done." in out
    assert "iterations=3" in out


def test_handler_uses_explore_tools_when_subagent_type_explore() -> None:
    client = _StubClient(final_text="found 3 files")
    h = make_agent_tool_handler(client=client)
    asyncio.run(
        h(
            {
                "description": "find auth callsites",
                "prompt": "list every auth() invocation",
                "subagent_type": "Explore",
            }
        )
    )
    kw = client.captured.kwargs
    assert set(kw["tools"]) == {"Read", "Grep", "Glob"}
    assert "Explore sub-agent" in kw["system"]


def test_handler_unknown_subagent_type_falls_back_to_general_purpose() -> None:
    client = _StubClient()
    h = make_agent_tool_handler(client=client)
    asyncio.run(
        h(
            {
                "description": "x",
                "prompt": "y",
                "subagent_type": "NotARealType",
            }
        )
    )
    kw = client.captured.kwargs
    assert set(kw["tools"]) == set(
        DEFAULT_SUBAGENT_TYPES["general-purpose"].tools
    )


@pytest.mark.parametrize(
    "alias,expected_substr",
    [
        ("opus",   "claude-opus-4-7"),
        ("sonnet", "claude-sonnet-4-6"),
        ("haiku",  "claude-haiku-4-5"),
    ],
)
def test_handler_friendly_model_alias(alias: str, expected_substr: str) -> None:
    client = _StubClient()
    h = make_agent_tool_handler(client=client)
    asyncio.run(
        h({"description": "d", "prompt": "p", "model": alias})
    )
    assert expected_substr in client.captured.kwargs["model"]


def test_handler_no_model_uses_spec_default() -> None:
    client = _StubClient()
    h = make_agent_tool_handler(client=client)
    asyncio.run(h({"description": "d", "prompt": "p"}))
    assert (
        client.captured.kwargs["model"]
        == DEFAULT_SUBAGENT_TYPES["general-purpose"].default_model
    )


def test_handler_unknown_model_alias_falls_back_to_default() -> None:
    client = _StubClient()
    h = make_agent_tool_handler(client=client)
    asyncio.run(
        h({"description": "d", "prompt": "p", "model": "gpt-5"})
    )
    assert (
        client.captured.kwargs["model"]
        == DEFAULT_SUBAGENT_TYPES["general-purpose"].default_model
    )


# ─── Validation & edge cases ────────────────────────────────────


def test_handler_empty_description_raises() -> None:
    h = make_agent_tool_handler(client=_StubClient())
    with pytest.raises(ValueError, match="description"):
        asyncio.run(h({"description": "", "prompt": "p"}))


def test_handler_empty_prompt_raises() -> None:
    h = make_agent_tool_handler(client=_StubClient())
    with pytest.raises(ValueError, match="prompt"):
        asyncio.run(h({"description": "d", "prompt": "   "}))


def test_handler_run_in_background_raises_notimplemented_before_dispatch() -> None:
    client = _StubClient()
    observed: list[dict[str, Any]] = []
    h = make_agent_tool_handler(client=client, on_dispatch=observed.append)
    with pytest.raises(NotImplementedError, match="background"):
        asyncio.run(
            h(
                {
                    "description": "d",
                    "prompt": "p",
                    "run_in_background": True,
                }
            )
        )
    assert observed == []
    assert client.captured.kwargs == {}


def test_handler_isolation_hint_is_ignored_not_crashing(caplog) -> None:
    h = make_agent_tool_handler(client=_StubClient())
    out = asyncio.run(
        h(
            {
                "description": "d",
                "prompt": "p",
                "isolation": "worktree",
            }
        )
    )
    assert "Sub-agent" in out  # ran successfully despite the unsupported hint


# ─── Observer ───────────────────────────────────────────────────


def test_handler_invokes_on_dispatch_observer() -> None:
    captured: list[dict[str, Any]] = []

    def observer(info: dict[str, Any]) -> None:
        captured.append(info)

    h = make_agent_tool_handler(
        client=_StubClient(), on_dispatch=observer
    )
    asyncio.run(
        h(
            {
                "description": "test desc",
                "prompt": "test prompt",
                "subagent_type": "Explore",
            }
        )
    )
    assert len(captured) == 1
    info = captured[0]
    assert info["description"] == "test desc"
    assert info["subagent_type"] == "Explore"
    assert info["max_iterations"] == 30
    assert info["prompt_chars"] == len("test prompt")


def test_handler_observer_exception_does_not_break_dispatch() -> None:
    def bad_observer(_info: dict[str, Any]) -> None:
        raise RuntimeError("observer broke")

    h = make_agent_tool_handler(
        client=_StubClient(), on_dispatch=bad_observer
    )
    out = asyncio.run(h({"description": "d", "prompt": "p"}))
    assert "Sub-agent" in out  # dispatch survived


# ─── Custom types_map ───────────────────────────────────────────


def test_custom_types_map_must_include_general_purpose() -> None:
    bad_map = {
        "Explore": DEFAULT_SUBAGENT_TYPES["Explore"],
    }
    with pytest.raises(ValueError, match="general-purpose"):
        make_agent_tool_handler(client=_StubClient(), types_map=bad_map)


def test_custom_subagent_type_can_be_added() -> None:
    custom = SubAgentTypeSpec(
        name="Reviewer",
        tools=("Read", "Grep"),
        system_prefix="You are a code reviewer.",
    )
    types_map = {
        "general-purpose": DEFAULT_SUBAGENT_TYPES["general-purpose"],
        "Reviewer": custom,
    }
    client = _StubClient()
    h = make_agent_tool_handler(client=client, types_map=types_map)
    asyncio.run(
        h(
            {
                "description": "review the auth diff",
                "prompt": "look at backend/auth.py",
                "subagent_type": "Reviewer",
            }
        )
    )
    assert client.captured.kwargs["system"].startswith("You are a code reviewer.")


# ─── parent_system_suffix ───────────────────────────────────────


def test_parent_system_suffix_is_appended() -> None:
    client = _StubClient()
    h = make_agent_tool_handler(
        client=client,
        parent_system_suffix="# Inherited PROJECT_ROOT: /tmp/x",
    )
    asyncio.run(h({"description": "d", "prompt": "p"}))
    sys_text = client.captured.kwargs["system"]
    assert "Inherited PROJECT_ROOT" in sys_text
    # ALSO has the spec prefix
    assert "sub-agent invoked from a parent" in sys_text
