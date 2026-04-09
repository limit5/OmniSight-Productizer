"""System tools that agents can invoke.

Every tool is a plain async function **and** a LangChain `@tool` so it works
both in rule-based fallback mode and LLM tool-calling mode.

Workspace-aware: when an agent has an isolated workspace provisioned,
all file/git/bash tools operate within that workspace instead of the
global project root. This is controlled via `set_active_workspace()`.

Safety:
 - File I/O is sandboxed to the active workspace root.
 - Bash commands run with a timeout and reject dangerous patterns.
 - Git push is restricted to agent/* branches only.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
from pathlib import Path

import yaml
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ─── Workspace context ───

# Global default (project root)
WORKSPACE_ROOT = Path(
    os.environ.get("OMNISIGHT_WORKSPACE", Path(__file__).resolve().parents[2])
)

BASH_TIMEOUT = int(os.environ.get("OMNISIGHT_BASH_TIMEOUT", "30"))

# Context variable: per-invocation workspace override
_active_workspace: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "_active_workspace", default=None
)


_active_agent_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_active_agent_id", default=None
)


def set_active_workspace(path: Path | None, agent_id: str | None = None) -> None:
    """Set the workspace root and agent ID for the current execution context."""
    _active_workspace.set(path)
    _active_agent_id.set(agent_id)


def get_active_workspace() -> Path:
    """Get the current workspace root (agent-specific or global default)."""
    return _active_workspace.get() or WORKSPACE_ROOT


def get_active_agent_id() -> str | None:
    """Get the active agent ID (for container routing)."""
    return _active_agent_id.get()


# ─── Safety ───

_DANGEROUS_PATTERNS = re.compile(
    r"(rm\s+-rf\s+/|mkfs|dd\s+if=|:(){ :|shutdown|reboot|halt"
    r"|>\s*/dev/sd|chmod\s+-R\s+777\s+/|curl.*\|\s*bash"
    r"|git\s+push\s+.*--force|git\s+push\s+.*-f\b)",
    re.IGNORECASE,
)

# Push is only allowed to agent/* branches
_SAFE_PUSH_PATTERN = re.compile(r"git\s+push\s+\S+\s+agent/", re.IGNORECASE)


def _safe_path(rel: str) -> Path:
    """Resolve a relative path inside the active workspace. Raise on escape."""
    root = get_active_workspace()
    target = (root / rel).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise PermissionError(f"Path escapes workspace: {rel}")
    return target


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. File system tools
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@tool
async def read_file(path: str) -> str:
    """Read a file from the workspace.

    Args:
        path: Relative path from workspace root (e.g. "src/main.c").
    """
    target = _safe_path(path)
    if not target.exists():
        return f"[ERROR] File not found: {path}"
    if target.stat().st_size > 512_000:
        return f"[ERROR] File too large (>{512_000} bytes): {path}"
    return target.read_text(encoding="utf-8", errors="replace")


@tool
async def write_file(path: str, content: str) -> str:
    """Write content to a file in the workspace. Creates parent dirs.

    Args:
        path: Relative path from workspace root.
        content: Full file content to write.
    """
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"[OK] Written {len(content)} bytes to {path}"


@tool
async def list_directory(path: str = ".") -> str:
    """List files and directories at the given path.

    Args:
        path: Relative directory path (default: workspace root).
    """
    root = get_active_workspace()
    target = _safe_path(path)
    if not target.is_dir():
        return f"[ERROR] Not a directory: {path}"
    skip_names = {".venv", "node_modules", ".next", ".git", "__pycache__"}
    entries = sorted(e for e in target.iterdir() if e.name not in skip_names)
    lines = []
    for entry in entries[:200]:
        prefix = "d " if entry.is_dir() else "f "
        try:
            rel = entry.relative_to(root)
        except ValueError:
            rel = entry.name
        size = ""
        if entry.is_file():
            size = f"  ({entry.stat().st_size} bytes)"
        lines.append(f"{prefix}{rel}{size}")
    return "\n".join(lines) or "[EMPTY]"


@tool
async def read_yaml(path: str) -> str:
    """Read and parse a YAML file, returning its structure as formatted text.

    Args:
        path: Relative path to the YAML file.
    """
    target = _safe_path(path)
    if not target.exists():
        return f"[ERROR] File not found: {path}"
    raw = target.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)
    except yaml.YAMLError as exc:
        return f"[ERROR] YAML parse error: {exc}"


@tool
async def write_yaml(path: str, content: str) -> str:
    """Parse a YAML string and write it to a file (validates before writing).

    Args:
        path: Relative path for the YAML file.
        content: YAML-formatted string to write.
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return f"[ERROR] Invalid YAML: {exc}"
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    return f"[OK] YAML written to {path}"


@tool
async def search_in_files(pattern: str, path: str = ".", glob: str = "*") -> str:
    """Search for a regex pattern in files under the given path.

    Args:
        pattern: Regex pattern to search for.
        path: Relative directory to search in.
        glob: File glob filter (e.g. "*.c", "*.yaml").
    """
    root = get_active_workspace()
    target = _safe_path(path)
    if not target.is_dir():
        return f"[ERROR] Not a directory: {path}"
    compiled = re.compile(pattern, re.IGNORECASE)
    matches: list[str] = []
    skip_dirs = {".venv", "node_modules", ".next", ".git", "__pycache__"}
    for fpath in sorted(target.rglob(glob)):
        if not fpath.is_file() or fpath.stat().st_size > 512_000:
            continue
        if any(part in skip_dirs for part in fpath.parts):
            continue
        try:
            for i, line in enumerate(fpath.read_text(errors="replace").splitlines(), 1):
                if compiled.search(line):
                    try:
                        rel = fpath.relative_to(root)
                    except ValueError:
                        rel = fpath.name
                    matches.append(f"{rel}:{i}: {line.strip()}")
                    if len(matches) >= 100:
                        matches.append("... (truncated at 100 results)")
                        return "\n".join(matches)
        except Exception:
            continue
    return "\n".join(matches) if matches else "[NO MATCHES]"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. Git tools
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _git(cmd: str, cwd: Path | None = None) -> str:
    """Run a git command in the active workspace."""
    work = cwd or get_active_workspace()
    proc = await asyncio.create_subprocess_shell(
        f"git {cmd}",
        cwd=work,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=BASH_TIMEOUT)
    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()
    if proc.returncode != 0:
        return f"[GIT ERROR] {err or out}"
    return out or err or "[OK]"


@tool
async def git_status() -> str:
    """Show the working tree status (git status --short)."""
    return await _git("status --short")


@tool
async def git_log(max_count: int = 10) -> str:
    """Show recent commit history.

    Args:
        max_count: Number of commits to show (default 10).
    """
    return await _git(f"log --oneline --no-decorate -n {min(max_count, 50)}")


@tool
async def git_diff(path: str = "") -> str:
    """Show unstaged changes, optionally for a specific file.

    Args:
        path: Optional relative file path to diff.
    """
    safe = ""
    if path:
        _safe_path(path)
        safe = f" -- {path}"
    result = await _git(f"diff{safe}")
    if len(result) > 20_000:
        return result[:20_000] + "\n... [diff truncated at 20 KB]"
    return result


@tool
async def git_diff_staged(path: str = "") -> str:
    """Show staged (cached) changes, optionally for a specific file.

    Args:
        path: Optional relative file path to diff.
    """
    safe = ""
    if path:
        _safe_path(path)
        safe = f" -- {path}"
    result = await _git(f"diff --cached{safe}")
    if len(result) > 20_000:
        return result[:20_000] + "\n... [diff truncated at 20 KB]"
    return result


@tool
async def git_branch() -> str:
    """List all local branches, highlighting the current one."""
    return await _git("branch --no-color")


@tool
async def git_add(path: str) -> str:
    """Stage a file for commit.

    Args:
        path: Relative file path to stage.
    """
    _safe_path(path)
    return await _git(f"add {path}")


@tool
async def git_commit(message: str) -> str:
    """Create a commit with the given message.

    Args:
        message: Commit message.
    """
    safe_msg = message.replace('"', '\\"')
    return await _git(f'commit -m "{safe_msg}"')


@tool
async def git_checkout_branch(branch: str, create: bool = False) -> str:
    """Switch to a branch, optionally creating it.

    Args:
        branch: Branch name.
        create: If True, create the branch (-b flag).
    """
    if not re.match(r"^[a-zA-Z0-9._/-]+$", branch):
        return "[ERROR] Invalid branch name"
    flag = "-b " if create else ""
    return await _git(f"checkout {flag}{branch}")


@tool
async def git_push(remote: str = "origin", branch: str = "") -> str:
    """Push current branch to remote. Only agent/* branches are allowed.

    Args:
        remote: Remote name (default: origin).
        branch: Branch to push (default: current branch).
    """
    if not branch:
        # Get current branch name
        branch = (await _git("rev-parse --abbrev-ref HEAD")).strip()
    if not branch.startswith("agent/"):
        return "[BLOCKED] Push is only allowed to agent/* branches for safety."
    return await _git(f"push {remote} {branch}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Bash execution (sandboxed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@tool
async def run_bash(command: str) -> str:
    """Execute a bash command in the workspace.

    If a Docker container is active for this agent, the command runs
    inside the container. Otherwise it runs on the host.

    The command is checked for dangerous patterns and runs with a timeout.

    Args:
        command: The shell command to execute.
    """
    if _DANGEROUS_PATTERNS.search(command):
        if "git" in command and "push" in command and _SAFE_PUSH_PATTERN.search(command):
            pass
        else:
            return "[BLOCKED] Command contains a dangerous pattern and was not executed."

    # Try container execution first
    agent_id = get_active_agent_id()
    if agent_id:
        from backend.container import get_container, exec_in_container
        container = get_container(agent_id)
        if container:
            try:
                safe_cmd = command.replace('"', '\\"')
                rc, output = await exec_in_container(container.container_id, safe_cmd)
                if rc != 0 and not output:
                    output = f"[CONTAINER EXIT CODE: {rc}]"
                prefix = "[DOCKER] "
                return prefix + (output[:15_000] if output else "[OK — no output]")
            except asyncio.TimeoutError:
                return f"[DOCKER TIMEOUT] Command did not complete within {BASH_TIMEOUT}s"
            except Exception as exc:
                # Fall through to host execution
                logger.warning("Container exec failed, falling back to host: %s", exc)

    # Host execution (default)
    workspace = get_active_workspace()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HOME": str(Path.home())},
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=BASH_TIMEOUT
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return f"[TIMEOUT] Command did not complete within {BASH_TIMEOUT}s"

    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()

    combined = ""
    if out:
        combined += out[:15_000]
    if err:
        combined += f"\n[STDERR]\n{err[:5_000]}"
    if proc.returncode != 0:
        combined += f"\n[EXIT CODE: {proc.returncode}]"

    return combined or "[OK — no output]"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tool registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FILE_TOOLS = [read_file, write_file, list_directory, read_yaml, write_yaml, search_in_files]
GIT_TOOLS = [git_status, git_log, git_diff, git_diff_staged, git_branch, git_add, git_commit, git_checkout_branch, git_push]
BASH_TOOLS = [run_bash]

ALL_TOOLS = FILE_TOOLS + GIT_TOOLS + BASH_TOOLS

TOOL_MAP = {t.name: t for t in ALL_TOOLS}

AGENT_TOOLS: dict[str, list] = {
    "firmware":  ALL_TOOLS,
    "software":  ALL_TOOLS,
    "validator":  FILE_TOOLS + GIT_TOOLS + [run_bash],
    "reporter":   FILE_TOOLS + GIT_TOOLS,
    "general":    FILE_TOOLS + GIT_TOOLS + BASH_TOOLS,
}
