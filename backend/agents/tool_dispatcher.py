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
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Empty, Queue
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


class StructuredToolError(Exception):
    """Raised by handlers to surface a typed error code in the tool_result.

    The dispatcher's ``_tool_error_from_exception`` recognises this class and
    forwards ``error_code`` verbatim into the ``ToolError.error`` envelope,
    so handlers like ``text_editor`` / ``ptc_sandbox`` can emit AC-defined
    codes (``text_editor_no_match``, ``sandbox_boundary_violation``, etc.)
    without redefining the result-block plumbing.
    """

    def __init__(self, error_code: str, hint: str = "") -> None:
        super().__init__(hint or error_code)
        self.error_code = error_code
        self.hint = hint


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


ProficiencyGate = Callable[[str, str], Awaitable[bool]]
"""Async (tool_name, agent_id) -> True/False gate hook (W13)."""


class ToolDispatcher:
    """Registers and executes tool handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._proficiency_gate: ProficiencyGate | None = None
        self._current_agent_id: str | None = None

    def set_proficiency_gate(
        self, gate: ProficiencyGate | None, *, agent_id: str | None = None
    ) -> None:
        """Install a W13 proficiency gate that runs before each handler.

        ``gate(tool_name, agent_id)`` returns True to allow the call and
        False to refuse it; on refusal the dispatcher returns a
        ``tool_proficiency_insufficient`` error result (the calling LLM
        can self-correct just like any other handler error). When
        ``agent_id`` is ``None`` the gate is bypassed entirely — this is
        the default for backwards compatibility with callers that have
        not yet wired W13.

        Production callers should not hand-roll the closure. The W13.3
        (OP-180) feature-unlock gate is built and installed by
        :func:`backend.agents.tool_proficiency.install_feature_unlock_gate`,
        which reads ``config/tool_proficiency_gates.yaml`` for the
        per-tool required Lv and consults
        :func:`backend.agents.tool_proficiency.can_invoke_at_level`
        against the per-agent row — re-installing with a different
        ``agent_id`` is the supported way to re-scope an already-running
        dispatcher to a different agent.
        """
        self._proficiency_gate = gate
        self._current_agent_id = agent_id

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
                agent_id=self._current_agent_id,
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

        if (
            self._proficiency_gate is not None
            and self._current_agent_id is not None
        ):
            try:
                allowed = await self._proficiency_gate(
                    tool_name, self._current_agent_id
                )
            except Exception as gate_exc:  # noqa: BLE001 — gate must not crash dispatch
                logger.exception("Proficiency gate raised on %s", tool_name)
                allowed = True  # fail-open per W13 §"Error catalog"
                _ = gate_exc
            if not allowed:
                emit_tool_invocation(
                    tool_name,
                    (time.perf_counter() - started_at) * 1000,
                    False,
                    input_size,
                    agent_id=self._current_agent_id,
                )
                return _error_result(
                    tool_use_id=tool_use_id,
                    error=ToolError(
                        error="tool_proficiency_insufficient",
                        error_type="ToolProficiencyInsufficient",
                        retryable=False,
                        hint=(
                            f"agent {self._current_agent_id!r} does not meet "
                            f"the required proficiency level for {tool_name!r}"
                        ),
                    ),
                    extra={
                        "tool_name": tool_name,
                        "agent_id": self._current_agent_id,
                    },
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
                agent_id=self._current_agent_id,
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
            agent_id=self._current_agent_id,
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
    if isinstance(exc, StructuredToolError):
        return ToolError(
            error=exc.error_code,
            error_type=exc.error_code,
            retryable=False,
            hint=exc.hint[:1000],
        )
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


# ─── Anthropic built-in tools (OP-828 / B1) ─────────────────────────
#
# Anthropic ships three server-orchestrated tool surfaces that this runner
# now leans on instead of OmniSight-defined Read/Write/Edit/Bash:
#
#   text_editor_20250728  →  ``str_replace_based_edit_tool``
#   bash_20250124         →  ``bash``
#   code_execution_20260120 → ``code_execution`` (PTC sandbox)
#
# The classes below give the dispatcher concrete handlers so
# ``test_built_in_tools_dispatch`` and ``test_ptc_sandbox_isolation`` can
# exercise the contract without round-tripping through the live API. For
# real runs the model still talks to Anthropic's hosted text_editor / bash
# / sandbox; these handlers are the local fall-through that keeps the
# launcher self-contained and unit-testable.


WORKTREE_ENV_VAR = "OMNISIGHT_WORKTREE_PATH"


def _resolve_worktree_root(explicit: Path | str | None = None) -> Path:
    """Resolve the worktree root used by built-in tool handlers.

    Priority: explicit arg > ``OMNISIGHT_WORKTREE_PATH`` env > runner repo
    parent. Caller-side launch gates (``PTCSandbox.launch``) may refuse
    when the env var is unset; this helper purposefully tolerates a
    missing env so unit tests can construct a handler with an explicit
    tmp path without touching process env.
    """
    if explicit is not None:
        return Path(explicit).resolve()
    env_value = os.environ.get(WORKTREE_ENV_VAR)
    if env_value:
        return Path(env_value).resolve()
    return Path(__file__).resolve().parents[2]


class TextEditorHandler:
    """Handler for built-in ``str_replace_based_edit_tool``.

    Implements the five commands listed in OP-828 AC #4: ``view``,
    ``create``, ``str_replace``, ``insert``, ``undo_edit``. Each command
    either returns content or raises :class:`StructuredToolError` with
    one of the AC-listed codes (``tool_input_invalid``,
    ``text_editor_no_match``, ``text_editor_path_outside_worktree``).

    Path safety: every ``path`` argument is resolved with realpath and
    must remain under ``worktree_root``; symlink escapes are rejected.

    Undo: each successful mutating call snapshots the prior file content
    onto a per-path stack so ``undo_edit`` can revert the most recent
    write without git ops.
    """

    _COMMANDS = frozenset(
        {"view", "create", "str_replace", "insert", "undo_edit"}
    )

    def __init__(self, worktree_root: Path | str | None = None) -> None:
        self.worktree_root = _resolve_worktree_root(worktree_root)
        self._undo: dict[str, list[str | None]] = {}

    def __call__(self, payload: dict[str, Any]) -> str:
        command = payload.get("command")
        if command not in self._COMMANDS:
            raise StructuredToolError(
                "tool_input_invalid",
                f"unknown text_editor command {command!r}",
            )
        path = payload.get("path")
        if not isinstance(path, str) or not path:
            raise StructuredToolError(
                "tool_input_invalid", "path is required"
            )
        target = self._resolve(path)
        method = getattr(self, f"_cmd_{command}")
        return method(target, payload)

    # ── path safety ────────────────────────────────────────────────
    def _resolve(self, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.worktree_root / candidate
        # ``resolve`` collapses symlinks too; combined with relative_to it
        # makes ``../`` and symlink escapes equivalently fatal.
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.worktree_root)
        except ValueError as exc:
            raise StructuredToolError(
                "text_editor_path_outside_worktree",
                f"path {resolved} is outside worktree {self.worktree_root}",
            ) from exc
        return resolved

    # ── undo plumbing ──────────────────────────────────────────────
    def _push_undo(self, target: Path) -> None:
        key = str(target)
        snapshot = (
            target.read_text(encoding="utf-8") if target.is_file() else None
        )
        self._undo.setdefault(key, []).append(snapshot)

    # ── commands ───────────────────────────────────────────────────
    def _cmd_view(self, target: Path, payload: dict[str, Any]) -> str:
        if not target.exists():
            raise StructuredToolError(
                "tool_input_invalid", f"path does not exist: {target}"
            )
        if target.is_dir():
            return "\n".join(sorted(p.name for p in target.iterdir()))
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        view_range = payload.get("view_range")
        offset = 0
        if view_range is not None:
            if (
                not isinstance(view_range, (list, tuple))
                or len(view_range) != 2
            ):
                raise StructuredToolError(
                    "tool_input_invalid",
                    "view_range must be [start, end]",
                )
            try:
                start = int(view_range[0])
                end_raw = int(view_range[1])
            except (TypeError, ValueError) as exc:
                raise StructuredToolError(
                    "tool_input_invalid",
                    "view_range entries must be integers",
                ) from exc
            if start < 1:
                raise StructuredToolError(
                    "tool_input_invalid",
                    "view_range start must be >= 1",
                )
            end = len(lines) if end_raw == -1 else min(len(lines), end_raw)
            lines = lines[start - 1 : end]
            offset = start - 1
        return "\n".join(
            f"{i + offset + 1}\t{line}" for i, line in enumerate(lines)
        )

    def _cmd_create(self, target: Path, payload: dict[str, Any]) -> str:
        file_text = payload.get("file_text")
        if not isinstance(file_text, str):
            raise StructuredToolError(
                "tool_input_invalid", "file_text must be a string"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        self._push_undo(target)
        target.write_text(file_text, encoding="utf-8")
        return f"created {target}"

    def _cmd_str_replace(self, target: Path, payload: dict[str, Any]) -> str:
        old_str = payload.get("old_str")
        new_str = payload.get("new_str", "")
        if not isinstance(old_str, str) or not old_str:
            raise StructuredToolError(
                "tool_input_invalid", "old_str is required"
            )
        if not isinstance(new_str, str):
            raise StructuredToolError(
                "tool_input_invalid", "new_str must be a string"
            )
        if not target.is_file():
            raise StructuredToolError(
                "tool_input_invalid", f"path is not a file: {target}"
            )
        text = target.read_text(encoding="utf-8")
        count = text.count(old_str)
        if count == 0:
            raise StructuredToolError(
                "text_editor_no_match",
                f"old_str not found in {target}",
            )
        if count > 1:
            raise StructuredToolError(
                "tool_input_invalid",
                f"old_str matches {count} times; must be unique",
            )
        self._push_undo(target)
        target.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        return f"replaced 1 occurrence in {target}"

    def _cmd_insert(self, target: Path, payload: dict[str, Any]) -> str:
        insert_line = payload.get("insert_line")
        insert_text = payload.get("insert_text", "")
        if not isinstance(insert_line, int) or isinstance(insert_line, bool):
            raise StructuredToolError(
                "tool_input_invalid", "insert_line must be an integer"
            )
        if not isinstance(insert_text, str):
            raise StructuredToolError(
                "tool_input_invalid", "insert_text must be a string"
            )
        if not target.is_file():
            raise StructuredToolError(
                "tool_input_invalid", f"path is not a file: {target}"
            )
        text = target.read_text(encoding="utf-8")
        lines = text.split("\n")
        if insert_line < 0 or insert_line > len(lines):
            raise StructuredToolError(
                "tool_input_invalid",
                f"insert_line {insert_line} out of range [0,{len(lines)}]",
            )
        self._push_undo(target)
        new_block = insert_text.split("\n")
        new_lines = lines[:insert_line] + new_block + lines[insert_line:]
        target.write_text("\n".join(new_lines), encoding="utf-8")
        return f"inserted at line {insert_line} in {target}"

    def _cmd_undo_edit(self, target: Path, payload: dict[str, Any]) -> str:
        del payload  # signature parity
        stack = self._undo.get(str(target))
        if not stack:
            raise StructuredToolError(
                "tool_input_invalid",
                f"no undo history for {target}",
            )
        previous = stack.pop()
        if previous is None:
            # The last edit created the file from scratch — undo means delete.
            if target.is_file():
                target.unlink()
            return f"undone create on {target}"
        target.write_text(previous, encoding="utf-8")
        return f"undone last edit on {target}"


# ── PersistentBashSession + BashHandlerV2 ──────────────────────────


class PersistentBashSession:
    """Long-running ``/bin/bash`` subprocess used by :class:`BashHandlerV2`.

    Persistence: every command is fed onto the same shell stdin, so
    ``export FOO=bar`` / ``cd subdir`` / function definitions remain in
    scope across calls. A unique per-call sentinel marks command end so
    we can demultiplex output streams without polling.

    Threading: a daemon thread drains ``stdout`` into a queue so the
    caller thread can apply timeouts without deadlocking on partial
    reads.
    """

    _SENTINEL_PREFIX = "__OMNISIGHT_BASH_DONE_"

    def __init__(self, cwd: Path | str) -> None:
        self.cwd = Path(cwd).resolve()
        if not self.cwd.is_dir():
            raise StructuredToolError(
                "tool_input_invalid",
                f"bash cwd {self.cwd} is not a directory",
            )
        self._proc: subprocess.Popen[str] | None = None
        self._queue: Queue[str] = Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._open()

    # ── lifecycle ──────────────────────────────────────────────────
    def _open(self) -> None:
        self._proc = subprocess.Popen(
            ["/bin/bash", "--noprofile", "--norc"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(self.cwd),
            text=True,
            bufsize=1,
        )
        self._queue = Queue()
        self._reader = threading.Thread(
            target=self._drain_stdout, daemon=True
        )
        self._reader.start()

    def _drain_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, ""):
            self._queue.put(line)
        self._queue.put("")  # sentinel for EOF

    def restart(self) -> None:
        """Tear down the running shell and start a fresh one (AC #5)."""
        with self._lock:
            self._close_locked()
            self._open()

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        finally:
            self._proc = None

    # ── exec ──────────────────────────────────────────────────────
    def run(self, command: str, timeout: float = 30.0) -> str:
        if not isinstance(command, str) or not command.strip():
            raise StructuredToolError(
                "tool_input_invalid", "bash command must be a non-empty string"
            )
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._open()
                proc = self._proc
            assert proc is not None and proc.stdin is not None  # for mypy
            sentinel = f"{self._SENTINEL_PREFIX}{uuid.uuid4().hex}__"
            framed = (
                f"{command}\n"
                f"printf '\\n%s:%s\\n' '{sentinel}' \"$?\"\n"
            )
            try:
                proc.stdin.write(framed)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise StructuredToolError(
                    "bash_timeout",
                    f"shell pipe broken: {exc}",
                ) from exc
            return self._read_until_sentinel(sentinel, timeout)

    def _read_until_sentinel(self, sentinel: str, timeout: float) -> str:
        collected: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Timed out — kill the shell so the next call gets a clean
                # state (the in-flight command may still hold stdin).
                self._close_locked()
                self._open()
                raise StructuredToolError(
                    "bash_timeout",
                    f"command timed out after {timeout:.1f}s",
                )
            try:
                line = self._queue.get(timeout=remaining)
            except Empty:
                continue
            if line == "":
                # EOF — shell died unexpectedly.
                raise StructuredToolError(
                    "bash_timeout",
                    "shell exited before sentinel",
                )
            idx = line.find(sentinel + ":")
            if idx == -1:
                collected.append(line)
                continue
            if idx > 0:
                collected.append(line[:idx])
            tail = line[idx + len(sentinel) + 1 :].strip()
            try:
                exit_code = int(tail)
            except ValueError:
                exit_code = -1
            stdout = "".join(collected)
            return f"{stdout}EXIT_CODE: {exit_code}"


class BashHandlerV2:
    """Handler for built-in ``bash`` (bash_20250124) tool name.

    Maintains a persistent shell session keyed on ``(self,)`` so callers
    that re-use the same handler instance see ``export``/``cd``/function
    definitions persist across calls (AC #5).

    Schema (mirrors Anthropic's bash_20250124):
        - ``command`` (str): shell command to run.
        - ``restart`` (bool): if true, recycle the underlying shell.
    """

    def __init__(self, cwd: Path | str | None = None) -> None:
        self._cwd = _resolve_worktree_root(cwd)
        self._session: PersistentBashSession | None = None

    @property
    def session(self) -> PersistentBashSession:
        if self._session is None:
            self._session = PersistentBashSession(cwd=self._cwd)
        return self._session

    def __call__(self, payload: dict[str, Any]) -> str:
        if payload.get("restart"):
            if self._session is not None:
                self._session.restart()
            return "shell restarted"
        command = payload.get("command")
        if command is None:
            raise StructuredToolError(
                "tool_input_invalid",
                "either 'command' or 'restart' is required",
            )
        timeout = float(payload.get("timeout") or 30.0)
        return self.session.run(command, timeout=timeout)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None


# ── PTCSandbox + ptc_sandbox_handler ───────────────────────────────


_PTC_DEFAULT_ALLOWED_CALLERS: tuple[str, ...] = (
    "text_editor_20250728",
    "bash_20250124",
)


class PTCSandbox:
    """Local emulation of Anthropic's code_execution_20260120 sandbox.

    The hosted sandbox runs server-side; this class is the runner-side
    boundary validator that test_ptc_sandbox_isolation exercises. It
    enforces the OP-828 invariants:

    * AC #2 — sandbox launch refuses if ``OMNISIGHT_WORKTREE_PATH`` is
      not set in the environment (worktree path is the only safe ``cwd``
      the launcher can hand to a remote sandbox).
    * AC #3 — only callers in ``allowed_callers`` may invoke the sandbox;
      any HTTP / DB attempt raises ``sandbox_boundary_violation``.

    Writes inside the worktree succeed via ``write_file``; writes outside
    are rejected with the same ``sandbox_boundary_violation`` code so the
    boundary surface stays uniform.
    """

    def __init__(
        self,
        cwd: Path,
        allowed_callers: tuple[str, ...] | list[str],
    ) -> None:
        self.cwd = Path(cwd).resolve()
        self.allowed_callers = tuple(allowed_callers)

    @classmethod
    def launch(
        cls,
        *,
        allowed_callers: tuple[str, ...] | list[str] = _PTC_DEFAULT_ALLOWED_CALLERS,
    ) -> PTCSandbox:
        env_value = os.environ.get(WORKTREE_ENV_VAR)
        if not env_value:
            raise StructuredToolError(
                "ptc_creds_missing",
                f"{WORKTREE_ENV_VAR} is not set; sandbox launch refused",
            )
        worktree = Path(env_value).resolve()
        if not worktree.is_dir():
            raise StructuredToolError(
                "ptc_creds_missing",
                f"{WORKTREE_ENV_VAR}={worktree} is not a directory",
            )
        return cls(cwd=worktree, allowed_callers=tuple(allowed_callers))

    # ── enforcement points ────────────────────────────────────────
    def _ensure_caller_allowed(self, caller: str) -> None:
        if caller not in self.allowed_callers:
            raise StructuredToolError(
                "sandbox_boundary_violation",
                f"caller {caller!r} not in allowed_callers {self.allowed_callers}",
            )

    def _ensure_inside_cwd(self, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.cwd / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.cwd)
        except ValueError as exc:
            raise StructuredToolError(
                "sandbox_boundary_violation",
                f"path {resolved} is outside sandbox cwd {self.cwd}",
            ) from exc
        return resolved

    # ── public surface ────────────────────────────────────────────
    def write_file(
        self,
        path: str,
        content: str,
        *,
        caller: str = "text_editor_20250728",
    ) -> Path:
        self._ensure_caller_allowed(caller)
        target = self._ensure_inside_cwd(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def http_request(self, *_args: Any, **_kwargs: Any) -> None:
        raise StructuredToolError(
            "sandbox_boundary_violation",
            "HTTP egress is not permitted inside the PTC sandbox",
        )

    def db_query(self, *_args: Any, **_kwargs: Any) -> None:
        raise StructuredToolError(
            "sandbox_boundary_violation",
            "Database access is not permitted inside the PTC sandbox",
        )

    def call(
        self, caller: str, action: str, **kwargs: Any
    ) -> Any:
        """Single dispatch helper used by ptc_sandbox_handler."""
        self._ensure_caller_allowed(caller)
        if action == "write":
            return str(
                self.write_file(
                    kwargs["path"], kwargs.get("content", ""), caller=caller
                )
            )
        if action in {"http", "fetch", "request"}:
            self.http_request(**kwargs)
        if action in {"db", "sql", "query"}:
            self.db_query(**kwargs)
        raise StructuredToolError(
            "tool_input_invalid",
            f"unknown sandbox action {action!r}",
        )


def ptc_sandbox_handler(payload: dict[str, Any]) -> str:
    """Dispatcher entry-point for ``code_execution`` tool calls.

    The model rarely invokes this locally — Anthropic runs the real
    sandbox server-side — but the dispatcher needs a handler so the unit
    tests (``test_ptc_sandbox_isolation``) can exercise the boundary
    contract end-to-end. Every call goes through ``PTCSandbox.launch``
    which honours the env-var precondition.
    """
    sandbox = PTCSandbox.launch(
        allowed_callers=tuple(
            payload.get("allowed_callers") or _PTC_DEFAULT_ALLOWED_CALLERS
        ),
    )
    caller = payload.get("caller", "text_editor_20250728")
    action = payload.get("action", "write")
    kwargs = {
        k: v
        for k, v in payload.items()
        if k not in {"caller", "action", "allowed_callers"}
    }
    result = sandbox.call(caller, action, **kwargs)
    return result if isinstance(result, str) else json.dumps(result)


def bind_built_in_tools(
    dispatcher: ToolDispatcher,
    *,
    worktree_root: Path | str | None = None,
) -> tuple[TextEditorHandler, BashHandlerV2]:
    """Register the three OP-828 built-in tool handlers on ``dispatcher``.

    Returns the text-editor and bash handler instances so the caller can
    inspect undo state or close the bash session at shutdown.
    """
    text_editor = TextEditorHandler(worktree_root=worktree_root)
    bash_v2 = BashHandlerV2(cwd=worktree_root)
    dispatcher.register("str_replace_based_edit_tool", text_editor)
    dispatcher.register("bash", bash_v2)
    dispatcher.register("code_execution", ptc_sandbox_handler)
    return text_editor, bash_v2


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
