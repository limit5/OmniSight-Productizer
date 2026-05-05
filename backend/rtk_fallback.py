"""BP.R.4 — RTK raw-log fallback gate for repeated compile failures.

RTK compression is valuable for noisy build logs, but it can hide
low-level compiler context after the agent has already failed to fix
the same build twice.  This module keeps the fallback decision pure:
given the current task id, failed tool output, and run-scoped history,
it decides whether the next retry should bypass compression and asks
the agent to re-run the compile command via ``rtk --no-rtk``.

Module-global state audit (SOP Step 1): this module stores no mutable
module-level state.  Regexes and string constants are immutable; the
failure history is carried on ``GraphState`` per graph run, so separate
uvicorn workers derive the same decision from the same state snapshot.

Read-after-write timing audit: N/A.  The fallback gate is a pure
projection from in-memory LangGraph state and does not touch PG, Redis,
or any timing-visible downstream store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence


__all__ = [
    "MAX_RTK_FALLBACK_HISTORY",
    "RTK_FALLBACK_HISTORY_PREFIX",
    "RTK_FALLBACK_THRESHOLD",
    "RTK_NO_RTK_FLAG",
    "RtkFallbackDecision",
    "build_no_rtk_command",
    "compile_failure_signature",
    "is_compile_failure",
    "update_rtk_fallback_history",
]


RTK_FALLBACK_THRESHOLD: int = 2
RTK_NO_RTK_FLAG: str = "--no-rtk"
RTK_FALLBACK_HISTORY_PREFIX: str = "rtk_compile"
MAX_RTK_FALLBACK_HISTORY: int = 20

_NON_COMPILE_MARKER = f"{RTK_FALLBACK_HISTORY_PREFIX}:_non_compile"

_COMPILE_COMMAND_RE = re.compile(
    r"^\s*(?:rtk\s+(?:--no-rtk\s+)?)?"
    r"(?:"
    r"make|cmake|ninja|gcc|g\+\+|clang|clang\+\+|"
    r"cargo\s+(?:build|test|check)|"
    r"go\s+(?:build|test)|"
    r"npm\s+run\s+build|pnpm\s+(?:run\s+)?build|yarn\s+build"
    r")\b",
    re.IGNORECASE,
)

_COMPILE_OUTPUT_RE = re.compile(
    r"(?:"
    r"\berror:|"
    r"\bfatal error:|"
    r"undefined reference|"
    r"ld returned \d+ exit status|"
    r"compilation terminated|"
    r"failed to compile|"
    r"compile failed|"
    r"ninja: build stopped|"
    r"make(?:\[\d+\])?: \*\*\*"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RtkFallbackDecision:
    """Decision payload returned when BP.R.4 fallback should activate."""

    signature: str
    count: int
    threshold: int
    raw_command: str
    message: str


def is_compile_failure(*, tool_name: str, output: str, command: str = "") -> bool:
    """Return True when a failed tool result looks like a compile failure."""

    name = (tool_name or "").strip()
    if name not in {"run_bash", "Bash"}:
        return False
    text = output or ""
    if command and _COMPILE_COMMAND_RE.search(command):
        return True
    return bool(_COMPILE_OUTPUT_RE.search(text))


def compile_failure_signature(
    *, task_id: str | None, tool_name: str, output: str, command: str = ""
) -> str:
    """Build a stable same-task signature for a compiler failure."""

    task_key = _normalise_token(task_id or "_no_task")
    command_key = _normalise_token(_command_bucket(command) or tool_name or "run_bash")
    output_key = _normalise_token(_first_compile_line(output) or output or "_empty")
    return (
        f"{RTK_FALLBACK_HISTORY_PREFIX}:{task_key}:"
        f"{command_key}:{output_key[:160]}"
    )


def build_no_rtk_command(command: str) -> str:
    """Return a shell command that asks RTK for raw, uncompressed output."""

    stripped = (command or "").strip()
    if not stripped:
        return ""
    if RTK_NO_RTK_FLAG in stripped.split():
        return stripped
    if stripped.startswith("rtk "):
        return "rtk --no-rtk " + stripped[len("rtk "):].strip()
    return f"rtk --no-rtk {stripped}"


def update_rtk_fallback_history(
    *,
    task_id: str | None,
    failed_tool_name: str,
    failed_output: str,
    prior_history: Sequence[str],
    command: str = "",
    threshold: int = RTK_FALLBACK_THRESHOLD,
) -> tuple[list[str], RtkFallbackDecision | None]:
    """Append this failure and decide whether RTK fallback should fire."""

    if threshold < 1:
        raise ValueError("threshold must be >= 1")

    history = list(prior_history or [])
    if not is_compile_failure(
        tool_name=failed_tool_name,
        output=failed_output,
        command=command,
    ):
        return _bounded([*history, _NON_COMPILE_MARKER]), None

    signature = compile_failure_signature(
        task_id=task_id,
        tool_name=failed_tool_name,
        output=failed_output,
        command=command,
    )
    updated = _bounded([*history, signature])
    count = _count_trailing(updated, signature)
    if count < threshold:
        return updated, None

    raw_command = build_no_rtk_command(command)
    command_hint = f" `{raw_command}`" if raw_command else " the last compile command with `--no-rtk`"
    message = (
        "RTK fallback active: same compile failure repeated "
        f"{count} times for task {task_id or '_no_task'}; "
        f"re-fetch raw build output with{command_hint}."
    )
    return updated, RtkFallbackDecision(
        signature=signature,
        count=count,
        threshold=threshold,
        raw_command=raw_command,
        message=message,
    )


def _bounded(history: list[str]) -> list[str]:
    return history[-MAX_RTK_FALLBACK_HISTORY:]


def _count_trailing(history: Sequence[str], signature: str) -> int:
    count = 0
    for item in reversed(history):
        if item != signature:
            break
        count += 1
    return count


def _command_bucket(command: str) -> str:
    stripped = (command or "").strip()
    if not stripped:
        return ""
    match = _COMPILE_COMMAND_RE.search(stripped)
    if not match:
        return stripped.split()[0]
    bucket = match.group(0)
    if bucket.lower().startswith("rtk "):
        bucket = bucket[4:]
    if bucket.lower().startswith("--no-rtk "):
        bucket = bucket[len("--no-rtk "):]
    return bucket


def _first_compile_line(output: str) -> str:
    for line in (output or "").splitlines():
        if _COMPILE_OUTPUT_RE.search(line):
            return line
    for line in (output or "").splitlines():
        if line.strip():
            return line
    return ""


def _normalise_token(value: str) -> str:
    text = re.sub(r"\s+", " ", value.strip().lower())
    text = re.sub(r"(?<!\S)(?:/|[a-z]:\\)[\w._/\-\\]+(?::\d+)*", "<path>", text)
    text = re.sub(r":\d+(?::\d+)?", ":<line>", text)
    return text[:200] or "_empty"
