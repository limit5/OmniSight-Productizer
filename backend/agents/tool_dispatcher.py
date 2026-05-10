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
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from backend.agents.telemetry.tool_invocation import (
    args_size_bytes,
    emit_tool_invocation,
)
from backend.agents.tool_schemas import get_schema

logger = logging.getLogger(__name__)


HandlerSync = Callable[[dict[str, Any]], Any]
HandlerAsync = Callable[[dict[str, Any]], Awaitable[Any]]
Handler = HandlerSync | HandlerAsync

_TOOL_SUMMARY_OVERRIDES: dict[str, str] = {
    "Read": "read file",
    "Write": "write file",
    "Edit": "in-place edit",
    "Bash": "shell",
    "Grep": "search",
    "Glob": "find files",
    "Agent": "sub-agent",
    "WebFetch": "fetch URL",
    "ToolSearch": "tool discovery",
    "Skill": "run skill",
}


@dataclass(frozen=True)
class ToolError:
    """Vendor-agnostic tool error envelope."""

    error: str
    error_type: str
    retryable: bool
    hint: str
    suggested_tool: str | None = None
    suggested_args: dict[str, Any] | None = None


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
        started_at = time.perf_counter()
        input_size = args_size_bytes(tool_input)
        handler = self._handlers.get(tool_name)
        if handler is None:
            emit_tool_invocation(
                tool_name,
                (time.perf_counter() - started_at) * 1000,
                False,
                input_size,
            )
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
            emit_tool_invocation(
                tool_name,
                (time.perf_counter() - started_at) * 1000,
                False,
                input_size,
            )
            return _error_result(
                tool_use_id=tool_use_id,
                error=_tool_error_from_exception(e, tool_name, tool_input),
                extra={
                    "tool_name": tool_name,
                    "exception_type": type(e).__name__,
                    "message": str(e)[:1000],
                },
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

        emit_tool_invocation(
            tool_name,
            (time.perf_counter() - started_at) * 1000,
            True,
            input_size,
        )
        return ToolResult(tool_use_id=tool_use_id, content=content, is_error=False)


_BASH_EXIT_RE = re.compile(r"(?m)^EXIT_CODE: (-?\d+)$")
_BASH_TIMEOUT_PREFIX = "❌ command timed out after "
_BASH_TRUNCATED_MARKERS = (
    "(... stdout truncated to last 30KB ...)",
    "(... stderr truncated to last 10KB ...)",
)
_GLOB_MAGIC_RE = re.compile(r"[*?\[]")


def _error_result(
    tool_use_id: str, error: ToolError, extra: dict[str, Any] | None = None
) -> ToolResult:
    payload = asdict(error)
    if extra:
        payload.update(extra)
    content = json.dumps(payload, ensure_ascii=False)
    return ToolResult(tool_use_id=tool_use_id, content=content, is_error=True)


def _tool_error_from_exception(
    exc: Exception, tool_name: str, tool_input: dict[str, Any]
) -> ToolError:
    redirect = _suggest_redirect(tool_name, tool_input)
    return ToolError(
        error="tool_raised",
        error_type=type(exc).__name__,
        retryable=isinstance(exc, (TimeoutError, asyncio.TimeoutError)),
        hint=str(exc)[:1000],
        suggested_tool=redirect[0] if redirect else None,
        suggested_args=redirect[1] if redirect else None,
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


def _suggest_redirect(
    tool_name: str, tool_input: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    if tool_name == "Bash":
        return _suggest_bash_redirect(tool_input.get("command"))
    if tool_name == "Read":
        path = tool_input.get("file_path")
        if isinstance(path, str) and _GLOB_MAGIC_RE.search(path):
            return "Glob", _glob_args_from_path(path)
    return None


def _glob_args_from_path(path: str) -> dict[str, Any]:
    parts = path.split("/")
    pattern_start = next(
        (idx for idx, part in enumerate(parts) if _GLOB_MAGIC_RE.search(part)),
        0,
    )
    prefix = "/".join(parts[:pattern_start])
    pattern = "/".join(parts[pattern_start:]) or "*"
    if prefix:
        return {"path": prefix, "pattern": pattern}
    return {"pattern": pattern}


def _suggest_bash_redirect(command: Any) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(command, str):
        return None
    try:
        argv = shlex.split(command.strip())
    except ValueError:
        return None
    if not argv:
        return None
    program = argv[0]
    if program == "cat":
        path = _first_non_option(argv[1:])
        if path:
            return "Read", {"file_path": path}
    if program == "grep":
        args = _translate_grep_args(argv[1:])
        if args is not None:
            return "Grep", args
    if program == "find":
        args = _translate_find_args(argv[1:])
        if args is not None:
            return "Glob", args
    return None


def _first_non_option(args: list[str]) -> str | None:
    for arg in args:
        if arg == "--":
            continue
        if not arg.startswith("-"):
            return arg
    return None


def _translate_grep_args(args: list[str]) -> dict[str, Any] | None:
    out: dict[str, Any] = {"output_mode": "files_with_matches"}
    positionals: list[str] = []
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg in {"-r", "-R", "--recursive"}:
            idx += 1
            continue
        if (
            arg.startswith("-")
            and not arg.startswith("--")
            and arg not in {"-i", "-n", "-l", "-c", "-e"}
        ):
            if "i" in arg:
                out["-i"] = True
            if "n" in arg:
                out["-n"] = True
            if "l" in arg:
                out["output_mode"] = "files_with_matches"
            if "c" in arg:
                out["output_mode"] = "count"
            idx += 1
            continue
        if arg == "-i":
            out["-i"] = True
            idx += 1
            continue
        if arg == "-n":
            out["-n"] = True
            idx += 1
            continue
        if arg == "-l":
            out["output_mode"] = "files_with_matches"
            idx += 1
            continue
        if arg == "-c":
            out["output_mode"] = "count"
            idx += 1
            continue
        if arg == "-e" and idx + 1 < len(args):
            positionals.append(args[idx + 1])
            idx += 2
            continue
        if arg.startswith("--include="):
            out["glob"] = arg.split("=", 1)[1]
            idx += 1
            continue
        if arg == "--":
            positionals.extend(args[idx + 1 :])
            break
        if arg.startswith("-"):
            idx += 1
            continue
        positionals.append(arg)
        idx += 1
    if not positionals:
        return None
    out["pattern"] = positionals[0]
    if len(positionals) > 1:
        out["path"] = positionals[1]
    return out


def _translate_find_args(args: list[str]) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    if args and not args[0].startswith("-"):
        out["path"] = args[0]
    if "-name" in args:
        idx = args.index("-name")
        if idx + 1 < len(args):
            out["pattern"] = args[idx + 1]
    if "pattern" not in out:
        out["pattern"] = "*"
    return out


def get_tool_summary(name: str) -> str:
    """Return the one-line catalog summary for ``name``.

    Common eager tools use short operator-facing labels so the boot prompt
    stays compact. Less common tools fall back to the first sentence from
    the schema registry, normalised onto one line.
    """
    if name in _TOOL_SUMMARY_OVERRIDES:
        return _TOOL_SUMMARY_OVERRIDES[name]

    schema = get_schema(name)
    first_sentence = schema.description.strip().split(".", 1)[0]
    return " ".join(first_sentence.split())


_default_dispatcher = ToolDispatcher()


def _slack_post_message_handler(input: dict[str, Any]) -> dict[str, Any]:
    from backend.agents.tools.slack_tool import slack_post_message

    return slack_post_message(input["channel"], input["text"])


_default_dispatcher.register("SlackPostMessage", _slack_post_message_handler)


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
