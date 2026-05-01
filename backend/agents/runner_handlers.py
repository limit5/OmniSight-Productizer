"""Tool handlers for `auto-runner-sdk.py` agentic loop.

Implements Read / Write / Edit / Bash / Grep / Glob with safety guards:

  * Path operations (Read / Write / Edit / Glob) reject paths outside
    `BASE_DIR` (resolved with realpath, so symlink escapes are blocked).
  * Bash forces ``cwd=BASE_DIR`` and honours the schema's ``timeout`` (ms).
  * Edit refuses non-unique ``old_string`` unless ``replace_all=True``.

Register on a dispatcher via :func:`bind_to_dispatcher`. Tests build a
fresh ``ToolDispatcher`` so registration does not leak across tests.

Used by ``auto-runner-sdk.py`` to back ``AnthropicClient.run_with_tools``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from backend.agents.tool_dispatcher import ToolDispatcher


# Project root: env override > ascend from this file (backend/agents/ -> root).
_THIS = Path(__file__).resolve()
_DEFAULT_BASE = _THIS.parents[2]
BASE_DIR: Path = Path(
    os.environ.get("OMNISIGHT_RUNNER_BASE_DIR") or _DEFAULT_BASE
).resolve()


def _ensure_inside_base(path: str | Path) -> Path:
    """Resolve to absolute and verify it's under :data:`BASE_DIR`.

    Uses ``realpath`` semantics — symlinks pointing outside BASE_DIR are
    rejected. Non-existent paths are still resolved (Write needs this).
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = BASE_DIR / p
    p = p.resolve()
    try:
        p.relative_to(BASE_DIR)
    except ValueError as e:
        raise PermissionError(
            f"path {p} is outside BASE_DIR {BASE_DIR}"
        ) from e
    return p


# ─── Read ────────────────────────────────────────────────────────


def read_handler(payload: dict[str, Any]) -> str:
    p = _ensure_inside_base(payload["file_path"])
    if not p.exists():
        raise FileNotFoundError(f"{p} does not exist")
    if not p.is_file():
        raise IsADirectoryError(f"{p} is not a regular file")
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    offset = max(int(payload.get("offset", 1)) - 1, 0)
    limit = int(payload.get("limit", 2000))
    sliced = lines[offset : offset + limit]
    # cat -n style numbering — same shape AnthropicClient already documents
    return "\n".join(f"{i + 1 + offset}\t{ln}" for i, ln in enumerate(sliced))


# ─── Write ───────────────────────────────────────────────────────


def write_handler(payload: dict[str, Any]) -> str:
    p = _ensure_inside_base(payload["file_path"])
    content = payload["content"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {p}"


# ─── Edit ────────────────────────────────────────────────────────


def edit_handler(payload: dict[str, Any]) -> str:
    p = _ensure_inside_base(payload["file_path"])
    if not p.exists():
        raise FileNotFoundError(f"{p} does not exist")
    old = payload["old_string"]
    new = payload["new_string"]
    if old == new:
        raise ValueError("old_string and new_string are identical")
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise ValueError("old_string not found in file")
    if payload.get("replace_all"):
        new_text = text.replace(old, new)
    else:
        if count > 1:
            raise ValueError(
                f"old_string is not unique (found {count} matches); "
                "pass replace_all=true or extend old_string with more context"
            )
        new_text = text.replace(old, new, 1)
    p.write_text(new_text, encoding="utf-8")
    return f"Replaced {count} occurrence(s) in {p}"


# ─── Bash ────────────────────────────────────────────────────────


_BASH_DEFAULT_TIMEOUT_MS = 30_000
_BASH_MAX_STDOUT = 30_000
_BASH_MAX_STDERR = 10_000


def bash_handler(payload: dict[str, Any]) -> str:
    cmd = payload["command"]
    if payload.get("run_in_background"):
        # The runner has no Monitor channel — backgrounding would orphan
        # processes when the runner exits. Refuse explicitly.
        raise NotImplementedError(
            "run_in_background is not supported in the runner; "
            "use a foreground command with `timeout` instead"
        )
    timeout_ms = int(payload.get("timeout") or _BASH_DEFAULT_TIMEOUT_MS)
    timeout_s = max(1, timeout_ms // 1000)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        return f"❌ command timed out after {timeout_s}s"
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if len(stdout) > _BASH_MAX_STDOUT:
        stdout = stdout[-_BASH_MAX_STDOUT:]
        stdout = "(... stdout truncated to last 30KB ...)\n" + stdout
    if len(stderr) > _BASH_MAX_STDERR:
        stderr = stderr[-_BASH_MAX_STDERR:]
        stderr = "(... stderr truncated to last 10KB ...)\n" + stderr
    return (
        f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}\nEXIT_CODE: {result.returncode}"
    )


# ─── Grep ────────────────────────────────────────────────────────


def _rg_available() -> bool:
    """True if a real ripgrep binary is on PATH (not a shell alias)."""
    return shutil.which("rg") is not None


def _build_grep_cmd(
    payload: dict[str, Any], path: Path, *, prefer_rg: bool
) -> list[str]:
    pattern = payload["pattern"]
    output_mode = payload.get("output_mode", "files_with_matches")
    if prefer_rg:
        cmd: list[str] = ["rg"]
        if payload.get("-i"):
            cmd.append("-i")
        if payload.get("-n"):
            cmd.append("-n")
        if payload.get("glob"):
            cmd.extend(["--glob", payload["glob"]])
        if payload.get("type"):
            cmd.extend(["-t", payload["type"]])
        if output_mode == "files_with_matches":
            cmd.append("-l")
        elif output_mode == "count":
            cmd.append("-c")
        cmd.extend(["--", pattern, str(path)])
        return cmd
    # POSIX grep fallback. Same surface, tighter feature set.
    cmd = ["grep", "-rE"]
    if payload.get("-i"):
        cmd.append("-i")
    if payload.get("-n"):
        cmd.append("-n")
    if output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")
    if payload.get("glob"):
        cmd.extend(["--include", payload["glob"]])
    if payload.get("type"):
        cmd.extend(["--include", f"*.{payload['type']}"])
    cmd.extend(["--", pattern, str(path)])
    return cmd


def grep_handler(payload: dict[str, Any]) -> str:
    path = (
        _ensure_inside_base(payload["path"]) if payload.get("path") else BASE_DIR
    )
    cmd = _build_grep_cmd(payload, path, prefer_rg=_rg_available())
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        return "❌ grep timed out after 60s"
    # rg/grep exit codes: 0 = found, 1 = no match (not an error), 2+ = real error
    if result.returncode >= 2:
        return f"❌ {cmd[0]} exit {result.returncode}\n{result.stderr[:1000]}"
    out = result.stdout or ""
    if len(out) > 50_000:
        out = "(... output truncated to last 50KB ...)\n" + out[-50_000:]
    return out


# ─── Glob ────────────────────────────────────────────────────────


def glob_handler(payload: dict[str, Any]) -> str:
    pattern = payload["pattern"]
    base = (
        _ensure_inside_base(payload["path"]) if payload.get("path") else BASE_DIR
    )
    matches: list[str] = []
    for p in sorted(base.glob(pattern)):
        try:
            _ensure_inside_base(p)
            matches.append(str(p))
        except PermissionError:
            continue
    if len(matches) > 1000:
        matches = matches[:1000]
        matches.append("(... truncated to first 1000 matches ...)")
    return "\n".join(matches)


# ─── Registration ────────────────────────────────────────────────


_HANDLERS: dict[str, Any] = {
    "Read": read_handler,
    "Write": write_handler,
    "Edit": edit_handler,
    "Bash": bash_handler,
    "Grep": grep_handler,
    "Glob": glob_handler,
}


def bind_to_dispatcher(dispatcher: ToolDispatcher) -> ToolDispatcher:
    """Register all 6 handlers on ``dispatcher``. Returns the same dispatcher."""
    for name, fn in _HANDLERS.items():
        dispatcher.register(name, fn)
    return dispatcher


def make_runner_dispatcher() -> ToolDispatcher:
    """Build a fresh dispatcher with all 6 handlers wired up."""
    return bind_to_dispatcher(ToolDispatcher())
