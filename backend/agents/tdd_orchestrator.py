"""OP-834 TDD phase guard for runner tool handlers."""
from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from backend.agents.tool_dispatcher import StructuredToolError, ToolDispatcher


ERROR_PREMATURE_GREEN = "tdd_violated_premature_green"
ERROR_UNRUNNABLE = "tdd_violated_unrunnable"
ERROR_UNWRITEABLE = "tdd_unwriteable"
ERROR_CONDITIONAL_INDETERMINATE = "tdd_conditional_indeterminate"
ERROR_ORDERING = "tdd_violated_ordering"

MUTATING_TEXT_COMMANDS = frozenset({"create", "str_replace", "insert"})
TEST_PATH_RE = re.compile(r"(^|/)(tests?|test_[^/]*|[^/]*_test)\b|(^|/)test_.*\.py$")


@dataclass
class TDDRunState:
    ticket_key: str
    applicable: str
    bypassed: bool = False
    bypass_reason: str = ""
    test_written: bool = False
    red_confirmed: bool = False
    source_edited: bool = False
    green_confirmed: bool = False
    test_write_attempts: int = 0
    last_error: str = ""
    events: list[str] = field(default_factory=list)

    @property
    def coder_unlocked(self) -> bool:
        return self.bypassed or self.red_confirmed


class TDDOrchestrator:
    """Small state machine that enforces red-before-code ordering."""

    def __init__(self, *, ticket_key: str, applicable: str) -> None:
        self.state = TDDRunState(ticket_key=ticket_key, applicable=applicable)
        if applicable == "no":
            self.bypass("tdd_applicable=no")

    def bypass(self, reason: str) -> None:
        self.state.bypassed = True
        self.state.bypass_reason = reason
        self.state.events.append(f"bypass:{reason}")

    def record_locator_decision(self, decision: str) -> None:
        if self.state.applicable != "conditional":
            return
        if decision == "bypass":
            self.bypass("locator:no-testable-behavior")
        elif decision == "active":
            self.state.events.append("locator:active")
        else:
            self.bypass(ERROR_CONDITIONAL_INDETERMINATE)
            self.state.last_error = ERROR_CONDITIONAL_INDETERMINATE

    def before_text_edit(self, payload: dict[str, Any]) -> None:
        if self.state.bypassed:
            return
        command = payload.get("command")
        if command not in MUTATING_TEXT_COMMANDS:
            return
        path = str(payload.get("path") or "")
        if _is_test_path(path):
            self.state.test_write_attempts += 1
            if self.state.test_write_attempts > 3 and not self.state.red_confirmed:
                self.state.last_error = ERROR_UNWRITEABLE
                raise StructuredToolError(
                    ERROR_UNWRITEABLE,
                    "test write failed to produce a runnable red test in 3 attempts",
                )
            return
        if not self.state.red_confirmed:
            self.state.last_error = ERROR_ORDERING
            raise StructuredToolError(
                ERROR_ORDERING,
                "source edits are blocked until a test exists and runs red",
            )

    def after_text_edit(self, payload: dict[str, Any]) -> None:
        if self.state.bypassed:
            return
        command = payload.get("command")
        if command not in MUTATING_TEXT_COMMANDS:
            return
        path = str(payload.get("path") or "")
        if _is_test_path(path):
            self.state.test_written = True
            self.state.events.append("write_test")
        else:
            self.state.source_edited = True
            self.state.events.append("write_code")

    def after_bash(self, payload: dict[str, Any], output: str) -> None:
        if self.state.bypassed:
            return
        command = str(payload.get("command") or "")
        if not _looks_like_test_command(command):
            return
        exit_code = _exit_code(output)
        if exit_code is None:
            return
        if not self.state.source_edited:
            self._classify_pre_code_test_run(exit_code, output)
            return
        if exit_code == 0:
            self.state.green_confirmed = True
            self.state.events.append("run_test_green")
        else:
            self.state.events.append("run_test_still_red")

    def _classify_pre_code_test_run(self, exit_code: int, output: str) -> None:
        if exit_code == 0:
            self.state.last_error = ERROR_PREMATURE_GREEN
            raise StructuredToolError(
                ERROR_PREMATURE_GREEN,
                "test passed before any source edit; revise the test",
            )
        if _looks_unrunnable(output):
            self.state.last_error = ERROR_UNRUNNABLE
            raise StructuredToolError(
                ERROR_UNRUNNABLE,
                "test command failed before exercising behavior",
            )
        self.state.red_confirmed = True
        self.state.events.append("test_runs_red")


class TDDTextEditorGuard:
    def __init__(self, inner: Callable[[dict[str, Any]], Any], orchestrator: TDDOrchestrator) -> None:
        self.inner = inner
        self.orchestrator = orchestrator

    def __call__(self, payload: dict[str, Any]) -> Any:
        self.orchestrator.before_text_edit(payload)
        try:
            result = self.inner(payload)
        except Exception:
            if _is_test_path(str(payload.get("path") or "")):
                self.orchestrator.state.events.append("test_write_failed")
            raise
        self.orchestrator.after_text_edit(payload)
        return result


class TDDBashGuard:
    def __init__(self, inner: Callable[[dict[str, Any]], Any], orchestrator: TDDOrchestrator) -> None:
        self.inner = inner
        self.orchestrator = orchestrator

    def __call__(self, payload: dict[str, Any]) -> Any:
        result = self.inner(payload)
        if isinstance(result, str):
            self.orchestrator.after_bash(payload, result)
        return result


@contextmanager
def installed_tdd_guard(
    dispatcher: ToolDispatcher | Any | None,
    orchestrator: TDDOrchestrator,
) -> Iterator[TDDOrchestrator]:
    """Temporarily wrap built-in editor/bash handlers for one ticket."""
    handlers = getattr(dispatcher, "_handlers", {})
    text_editor = handlers.get("str_replace_based_edit_tool")
    bash = handlers.get("bash")
    if text_editor is None or bash is None or orchestrator.state.bypassed:
        yield orchestrator
        return
    handlers["str_replace_based_edit_tool"] = TDDTextEditorGuard(text_editor, orchestrator)
    handlers["bash"] = TDDBashGuard(bash, orchestrator)
    try:
        yield orchestrator
    finally:
        handlers["str_replace_based_edit_tool"] = text_editor
        handlers["bash"] = bash


def _is_test_path(path: str) -> bool:
    normalised = Path(path).as_posix()
    return bool(TEST_PATH_RE.search(normalised))


def _looks_like_test_command(command: str) -> bool:
    return "pytest" in command or "unittest" in command or " npm test" in f" {command}"


def _exit_code(output: str) -> int | None:
    match = re.search(r"(?m)^EXIT_CODE: (-?\d+)$", output)
    return int(match.group(1)) if match else None


def _looks_unrunnable(output: str) -> bool:
    markers = (
        "SyntaxError",
        "ImportError",
        "ModuleNotFoundError",
        "fixture ",
        "fixture '",
        "ERROR collecting",
        "collected 0 items",
    )
    return any(marker in output for marker in markers)
