"""AB.2.4 — Tool execution router.

Maps Anthropic `tool_use.name` → OmniSight backend implementation. Each
registered handler:

  - takes an input dict (matches the corresponding ToolSchema.input_schema)
  - returns a JSON-serializable result (str / dict / list / None)
  - may raise — dispatcher catches and returns an error tool_result

Used by `anthropic_native_client.run_with_tools()` to handle tool_use
blocks during the multi-turn loop, and by AB.4 batch dispatcher to
execute pre-computed tool calls.

Registration:

    from backend.agents.tool_dispatcher import register_handler

    @register_handler("Read")
    async def read_handler(input: dict) -> str:
        return Path(input["file_path"]).read_text()

Resolution rules:

  - Sync handlers wrapped to coroutine for uniform await
  - Missing handler → returns error tool_result (does NOT raise) so the
    model can recover via self-correction
  - Schema mismatch (missing required field) → error tool_result with
    structured detail

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §3
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from backend.agents.tool_schemas import get_schema

logger = logging.getLogger(__name__)


HandlerSync = Callable[[dict[str, Any]], Any]
HandlerAsync = Callable[[dict[str, Any]], Awaitable[Any]]
Handler = HandlerSync | HandlerAsync


@dataclass(frozen=True)
class ToolError:
    """Vendor-agnostic tool error envelope."""

    error: str
    error_type: str
    retryable: bool
    hint: str


@dataclass(frozen=True)
class ToolResult:
    """Outcome of a single tool execution.

    Mirrors the shape Anthropic expects in `tool_result` content blocks.
    """

    tool_use_id: str
    content: str
    is_error: bool = False

    def to_anthropic_block(self) -> dict[str, Any]:
        """Serialize to Anthropic content-block shape."""
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "content": self.content,
        }
        if self.is_error:
            block["is_error"] = True
        return block


class ToolDispatcher:
    """Registers and executes tool handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, tool_name: str, handler: Handler) -> Handler:
        """Register a handler for `tool_name`. Raises if already registered."""
        # Validate name is a known schema (drift guard against typos).
        try:
            get_schema(tool_name)
        except KeyError as e:
            raise ValueError(
                f"Cannot register handler for unknown tool {tool_name!r}. "
                "Add the schema to backend/agents/tool_schemas.py first."
            ) from e
        if tool_name in self._handlers:
            raise ValueError(f"Handler for {tool_name!r} already registered")
        self._handlers[tool_name] = handler
        return handler

    def has_handler(self, tool_name: str) -> bool:
        return tool_name in self._handlers

    def registered_tools(self) -> list[str]:
        return sorted(self._handlers)

    async def execute(
        self, tool_use_id: str, tool_name: str, tool_input: dict[str, Any]
    ) -> ToolResult:
        """Execute a tool by name, returning a ToolResult.

        Never raises — exceptions are captured and returned as error
        tool_results so the calling LLM can self-correct.
        """
        handler = self._handlers.get(tool_name)
        if handler is None:
            return ToolResult(
                tool_use_id=tool_use_id,
                content=json.dumps(
                    {
                        "error": "no_handler_registered",
                        "tool_name": tool_name,
                        "registered": self.registered_tools()[:10],
                    }
                ),
                is_error=True,
            )

        try:
            if inspect.iscoroutinefunction(handler):
                raw = await handler(tool_input)
            else:
                # Run sync handlers in default executor to avoid blocking event loop.
                raw = await asyncio.get_running_loop().run_in_executor(
                    None, handler, tool_input
                )
        except Exception as e:  # noqa: BLE001 - boundary, must capture all
            logger.exception("Tool %s raised", tool_name)
            if tool_name in _STRUCTURED_ERROR_TOOLS:
                return _error_result(
                    tool_use_id,
                    _tool_error_from_exception(e),
                    {
                        "tool_name": tool_name,
                        "exception_type": type(e).__name__,
                        "message": str(e)[:1000],
                    },
                )
            return ToolResult(
                tool_use_id=tool_use_id,
                content=json.dumps(
                    {
                        "error": "tool_raised",
                        "tool_name": tool_name,
                        "exception_type": type(e).__name__,
                        "message": str(e)[:1000],
                    }
                ),
                is_error=True,
            )

        if tool_name == "Bash":
            bash_error = _tool_error_from_bash_output(raw)
            if bash_error is not None:
                return _error_result(tool_use_id, bash_error)

        # Normalise to string content.
        if isinstance(raw, str):
            content = raw
        elif raw is None:
            content = ""
        else:
            try:
                content = json.dumps(raw, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                content = str(raw)

        return ToolResult(tool_use_id=tool_use_id, content=content, is_error=False)


_STRUCTURED_ERROR_TOOLS = frozenset({"Read", "Edit", "Bash"})
_BASH_EXIT_RE = re.compile(r"(?m)^EXIT_CODE: (-?\d+)$")
_BASH_TIMEOUT_PREFIX = "❌ command timed out after "
_BASH_TRUNCATED_MARKERS = (
    "(... stdout truncated to last 30KB ...)",
    "(... stderr truncated to last 10KB ...)",
)


def _error_result(
    tool_use_id: str, error: ToolError, extra: dict[str, Any] | None = None
) -> ToolResult:
    payload = asdict(error)
    if extra:
        payload.update(extra)
    content = json.dumps(payload, ensure_ascii=False)
    return ToolResult(tool_use_id=tool_use_id, content=content, is_error=True)


def _tool_error_from_exception(exc: Exception) -> ToolError:
    return ToolError(
        error="tool_raised",
        error_type=type(exc).__name__,
        retryable=isinstance(exc, (TimeoutError, asyncio.TimeoutError)),
        hint=str(exc)[:1000],
    )


def _tool_error_from_bash_output(raw: Any) -> ToolError | None:
    if not isinstance(raw, str):
        return None
    if raw.startswith(_BASH_TIMEOUT_PREFIX):
        return ToolError(
            error="bash_timeout",
            error_type="timeout",
            retryable=True,
            hint=raw[:1000],
        )
    if any(marker in raw for marker in _BASH_TRUNCATED_MARKERS):
        return ToolError(
            error="bash_output_oversized",
            error_type="oversized_output",
            retryable=True,
            hint="Narrow the command or redirect large output to a file.",
        )
    match = _BASH_EXIT_RE.search(raw)
    if match and int(match.group(1)) != 0:
        return ToolError(
            error="bash_nonzero_exit",
            error_type="nonzero_exit",
            retryable=False,
            hint=raw[:1000],
        )
    return None


_default_dispatcher = ToolDispatcher()


def register_handler(tool_name: str) -> Callable[[Handler], Handler]:
    """Decorator: register a handler in the default dispatcher.

        @register_handler("Read")
        async def read_handler(input):
            ...
    """

    def _wrap(fn: Handler) -> Handler:
        _default_dispatcher.register(tool_name, fn)
        return fn

    return _wrap


def get_default_dispatcher() -> ToolDispatcher:
    """Return the module-level default dispatcher."""
    return _default_dispatcher
