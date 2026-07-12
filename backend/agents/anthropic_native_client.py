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
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from backend.agents.anthropic_sdk_audit import assert_no_deprecated_beta_messages_call
from backend.agents.mcp_integration import enforce_mcp_policy
from backend.agents.provenance import (
    CaptureUnavailable,
    STALE_REFRESH,
    TOOL_RESULT,
    SnapshotCache,
    for_model_response,
    provenance_scope,
    record_content,
)
from backend.agents.stale_refresh_strategy import (
    build_refresh_marker,
    build_skip_marker,
    estimate_tokens,
    extract_mutated_path,
    get_refresh_every_n,
    get_refresh_max_tokens,
    get_refresh_strategy,
    pick_refresh_target,
    read_file_for_refresh,
)
from backend.agents.system_prompt_builder import inject_tool_catalog
from backend.agents.tool_dispatcher import ToolDispatcher, get_default_dispatcher
from backend.agents.tool_schemas import to_anthropic_tools

if TYPE_CHECKING:
    from backend.agents.execution_context import ExecutionContext

logger = logging.getLogger(__name__)

# U6-0 P-ID-B: task-local channel that carries the ExecutionContext to
# registered handlers that cannot receive the dispatcher keyword param
# (e.g. the nested sub-agent handler). Published for the duration of each
# `run_with_tools` loop; a ContextVar (not a client attribute) so a
# per-call override in one asyncio task never leaks into another.
_active_execution_context: ContextVar["ExecutionContext | None"] = ContextVar(
    "omnisight_active_execution_context", default=None
)


def get_active_execution_context() -> "ExecutionContext | None":
    """Return the ExecutionContext of the innermost active `run_with_tools`
    loop in this task, or ``None`` when no context is active (dormant)."""
    return _active_execution_context.get()


# U6-0 T5b-1a: process-local, NON-authoritative snapshot cache (T5a
# docstring) — a grant will NOT bind via this cache; the durable store is
# T9/T10. Sealed per-turn snapshots land here so audit ids are resolvable
# in-process.
_SNAPSHOT_CACHE = SnapshotCache()


DEFAULT_MODEL_OPUS = "claude-opus-4-7"
DEFAULT_MODEL_SONNET = "claude-sonnet-4-6"
DEFAULT_MODEL_HAIKU = "claude-haiku-4-5-20251001"

# Hard stop on runaway loops. Real workflows almost never need > 25 turns;
# beyond this we bail with a structured error so the operator can inspect.
DEFAULT_MAX_ITERATIONS = 25

# U6-0 P-PROV-B: provider-side tool types the client refuses to offer via
# `raw_tools` — code runs on Anthropic infrastructure outside every OmniSight
# containment layer. Single place to add future provider-side types; widening
# (removing an entry) requires a containment review (H0 pattern).
FORBIDDEN_PROVIDER_TOOL_TYPE_PREFIXES = ("code_execution",)

# U6-0 P-PROV-B: explicit MCP connector beta pin. The SDK does NOT
# auto-inject an `mcp-client-*` version, so without this pin our
# payload-shape assumptions (`tool_configuration.allowed_tools`) ride an
# implicit server-side default. Applied PER-REQUEST (only when mcp_servers
# survive policy), merged with — never clobbering — the ctor beta_headers.
# Migrating to the current `mcp_toolset` format is a tracked follow-up.
MCP_CONNECTOR_BETA = "mcp-client-2025-04-04"


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


def _apply_message_cache_control(
    messages: list[dict[str, Any]], enable_cache: bool,
) -> list[dict[str, Any]]:
    """Mark the last two messages as Anthropic ephemeral cache breakpoints."""
    if not enable_cache:
        return messages

    cached = [dict(message) for message in messages]
    start = max(0, len(cached) - 2)
    for idx in range(start, len(cached)):
        message = cached[idx]
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        elif isinstance(content, list) and content:
            blocks = list(content)
            last = dict(blocks[-1])
            last["cache_control"] = {"type": "ephemeral"}
            blocks[-1] = last
            message["content"] = blocks
        cached[idx] = message
    return cached


def _strip_cache_control(value: Any) -> Any:
    """Remove Anthropic cache_control keys from a request payload."""
    if isinstance(value, dict):
        return {
            key: _strip_cache_control(item)
            for key, item in value.items()
            if key != "cache_control"
        }
    if isinstance(value, list):
        return [_strip_cache_control(item) for item in value]
    return value


def _is_cache_control_rejection(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code is not None and int(status_code) != 400:
        return False

    body = getattr(exc, "body", None)
    text = f"{body} {exc}".lower()
    return "cache_control" in text or "cache breakpoint" in text


def _tool_type_requires_beta_messages(tool_type: str) -> bool:
    """Return True for Anthropic beta built-in tool type strings."""
    return tool_type.startswith(
        (
            "bash_",
            "code_execution_",
            "computer_",
            "memory_",
            "text_editor_",
        )
    )


def _requires_beta_messages(kwargs: dict[str, Any]) -> bool:
    """Detect request shapes that must use ``client.beta.messages``."""
    if kwargs.get("mcp_servers"):
        return True

    for tool in kwargs.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        tool_type = str(tool.get("type", ""))
        if _tool_type_requires_beta_messages(tool_type):
            return True
    return False


def _messages_namespace(client: Any, kwargs: dict[str, Any]) -> tuple[Any, bool]:
    """Pick stable vs beta messages namespace for a request payload."""
    requires_beta = _requires_beta_messages(kwargs)
    beta_messages = getattr(getattr(client, "beta", None), "messages", None)
    used_beta = requires_beta and beta_messages is not None
    assert_no_deprecated_beta_messages_call(
        requires_beta_messages=requires_beta,
        used_beta_messages=used_beta,
    )
    if used_beta:
        return beta_messages, True
    return client.messages, False


def _create_message_with_cache_fallback(client: Any, kwargs: dict[str, Any]) -> Any:
    messages, _used_beta = _messages_namespace(client, kwargs)
    try:
        return messages.create(**kwargs)
    except Exception as exc:
        if not _is_cache_control_rejection(exc):
            raise
        logger.warning(
            "cache_breakpoint_rejected_by_api; retrying Anthropic call without cache_control"
        )
        stripped = _strip_cache_control(kwargs)
        messages, _used_beta = _messages_namespace(client, stripped)
        return messages.create(**stripped)


class AnthropicClient:
    """Direct Anthropic SDK client with tool-use loop + prompt caching."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = DEFAULT_MODEL_SONNET,
        max_tokens_default: int = 8192,
        dispatcher: ToolDispatcher | None = None,
        beta_headers: list[str] | None = None,
        execution_context: "ExecutionContext | None" = None,
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

        # OP-851 (C1): beta headers — comma-joined per Anthropic spec.
        # Pinned at construction so every messages.create() in this
        # client's lifetime carries the same beta surface; the policy
        # is "one beta set per client" to avoid the cache-key drift
        # that would otherwise blow the prompt cache.
        client_kwargs: dict[str, Any] = {"api_key": resolved_key}
        if beta_headers:
            client_kwargs["default_headers"] = {
                "anthropic-beta": ",".join(beta_headers),
            }
        self._client = anthropic.Anthropic(**client_kwargs)
        self.default_model = default_model
        self.max_tokens_default = max_tokens_default
        self.dispatcher = dispatcher or get_default_dispatcher()
        self.beta_headers = tuple(beta_headers) if beta_headers else ()
        # U6-0 P-ID-B: per-run default identity injection point (P-ID-C
        # populates it at the launchers). The `run_with_tools` per-call
        # param overrides this attribute; dormant while None.
        self.execution_context = execution_context

    @property
    def messages(self) -> Any:
        """Expose the SDK `messages` namespace for low-level access (batches, etc)."""
        return self._client.messages

    @staticmethod
    def _maybe_inject_stale_refresh(
        *,
        iteration: int,
        messages: list[dict[str, Any]],
        transcript: list[dict[str, Any]],
        touched_files: dict[str, int],
        every_n: int,
        strategy: str,
        max_tokens: int,
    ) -> tuple[str, str] | None:
        """C5 refresh: every ``every_n`` iterations append a synthetic view block.

        The block is appended as an extra ``text`` content block on the most
        recent ``user`` message — keeps the user/assistant alternation that
        Anthropic enforces while making the fresh file content visible on the
        next API call. Cost-gated by ``max_tokens`` to bound the injection.

        Returns ``(target, injected_text)`` — the SAME string appended as
        the ``injected_block`` text — when a refresh was injected, else
        ``None`` (U6-0 T5b-1a: purely additive; callers may ignore it).
        """
        if every_n <= 0 or iteration % every_n != 0:
            return None
        if not touched_files or not messages or messages[-1].get("role") != "user":
            return None

        target = pick_refresh_target(
            touched_files, strategy, iteration=iteration
        )
        if not target:
            return None

        content = read_file_for_refresh(target)
        if content is None:
            return None

        tokens = estimate_tokens(content)
        if tokens > max_tokens:
            logger.info(build_skip_marker(target, iteration, tokens, max_tokens))
            return None

        marker = build_refresh_marker(target, iteration, strategy)
        logger.info(marker)
        injected_text = f"{marker}\n\n{content}"
        injected_block = {"type": "text", "text": injected_text}

        last_msg = messages[-1]
        existing = last_msg.get("content")
        if isinstance(existing, list):
            last_msg["content"] = [*existing, injected_block]
        elif isinstance(existing, str):
            last_msg["content"] = [
                {"type": "text", "text": existing},
                injected_block,
            ]
        else:
            last_msg["content"] = [injected_block]
        transcript.append(
            {"role": "user", "content": [injected_block], "stale_refresh": True}
        )
        return (target, injected_text)

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
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "max_tokens": max_tokens or self.max_tokens_default,
            "temperature": temperature,
            "messages": _apply_message_cache_control(
                [{"role": "user", "content": prompt}], True
            ),
        }
        if system:
            kwargs["system"] = system

        response = _create_message_with_cache_fallback(self._client, kwargs)
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
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build a `params` dict for batch submission.

        AB.3 batch dispatcher uses this to construct each request entry of
        a `messages.batches.create()` call. Identical shape to what
        `messages.create()` accepts.

        AB.5.6: ``mcp_servers`` is forwarded as Anthropic's
        ``mcp_servers=[]`` parameter so SDK auto-injects remote MCP tool
        definitions (Figma / Gmail / Calendar / Drive). Caller typically
        builds via ``RemoteMCPRegistry.to_anthropic_mcp_servers()``.

        U6-0 P-PROV-B: this method is a FINAL client boundary for
        provider-side surfaces — ``mcp_servers`` input is re-validated via
        :func:`backend.agents.mcp_integration.enforce_mcp_policy` regardless
        of who built it (defense in depth over the registry's own filter).
        Denied/unknown/origin-mismatched entries are dropped; when nothing
        survives, the param is omitted. Widening MCP forwarding here
        requires a containment review (H0 pattern). Residual: the batch
        lane carries NO ``mcp-client-*`` beta pin — headers live in the
        batch client, not in this params dict.
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
        if mcp_servers:
            allowed_mcp_servers = enforce_mcp_policy(mcp_servers)
            if allowed_mcp_servers:
                params["mcp_servers"] = allowed_mcp_servers
        return params

    async def run_with_tools(
        self,
        *,
        prompt: str,
        tools: list[str] | None = None,
        raw_tools: list[dict[str, Any]] | None = None,
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
        mcp_servers: list[dict[str, Any]] | None = None,
        execution_context: "ExecutionContext | None" = None,
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

        OP-828 (B1): when ``raw_tools`` is supplied, the caller has already
        constructed the Anthropic ``tools=[]`` payload (e.g. for built-in
        ``text_editor_20250728`` / ``bash_20250124``), so we skip
        ``to_anthropic_tools`` translation and the OmniSight tool-catalog
        system block injection (built-in tools are documented by Anthropic,
        not by us).

        U6-0 P-PROV-B: this method is a FINAL client boundary for
        provider-side surfaces:

          * ``raw_tools`` entries whose type starts with a
            :data:`FORBIDDEN_PROVIDER_TOOL_TYPE_PREFIXES` prefix raise
            ``ValueError`` — a programming-error guard (the model never
            chooses ``raw_tools``), because those tools execute on
            Anthropic infrastructure outside OmniSight containment.
          * ``mcp_servers`` is re-validated via
            :func:`backend.agents.mcp_integration.enforce_mcp_policy`
            regardless of who built it; denied/unknown/origin-mismatched
            entries are dropped, and when nothing survives the param is
            omitted.
          * requests that carry surviving ``mcp_servers`` pin
            :data:`MCP_CONNECTOR_BETA` via per-request ``betas=``, merged
            with the ctor ``beta_headers`` (a per-request ``betas=``
            REPLACES the client-level default header — clobbering would
            silently drop e.g. managed-agents). Migrating to the current
            ``mcp_toolset`` format is a tracked follow-up.

        Adding a provider-side tool type or widening MCP forwarding here
        requires a containment review (H0 pattern).
        """
        if raw_tools is not None and tools:
            raise ValueError(
                "run_with_tools: pass either `tools` or `raw_tools`, not both"
            )
        if raw_tools is not None:
            for entry in raw_tools:
                if not isinstance(entry, dict):
                    continue
                entry_type = str(entry.get("type", ""))
                if entry_type.startswith(FORBIDDEN_PROVIDER_TOOL_TYPE_PREFIXES):
                    raise ValueError(
                        "run_with_tools: provider-side tool type "
                        f"{entry_type!r} is forbidden at the client boundary "
                        "(matches FORBIDDEN_PROVIDER_TOOL_TYPE_PREFIXES="
                        f"{FORBIDDEN_PROVIDER_TOOL_TYPE_PREFIXES!r}); "
                        "offering it requires a containment review"
                    )
        # U6-0 P-ID-B: the per-call param is authoritative for this call —
        # a sub-agent override must never clobber the parent's attribute,
        # so `self.execution_context` is read but never mutated here.
        effective = (
            execution_context
            if execution_context is not None
            else self.execution_context
        )
        if raw_tools is not None:
            tool_payload = list(raw_tools)
            catalog_system = system
        else:
            tool_payload = to_anthropic_tools(tools) if tools else None
            catalog_system = (
                inject_tool_catalog(system or "", tools) if tools else system
            )
        sys_blocks, tool_payload = _apply_cache_control(
            catalog_system, tool_payload, enable_cache
        )

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

        # U6-0 P-PROV-B: MCP policy at the final client boundary — mcp_servers
        # is loop-invariant, so enforce once here (not per iteration, which
        # would re-emit the drop warnings every turn). Non-empty survivors
        # always force beta routing (_requires_beta_messages), so the
        # per-request `betas=` pin is safe; it MERGES the ctor beta_headers
        # because a per-request betas= REPLACES the client default header.
        request_betas: list[str] | None = None
        if mcp_servers:
            mcp_servers = enforce_mcp_policy(mcp_servers)
            if mcp_servers:
                request_betas = list(
                    dict.fromkeys([*self.beta_headers, MCP_CONNECTOR_BETA])
                )

        # C5 semantic-drift refresh policy (SP-B-X-006 / OP-1064).
        touched_files: dict[str, int] = {}
        refresh_every_n = get_refresh_every_n()
        refresh_strategy = get_refresh_strategy()
        refresh_max_tokens = get_refresh_max_tokens()

        # U6-0 P-ID-B: publish the effective context for the duration of the
        # tool-use loop so registered handlers (which cannot receive the
        # dispatcher keyword param) can read it via
        # `get_active_execution_context()`. Token-reset keeps this nesting-
        # and task-safe.
        token = _active_execution_context.set(effective)
        try:
            # U6-0 T5b-1a: one FRESH provenance collector per run_with_tools
            # invocation (a nested sub-agent run gets its OWN scope, mirroring
            # the per-invocation execution-context token). The collector
            # accumulates across iterations WITHIN this invocation — the
            # message list is itself cumulative — and seal() never resets it.
            with provenance_scope() as _pcol:
                while iterations < max_iterations:
                    iterations += 1
                    refreshed = self._maybe_inject_stale_refresh(
                        iteration=iterations,
                        messages=messages,
                        transcript=transcript,
                        touched_files=touched_files,
                        every_n=refresh_every_n,
                        strategy=refresh_strategy,
                        max_tokens=refresh_max_tokens,
                    )
                    if refreshed is not None:
                        record_content(
                            _pcol, STALE_REFRESH, refreshed[0], refreshed[1]
                        )
                    kwargs: dict[str, Any] = {
                        "model": model or self.default_model,
                        "max_tokens": max_tokens or self.max_tokens_default,
                        "temperature": temperature,
                        "messages": _apply_message_cache_control(
                            messages, enable_cache
                        ),
                    }
                    if sys_blocks is not None:
                        kwargs["system"] = sys_blocks
                    if tool_payload:
                        kwargs["tools"] = tool_payload
                    if mcp_servers:
                        kwargs["mcp_servers"] = mcp_servers
                        kwargs["betas"] = list(request_betas or [])

                    # Seal ONCE per iteration, immediately before the model
                    # call — best-effort: provenance never breaks the turn.
                    # CaptureUnavailable is not grant-eligible (fail-closed
                    # for a future grant).
                    try:
                        _turn_prov = for_model_response(_pcol.seal(_SNAPSHOT_CACHE))
                    except Exception:  # noqa: BLE001 — hot loop, degrade only
                        _turn_prov = CaptureUnavailable("seal_failed")

                    response = _create_message_with_cache_fallback(
                        self._client, kwargs
                    )
                    content = _content_to_dict(response.content)
                    stop_reason = (
                        getattr(response, "stop_reason", "unknown") or "unknown"
                    )
                    total_usage = total_usage + _extract_usage(
                        getattr(response, "usage", None)
                    )

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
                            execution_context=effective,
                            turn_provenance=_turn_prov,
                        )
                        # §2.E temporal rule: turn N's result enters turn
                        # N+1's snapshot (turn N's actions reference the
                        # pre-N frame that was actually sent).
                        record_content(_pcol, TOOL_RESULT, tu["id"], result.content)
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
                        if not result.is_error:
                            mutated_path = extract_mutated_path(tu)
                            if mutated_path:
                                touched_files[mutated_path] = (
                                    touched_files.get(mutated_path, 0) + 1
                                )

                    # Feed all tool_results back in one user message —
                    # Anthropic's convention for multi-tool responses in a
                    # single turn.
                    tr_msg = {"role": "user", "content": tool_results_blocks}
                    messages.append(tr_msg)
                    transcript.append(tr_msg)

                else:
                    # Loop exhausted without `break` — i.e. iterations ==
                    # max_iterations and the last response was still tool_use.
                    stop_reason = "max_iterations_exceeded"
                    logger.warning(
                        "run_with_tools hit max_iterations=%d; bailing",
                        max_iterations,
                    )
        finally:
            _active_execution_context.reset(token)

        return RunResult(
            final_text=final_text,
            iterations=iterations,
            stop_reason=stop_reason,
            usage=total_usage,
            transcript=transcript,
            tool_calls=tool_calls_log,
        )
