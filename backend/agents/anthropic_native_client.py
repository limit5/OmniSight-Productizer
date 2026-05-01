"""AB.2 — Native Anthropic Messages API client with tool use loop.

Direct `anthropic` SDK wrapper, bypassing LangChain abstraction. Used for
AB.3 batch and AB.4 dispatcher hot paths where LangChain overhead is
undesirable. The existing `backend.llm_adapter.build_chat_model("anthropic")`
(LangChain `ChatAnthropic`) remains the multi-provider universal path —
this module is the Anthropic-specific fast path.

Two consumers:

  1. AB.3 Batch API integration — pass `simple_params()` output to
     `client.messages.batches.create()`
  2. AB.4 Real-time dispatcher — call `run_with_tools()` for the full
     multi-turn loop with tool execution

Design:

  - Stateless `AnthropicClient` wraps `anthropic.Anthropic` SDK
  - `simple()` — no tools, straight prompt → response (good for routine
    classification / scoring)
  - `run_with_tools()` — multi-turn loop until `stop_reason="end_turn"`
    or max_iterations, executing tools via `ToolDispatcher`
  - Prompt caching via `cache_control: {"type": "ephemeral"}` on system
    message + tool definitions (90% off cached input on subsequent turns)
  - Token usage aggregated across all turns and returned in `RunResult`

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §3, §4
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal

from backend.agents.tool_dispatcher import ToolDispatcher, get_default_dispatcher
from backend.agents.tool_schemas import to_anthropic_tools

logger = logging.getLogger(__name__)


DEFAULT_MODEL_OPUS = "claude-opus-4-7"
DEFAULT_MODEL_SONNET = "claude-sonnet-4-6"
DEFAULT_MODEL_HAIKU = "claude-haiku-4-5-20251001"

# Hard stop on runaway loops. Real workflows almost never need > 25 turns;
# beyond this we bail with a structured error so the operator can inspect.
DEFAULT_MAX_ITERATIONS = 25


@dataclass(frozen=True)
class TokenUsage:
    """Aggregated token usage across all turns of one run.

    `cache_read` / `cache_creation` are non-zero only when prompt caching
    actually fires (depends on model + cache_control placement).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens
            + other.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
        )


@dataclass(frozen=True)
class RunResult:
    """Outcome of a `run_with_tools()` invocation."""

    final_text: str
    """Concatenated text from the final assistant turn (after tools resolved)."""

    iterations: int
    """How many round-trips with the model occurred."""

    stop_reason: str
    """Anthropic stop_reason on the final turn."""

    usage: TokenUsage
    """Aggregated token usage across all turns."""

    transcript: list[dict[str, Any]] = field(default_factory=list)
    """Full message history (system + user + assistant turns + tool_results)."""

    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """Each entry: {turn, name, input, result, is_error}."""


def _extract_usage(raw_usage: Any) -> TokenUsage:
    """Pull token counts from a `Usage` object or dict, tolerating shape drift."""
    if raw_usage is None:
        return TokenUsage()

    def _get(name: str) -> int:
        if isinstance(raw_usage, dict):
            return int(raw_usage.get(name, 0) or 0)
        return int(getattr(raw_usage, name, 0) or 0)

    return TokenUsage(
        input_tokens=_get("input_tokens"),
        output_tokens=_get("output_tokens"),
        cache_read_input_tokens=_get("cache_read_input_tokens"),
        cache_creation_input_tokens=_get("cache_creation_input_tokens"),
    )


def _content_to_text(content: list[Any]) -> str:
    """Concatenate text blocks from an assistant content list, ignoring tool_use."""
    parts: list[str] = []
    for block in content or []:
        block_type = (
            block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        )
        if block_type == "text":
            text = (
                block.get("text")
                if isinstance(block, dict)
                else getattr(block, "text", "")
            )
            if text:
                parts.append(text)
    return "".join(parts)


def _content_to_dict(content: list[Any]) -> list[dict[str, Any]]:
    """Convert SDK content blocks to plain-dict form for transcript storage."""
    out: list[dict[str, Any]] = []
    for block in content or []:
        if isinstance(block, dict):
            out.append(block)
        else:
            out.append(block.model_dump() if hasattr(block, "model_dump") else dict(block.__dict__))
    return out


def _extract_tool_uses(content: list[Any]) -> list[dict[str, Any]]:
    """Pull tool_use blocks from an assistant message content list."""
    uses: list[dict[str, Any]] = []
    for block in content or []:
        block_type = (
            block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        )
        if block_type != "tool_use":
            continue
        if isinstance(block, dict):
            uses.append(
                {
                    "id": block["id"],
                    "name": block["name"],
                    "input": block.get("input", {}),
                }
            )
        else:
            uses.append(
                {
                    "id": block.id,
                    "name": block.name,
                    "input": getattr(block, "input", {}) or {},
                }
            )
    return uses


def _apply_cache_control(
    system: str | list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
    enable_cache: bool,
) -> tuple[Any, list[dict[str, Any]] | None]:
    """Add `cache_control: {"type": "ephemeral"}` to system + tools when enabled.

    Anthropic prompt caching cuts input cost by 90% on cached blocks, but only
    fires when the same prefix repeats across calls within ~5 min TTL. For a
    multi-turn agent loop with the same system + tools, this is a near-100%
    hit rate from turn 2 onwards.
    """
    if not enable_cache:
        return system, tools

    new_system: Any
    if system is None:
        new_system = None
    elif isinstance(system, str):
        new_system = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    else:
        # Already a list of system blocks — mark only the last.
        new_system = list(system)
        if new_system:
            last = dict(new_system[-1])
            last["cache_control"] = {"type": "ephemeral"}
            new_system[-1] = last

    new_tools = tools
    if tools:
        new_tools = list(tools)
        last_tool = dict(new_tools[-1])
        last_tool["cache_control"] = {"type": "ephemeral"}
        new_tools[-1] = last_tool

    return new_system, new_tools


class AnthropicClient:
    """Direct Anthropic SDK client with tool-use loop + prompt caching."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = DEFAULT_MODEL_SONNET,
        max_tokens_default: int = 8192,
        dispatcher: ToolDispatcher | None = None,
    ) -> None:
        # Lazy import so test code can monkeypatch easily and so unrelated
        # callers don't pull anthropic SDK into their import graph.
        import anthropic

        self._sdk = anthropic
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set and no api_key passed. "
                "Set the env var or pass api_key explicitly."
            )

        self._client = anthropic.Anthropic(api_key=resolved_key)
        self.default_model = default_model
        self.max_tokens_default = max_tokens_default
        self.dispatcher = dispatcher or get_default_dispatcher()

    @property
    def messages(self):  # noqa: ANN201 - returns SDK proxy, dynamic
        """Expose the SDK `messages` namespace for low-level access (batches, etc)."""
        return self._client.messages

    def simple(
        self,
        *,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 1.0,
    ) -> tuple[str, TokenUsage]:
        """One-shot prompt → text response. No tools, no loop.

        Good fit for: routine classification, scoring, summarization.
        """
        kwargs = {
            "model": model or self.default_model,
            "max_tokens": max_tokens or self.max_tokens_default,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        response = self._client.messages.create(**kwargs)
        text = _content_to_text(response.content)
        usage = _extract_usage(getattr(response, "usage", None))
        return text, usage

    def simple_params(
        self,
        *,
        prompt: str,
        tools: list[str] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 1.0,
        enable_cache: bool = True,
    ) -> dict[str, Any]:
        """Build a `params` dict for batch submission.

        AB.3 batch dispatcher uses this to construct each request entry of
        a `messages.batches.create()` call. Identical shape to what
        `messages.create()` accepts.
        """
        tool_payload = to_anthropic_tools(tools) if tools else None
        sys_blocks, tool_payload = _apply_cache_control(system, tool_payload, enable_cache)

        params: dict[str, Any] = {
            "model": model or self.default_model,
            "max_tokens": max_tokens or self.max_tokens_default,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if sys_blocks is not None:
            params["system"] = sys_blocks
        if tool_payload:
            params["tools"] = tool_payload
        return params

    async def run_with_tools(
        self,
        *,
        prompt: str,
        tools: list[str] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        temperature: float = 1.0,
        enable_cache: bool = True,
        on_tool_call: (
            Literal["log", "silent"]
            | None
        ) = "log",
    ) -> RunResult:
        """Execute multi-turn tool-use loop until `stop_reason="end_turn"`.

        Loop guard: bails with `stop_reason="max_iterations_exceeded"` if the
        model keeps calling tools beyond `max_iterations`. This is a hard
        safety, not a polite request — runaway loops burn tokens fast.

        Tool execution: each `tool_use` block in an assistant turn is
        dispatched via `self.dispatcher.execute()`. Errors during tool
        execution are returned as `is_error=True` tool_results, allowing
        the model to self-correct.

        Prompt caching: when `enable_cache=True` (default), system + tools
        are marked with `cache_control: ephemeral`. From turn 2 onwards
        Anthropic charges 10% (90% off) for the cached prefix, dramatically
        cutting cost for long agent loops.
        """
        tool_payload = to_anthropic_tools(tools) if tools else None
        sys_blocks, tool_payload = _apply_cache_control(system, tool_payload, enable_cache)

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        transcript: list[dict[str, Any]] = []
        if sys_blocks is not None:
            transcript.append({"role": "system", "content": sys_blocks})
        transcript.append({"role": "user", "content": prompt})

        tool_calls_log: list[dict[str, Any]] = []
        total_usage = TokenUsage()
        iterations = 0
        stop_reason = "unknown"
        final_text = ""

        while iterations < max_iterations:
            iterations += 1
            kwargs: dict[str, Any] = {
                "model": model or self.default_model,
                "max_tokens": max_tokens or self.max_tokens_default,
                "temperature": temperature,
                "messages": messages,
            }
            if sys_blocks is not None:
                kwargs["system"] = sys_blocks
            if tool_payload:
                kwargs["tools"] = tool_payload

            response = self._client.messages.create(**kwargs)
            content = _content_to_dict(response.content)
            stop_reason = getattr(response, "stop_reason", "unknown") or "unknown"
            total_usage = total_usage + _extract_usage(getattr(response, "usage", None))

            transcript.append({"role": "assistant", "content": content})
            messages.append({"role": "assistant", "content": content})

            if stop_reason != "tool_use":
                final_text = _content_to_text(content)
                break

            # Resolve every tool_use in this turn before continuing.
            tool_uses = _extract_tool_uses(content)
            tool_results_blocks: list[dict[str, Any]] = []
            for tu in tool_uses:
                if on_tool_call == "log":
                    logger.info(
                        "tool_call iter=%d name=%s id=%s",
                        iterations,
                        tu["name"],
                        tu["id"],
                    )
                result = await self.dispatcher.execute(
                    tool_use_id=tu["id"],
                    tool_name=tu["name"],
                    tool_input=tu["input"],
                )
                tool_results_blocks.append(result.to_anthropic_block())
                tool_calls_log.append(
                    {
                        "turn": iterations,
                        "name": tu["name"],
                        "input": tu["input"],
                        "tool_use_id": tu["id"],
                        "result": result.content[:500],
                        "is_error": result.is_error,
                    }
                )

            # Feed all tool_results back in one user message — Anthropic's
            # convention for multi-tool responses in a single turn.
            tr_msg = {"role": "user", "content": tool_results_blocks}
            messages.append(tr_msg)
            transcript.append(tr_msg)

        else:
            # Loop exhausted without `break` — i.e. iterations == max_iterations
            # and the last response was still tool_use.
            stop_reason = "max_iterations_exceeded"
            logger.warning(
                "run_with_tools hit max_iterations=%d; bailing", max_iterations
            )

        return RunResult(
            final_text=final_text,
            iterations=iterations,
            stop_reason=stop_reason,
            usage=total_usage,
            transcript=transcript,
            tool_calls=tool_calls_log,
        )
