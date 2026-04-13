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
    root = get_active_workspace().resolve()
    target = (root / rel).resolve()
    # Use is_relative_to for safe path component comparison
    # (string startswith is vulnerable: /home/user/work allows /home/user/workspace)
    try:
        target.relative_to(root)
    except ValueError:
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
    # CODEOWNERS check: warn or block if agent doesn't own this file
    agent_id = get_active_agent_id()
    if agent_id:
        try:
            from backend.codeowners import check_file_permission
            from backend.routers.agents import _agents
            agent = _agents.get(agent_id)
            if agent:
                allowed, reason = check_file_permission(path, agent.type.value, agent.sub_type)
                if not allowed:
                    return f"[BLOCKED] {reason}"
                if reason:
                    import logging
                    logging.getLogger(__name__).info("CODEOWNERS: %s", reason)
        except Exception:
            pass  # CODEOWNERS check is best-effort
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

async def _git(cmd: str, cwd: Path | None = None, auth_for_url: str | None = None) -> str:
    """Run a git command in the active workspace.

    If *auth_for_url* is provided, injects authentication env vars for
    that remote URL (supports GitHub/GitLab tokens and SSH keys).
    """
    work = cwd or get_active_workspace()
    env = None
    if auth_for_url:
        from backend.git_auth import get_auth_env
        extra = get_auth_env(auth_for_url)
        if extra:
            env = {**os.environ, **extra}
    proc = await asyncio.create_subprocess_shell(
        f"git {cmd}",
        cwd=work,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=BASH_TIMEOUT)
    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()
    if proc.returncode != 0:
        return f"[GIT ERROR] {err or out}"
    return out or err or "[OK]"


async def _get_remote_url(remote: str = "origin", cwd: Path | None = None) -> str:
    """Get the URL of a git remote."""
    work = cwd or get_active_workspace()
    proc = await asyncio.create_subprocess_shell(
        f'git remote get-url "{remote}"',
        cwd=work,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    return stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""


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
    import shlex
    safe = ""
    if path:
        _safe_path(path)
        safe = f" -- {shlex.quote(path)}"
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
    import shlex
    safe = ""
    if path:
        _safe_path(path)
        safe = f" -- {shlex.quote(path)}"
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
    import shlex
    _safe_path(path)
    return await _git(f"add {shlex.quote(path)}")


@tool
async def git_commit(message: str) -> str:
    """Create a commit with the given message.

    Args:
        message: Commit message.
    """
    import shlex
    return await _git(f"commit -m {shlex.quote(message)}")


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
async def git_push(remote: str = "", branch: str = "", target_branch: str = "main") -> str:
    """Push current branch to remote. Only agent/* branches are allowed.

    When Gerrit is enabled and the remote is a Gerrit server, automatically
    pushes to ``refs/for/{target_branch}`` for code review.

    Args:
        remote: Remote name (auto-detect if empty).
        branch: Branch to push (default: current branch).
        target_branch: Target branch for Gerrit review (default: main).
    """
    if not branch:
        branch = (await _git("rev-parse --abbrev-ref HEAD")).strip()
    if not branch.startswith("agent/"):
        return "[BLOCKED] Push is only allowed to agent/* branches for safety."
    # Auto-detect remote if not specified
    if not remote:
        remotes_out = await _git("remote")
        remotes = [r.strip() for r in remotes_out.splitlines() if r.strip() and not r.startswith("[")]
        remote = "origin" if "origin" in remotes else (remotes[0] if remotes else "origin")
    # Get remote URL for auth injection
    remote_url = await _get_remote_url(remote)

    # Gerrit mode: push to refs/for/{target} for code review
    from backend.config import settings
    from backend.git_auth import detect_platform
    if settings.gerrit_enabled and detect_platform(remote_url) == "gerrit":
        refspec = f"HEAD:refs/for/{target_branch}"
        return await _git(f"push {remote} {refspec}", auth_for_url=remote_url)

    return await _git(f"push {remote} {branch}", auth_for_url=remote_url)


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

    # Redirect direct simulate.sh invocations to the dedicated run_simulation tool
    if re.search(r'(?:^|[/\s])simulate\.sh\b', command):
        return "[REDIRECT] Please use the run_simulation tool instead of calling simulate.sh directly. It provides structured JSON parsing, DB tracking, and proper timeout (120s)."

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

@tool
async def git_remote_list() -> str:
    """List all git remotes and their URLs."""
    return await _git("remote -v")


@tool
async def create_pr(remote: str = "", title: str = "", description: str = "") -> str:
    """Create a Pull Request (GitHub) or Merge Request (GitLab).

    Auto-detects platform from the remote URL.

    Args:
        remote: Remote name (auto-detect if empty).
        title: PR/MR title (auto-generated from branch name if empty).
        description: PR/MR description body.
    """
    from backend.git_platform import create_merge_request
    from backend.workspace import _detect_base_branch

    workspace = get_active_workspace()

    # Get current branch
    branch = (await _git("rev-parse --abbrev-ref HEAD")).strip()
    if not branch.startswith("agent/"):
        return "[BLOCKED] PR/MR creation is only allowed from agent/* branches."

    # Auto-detect remote
    if not remote:
        remotes_out = await _git("remote")
        remotes = [r.strip() for r in remotes_out.splitlines() if r.strip() and not r.startswith("[")]
        remote = "origin" if "origin" in remotes else (remotes[0] if remotes else "origin")

    # Auto-detect target branch
    target = await _detect_base_branch(workspace)

    # Auto-generate title if empty
    if not title:
        title = f"[Agent] {branch.split('/')[-1]}"

    result = await create_merge_request(
        repo_path=workspace,
        remote=remote,
        source_branch=branch,
        target_branch=target,
        title=title,
        description=description,
    )

    if "error" in result:
        return f"[ERROR] {result['error']}"

    platform = result.get("platform", "unknown")
    url = result.get("url", "")
    return f"[OK] {platform.upper()} {'PR' if platform == 'github' else 'MR'} created: {url}"


@tool
async def git_add_remote(name: str, url: str) -> str:
    """Add a new git remote to the workspace.

    Args:
        name: Remote name (e.g. 'github', 'gitlab', 'upstream').
        url: Remote URL (HTTPS or SSH).
    """
    if not re.match(r"^[a-zA-Z0-9._-]+$", name):
        return "[ERROR] Invalid remote name"
    # Remove existing remote with same name (idempotent)
    await _git(f'remote remove "{name}" 2>/dev/null')
    return await _git(f'remote add "{name}" "{url}"')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Gerrit Code Review tools
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@tool
async def gerrit_get_diff(commit: str = "") -> str:
    """Get the diff of a Gerrit patchset for code review.

    If no commit is specified, uses the latest commit in the workspace.

    Args:
        commit: Git commit SHA (optional, defaults to HEAD).
    """
    from backend.config import settings
    if not settings.gerrit_enabled:
        return "[ERROR] Gerrit integration not enabled"
    workspace = get_active_workspace()
    target = commit or "HEAD"
    # Try parent diff first; fall back to --root for initial commits
    proc = await asyncio.create_subprocess_shell(
        f"git diff {target}~1..{target} 2>/dev/null || git diff --root {target}",
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=BASH_TIMEOUT)
    out = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        return f"[ERROR] {stderr.decode(errors='replace').strip()}"
    if len(out) > 20_000:
        return out[:20_000] + "\n... [diff truncated at 20 KB]"
    return out or "[EMPTY DIFF]"


@tool
async def gerrit_post_comment(commit: str, file: str, line: int, message: str) -> str:
    """Post an inline comment on a Gerrit patchset.

    Args:
        commit: Git commit SHA of the patchset.
        file: File path to comment on.
        line: Line number.
        message: Comment text.
    """
    from backend.config import settings
    from backend.gerrit import gerrit_client
    if not settings.gerrit_enabled:
        return "[ERROR] Gerrit integration not enabled"
    result = await gerrit_client.post_inline_comments(
        commit=commit,
        comments={file: [{"line": line, "message": message}]},
    )
    if "error" in result:
        return f"[ERROR] {result['error']}"
    return f"[OK] Comment posted on {file}:{line}"


@tool
async def gerrit_submit_review(commit: str, score: int, message: str = "") -> str:
    """Submit a Code-Review score on a Gerrit patchset.

    AI Reviewers can only give +1 (approve) or -1 (request changes).

    Args:
        commit: Git commit SHA of the patchset.
        score: Code-Review score (+1 or -1 only).
        message: Review summary message.
    """
    from backend.config import settings
    from backend.gerrit import gerrit_client
    if not settings.gerrit_enabled:
        return "[ERROR] Gerrit integration not enabled"
    # AI agents are limited to +1/-1
    if score not in (-1, 1):
        return "[BLOCKED] AI reviewers can only give Code-Review +1 or -1. +2 and Submit are reserved for human maintainers."
    result = await gerrit_client.post_review(
        commit=commit,
        message=message,
        labels={"Code-Review": score},
    )
    if "error" in result:
        return f"[ERROR] {result['error']}"
    return f"[OK] Code-Review {'+' if score > 0 else ''}{score} submitted for {commit[:8]}"


FILE_TOOLS = [read_file, write_file, list_directory, read_yaml, write_yaml, search_in_files]
GIT_TOOLS = [git_status, git_log, git_diff, git_diff_staged, git_branch, git_add, git_commit, git_checkout_branch, git_push, git_remote_list, create_pr, git_add_remote]
BASH_TOOLS = [run_bash]
REVIEW_TOOLS = [gerrit_get_diff, gerrit_post_comment, gerrit_submit_review]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. Issue tracking wrapper tools
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@tool
async def get_next_task(label_filter: str = "") -> str:
    """Get the next pending task from the backlog.

    Returns a simplified summary with title, acceptance criteria, and
    recent comments — optimized for LLM context window.

    Args:
        label_filter: Only return tasks with this label (e.g. "ai-assigned").
    """
    from backend.routers.tasks import _tasks
    from backend import db

    candidates = [
        t for t in _tasks.values()
        if t.status.value == "backlog"
        and (not label_filter or label_filter in (t.labels or []))
    ]
    if not candidates:
        return "[NO TASKS] No pending tasks in backlog" + (f" with label '{label_filter}'" if label_filter else "") + "."

    # Sort by priority
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    candidates.sort(key=lambda t: rank.get(t.priority.value if hasattr(t.priority, "value") else t.priority, 4))
    task = candidates[0]

    # Build concise summary (context window protection)
    lines = [
        f"Task ID: {task.id}",
        f"Title: {task.title}",
        f"Priority: {task.priority.value if hasattr(task.priority, 'value') else task.priority}",
    ]
    if task.description:
        lines.append(f"Description: {task.description[:300]}")
    if task.acceptance_criteria:
        lines.append(f"Acceptance Criteria: {task.acceptance_criteria[:500]}")
    if task.suggested_agent_type:
        lines.append(f"Suggested Agent: {task.suggested_agent_type}")
    if task.external_issue_id:
        lines.append(f"External Issue: {task.external_issue_id}")
    if task.issue_url:
        lines.append(f"Issue URL: {task.issue_url}")

    # Include up to 3 recent comments
    try:
        comments = await db.list_task_comments(task.id, limit=3)
        if comments:
            lines.append("Recent Comments:")
            for c in comments:
                lines.append(f"  [{c['author']}] {c['content'][:100]}")
    except Exception:
        pass

    return "\n".join(lines)


@tool
async def update_task_status(task_id: str, status: str) -> str:
    """Update a task's status with state machine validation.

    Only transitions allowed by the state machine are accepted.
    Use get_next_task() first to see which task to work on.

    Args:
        task_id: The task ID to update.
        status: New status (backlog, assigned, in_progress, in_review, completed, blocked).
    """
    from backend.routers.tasks import _tasks, _persist
    from backend.models import TaskStatus, TASK_TRANSITIONS
    from backend.events import emit_task_update

    task = _tasks.get(task_id)
    if not task:
        return f"[ERROR] Task not found: {task_id}"

    current = task.status.value if hasattr(task.status, "value") else task.status
    allowed = TASK_TRANSITIONS.get(current, set())
    if status not in allowed:
        return f"[ERROR] Invalid transition: {current} → {status}. Allowed: {sorted(allowed)}"

    # Fact gate: in_review requires commits
    if status == "in_review" and task.assigned_agent_id:
        from backend.workspace import get_workspace
        ws = get_workspace(task.assigned_agent_id)
        if ws and ws.commit_count == 0:
            return "[ERROR] Cannot move to in_review: no commits in workspace. Push code first."

    task.status = TaskStatus(status)
    if status == "completed":
        from datetime import datetime
        task.completed_at = datetime.now().isoformat()
    await _persist(task)
    emit_task_update(task_id, task.status, task.assigned_agent_id)
    return f"[OK] Task {task_id} status updated: {current} → {status}"


@tool
async def add_task_comment(task_id: str, content: str) -> str:
    """Add a comment to a task's discussion thread.

    Use this to report progress, share Gerrit links, or note blockers.

    Args:
        task_id: The task to comment on.
        content: Comment text.
    """
    from backend.routers.tasks import _tasks
    from backend import db
    import uuid as _uuid

    if task_id not in _tasks:
        return f"[ERROR] Task not found: {task_id}"
    if not content.strip():
        return "[ERROR] Comment cannot be empty"

    # Use the active agent ID as author
    author = get_active_agent_id() or "agent"
    comment = {
        "id": f"comment-{_uuid.uuid4().hex[:8]}",
        "task_id": task_id,
        "author": author,
        "content": content,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }
    try:
        await db.insert_task_comment(comment)
    except Exception as exc:
        return f"[ERROR] Failed to save comment: {exc}"
    return f"[OK] Comment added to task {task_id} by {author}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. Report generation tools
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@tool
async def generate_artifact_report(template: str, title: str = "", context_json: str = "{}", task_id: str = "") -> str:
    """Generate a report from a template and save as an artifact.

    Available templates: compliance_report, test_summary.
    The context_json provides template variables as a JSON string.

    Args:
        template: Template name (e.g. "compliance_report", "test_summary").
        title: Report title.
        context_json: JSON string of template variables.
        task_id: Associated task ID for artifact tracking.
    """
    import json as _json
    from backend.report_generator import generate_report as _gen, list_templates

    try:
        ctx = _json.loads(context_json)
    except _json.JSONDecodeError:
        ctx = {}

    if title:
        ctx["title"] = title

    agent_id = get_active_agent_id() or "reporter"
    result = await _gen(template, ctx, task_id=task_id, agent_id=agent_id)
    if "error" in result:
        return f"[ERROR] {result['error']}"

    return f"[OK] Report generated: {result['name']} ({result['size']} bytes). Available templates: {list_templates()}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. Platform / Vendor SDK tools
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@tool
async def get_platform_config(platform: str = "") -> str:
    """Get build parameters (ARCH, CROSS_COMPILE, sysroot, cmake) for a platform.

    Args:
        platform: Platform profile name (e.g. 'aarch64', 'vendor-example').
                 If empty, reads from workspace .omnisight/platform hint.
    """
    from pathlib import Path as _Path

    if not platform:
        ws = get_active_workspace()
        hint = ws / ".omnisight" / "platform"
        platform = hint.read_text().strip() if hint.exists() else "aarch64"

    profile = _Path(__file__).resolve().parent.parent.parent / "configs" / "platforms" / f"{platform}.yaml"
    if not profile.exists():
        return f"[ERROR] Platform profile not found: {platform}"

    try:
        import yaml
        data = yaml.safe_load(profile.read_text())
    except Exception as exc:
        return f"[ERROR] Failed to parse platform YAML: {exc}"

    lines = [
        f"PLATFORM={data.get('platform', platform)}",
        f"ARCH={data.get('kernel_arch', 'arm64')}",
        f"CROSS_COMPILE={data.get('cross_prefix', '')}",
        f"TOOLCHAIN={data.get('toolchain', 'gcc')}",
        f"ARCH_FLAGS={data.get('arch_flags', '')}",
        f"QEMU={data.get('qemu', '')}",
    ]
    vendor = data.get("vendor_id", "")
    if vendor:
        lines.append(f"VENDOR_ID={vendor}")
        lines.append(f"SDK_VERSION={data.get('sdk_version', '')}")
    sysroot = data.get("sysroot_path", "")
    if sysroot:
        lines.append(f"SYSROOT={sysroot}")
    cmake_tc = data.get("cmake_toolchain_file", "")
    if cmake_tc:
        lines.append(f"CMAKE_TOOLCHAIN_FILE={cmake_tc}")
    # NPU acceleration fields
    if data.get("npu_enabled"):
        lines.append("NPU_ENABLED=true")
        lines.append(f"NPU_TYPE={data.get('npu_type', '')}")
        npu_sdk = data.get("npu_sdk_path", "")
        if npu_sdk:
            lines.append(f"NPU_SDK_PATH={npu_sdk}")
        npu_fmt = data.get("npu_model_format", "")
        if npu_fmt:
            lines.append(f"NPU_MODEL_FORMAT={npu_fmt}")
        npu_ver = data.get("npu_toolchain_version", "")
        if npu_ver:
            lines.append(f"NPU_TOOLCHAIN_VERSION={npu_ver}")

    # Deploy fields (for EVK deployment)
    deploy_method = data.get("deploy_method", "")
    if deploy_method:
        lines.append(f"DEPLOY_METHOD={deploy_method}")
        lines.append(f"DEPLOY_TARGET_IP={data.get('deploy_target_ip', '')}")
        lines.append(f"DEPLOY_USER={data.get('deploy_user', 'root')}")
        lines.append(f"DEPLOY_PATH={data.get('deploy_path', '/opt/app')}")

    return "[OK] Platform config:\n" + "\n".join(lines)


PLATFORM_TOOLS = [get_platform_config]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9.5. Hardware deploy tools
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEPLOY_TIMEOUT = 60  # seconds


@tool
async def check_evk_connection(platform: str = "") -> str:
    """Check if an EVK (evaluation kit) board is reachable via SSH.

    Args:
        platform: Platform profile name (e.g. 'vendor-example'). If empty, auto-detect.
    """
    deploy_info = await _get_deploy_info(platform)
    if not deploy_info:
        return "[ERROR] No deploy configuration found. Set deploy_method and deploy_target_ip in platform YAML."
    ip = deploy_info.get("ip", "")
    if not ip:
        return "[NOT_CONFIGURED] deploy_target_ip is empty. Set it in configs/platforms/{platform}.yaml"

    method = deploy_info.get("method", "ssh")
    user = deploy_info.get("user", "root")

    if method == "ssh":
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes", f"{user}@{ip}", "echo", "OMNISIGHT_OK",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode().strip()
            if "OMNISIGHT_OK" in output:
                return f"[OK] EVK reachable: {user}@{ip} (SSH)"
            return f"[ERROR] EVK SSH connected but unexpected response: {output[:100]}"
        except asyncio.TimeoutError:
            return f"[ERROR] EVK SSH timeout: {user}@{ip}"
        except Exception as exc:
            return f"[ERROR] EVK SSH failed: {exc}"
    elif method in ("adb", "fastboot"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb", "devices",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode()
            if "device" in output and "List" not in output.split("\n")[-2]:
                return f"[OK] ADB device detected:\n{output.strip()}"
            return "[NOT_CONNECTED] No ADB device found"
        except Exception as exc:
            return f"[ERROR] ADB check failed: {exc}"
    return f"[ERROR] Unsupported deploy method: {method}"


@tool
async def deploy_to_evk(
    platform: str = "",
    binary_path: str = "",
    run_after_deploy: bool = True,
) -> str:
    """Deploy compiled binary to an EVK board via SSH/SCP.

    Args:
        platform: Platform profile name.
        binary_path: Path to compiled binary (relative to workspace).
        run_after_deploy: If True, execute the binary on the EVK after copying.
    """
    import time as _time
    start = _time.time()

    deploy_info = await _get_deploy_info(platform)
    if not deploy_info:
        return "[ERROR] No deploy configuration found."
    ip = deploy_info.get("ip", "")
    user = deploy_info.get("user", "root")
    remote_path = deploy_info.get("path", "/opt/app")
    method = deploy_info.get("method", "ssh")

    if not ip:
        return "[NOT_CONFIGURED] deploy_target_ip is empty."
    if method != "ssh":
        return f"[ERROR] Only SSH deploy is currently supported (got: {method})"

    import shlex

    workspace = get_active_workspace()
    if binary_path:
        # Validate path stays inside workspace (prevent traversal)
        try:
            src = _safe_path(binary_path)
        except PermissionError:
            return f"[BLOCKED] Path escapes workspace: {binary_path}"
    else:
        # Auto-detect: look for common build outputs
        for candidate in ["build/output", "build/bin", "out"]:
            src = workspace / candidate
            if src.exists():
                break
        else:
            src = workspace / "build"

    if not src.exists():
        return f"[ERROR] Binary not found: {src}. Build first with run_simulation --type=hw --mock=false"

    # Sanitize all values used in remote SSH commands
    safe_remote_path = shlex.quote(remote_path)
    safe_binary_name = shlex.quote(src.name)

    # SCP to EVK
    ssh_opts = ["-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no"]
    try:
        # Ensure remote directory exists
        proc = await asyncio.create_subprocess_exec(
            "ssh", *ssh_opts, f"{user}@{ip}", f"mkdir -p {safe_remote_path}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=DEPLOY_TIMEOUT)

        # Copy files
        proc = await asyncio.create_subprocess_exec(
            "scp", "-r", *ssh_opts, str(src), f"{user}@{ip}:{remote_path}/",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=DEPLOY_TIMEOUT)
        if proc.returncode != 0:
            return f"[ERROR] SCP failed: {stderr.decode()[:200]}"

        artifacts = [str(src.name)]
        remote_output = ""

        # Run after deploy
        if run_after_deploy:
            proc = await asyncio.create_subprocess_exec(
                "ssh", *ssh_opts, f"{user}@{ip}",
                f"cd {safe_remote_path} && chmod +x {safe_binary_name} && ./{safe_binary_name} 2>&1 | head -50",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=DEPLOY_TIMEOUT)
            remote_output = stdout.decode()[:500]

        duration = int((_time.time() - start) * 1000)
        return (
            f"[OK] Deployed to {user}@{ip}:{remote_path}\n"
            f"Artifacts: {', '.join(artifacts)}\n"
            f"Duration: {duration}ms\n"
            + (f"Output:\n{remote_output}" if remote_output else "")
        )
    except asyncio.TimeoutError:
        return f"[TIMEOUT] Deploy timed out after {DEPLOY_TIMEOUT}s"
    except Exception as exc:
        return f"[ERROR] Deploy failed: {exc}"


@tool
async def list_uvc_devices() -> str:
    """List connected UVC (USB Video Class) camera devices with their capabilities.

    Detects /dev/video* devices and queries V4L2 capabilities.
    """
    results = []

    # Try v4l2-ctl first
    try:
        proc = await asyncio.create_subprocess_exec(
            "v4l2-ctl", "--list-devices",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        devices_text = stdout.decode().strip()
        if devices_text:
            results.append(f"V4L2 Devices:\n{devices_text}")
    except Exception:
        pass

    # Enumerate /dev/video* directly
    import glob
    video_devices = sorted(glob.glob("/dev/video*"))
    if not video_devices:
        if not results:
            return "[NOT_FOUND] No UVC camera devices detected (/dev/video* empty, v4l2-ctl unavailable)"
        return "[OK] " + "\n".join(results)

    for dev in video_devices[:8]:  # Limit to 8 devices
        try:
            proc = await asyncio.create_subprocess_exec(
                "v4l2-ctl", "-d", dev, "--all",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            info = stdout.decode()
            # Extract key info
            name = ""
            for line in info.split("\n"):
                if "Card type" in line:
                    name = line.split(":", 1)[-1].strip()
                    break
            formats = []
            for line in info.split("\n"):
                if "Pixel Format" in line and "'" in line:
                    fmt = line.split("'")[1] if "'" in line else ""
                    if fmt and fmt not in formats:
                        formats.append(fmt)
            results.append(f"  {dev}: {name or 'Unknown'} (formats: {', '.join(formats[:5]) or 'N/A'})")
        except Exception:
            results.append(f"  {dev}: detected (v4l2-ctl unavailable)")

    return "[OK] UVC Cameras:\n" + "\n".join(results)


async def _get_deploy_info(platform: str = "") -> dict | None:
    """Read deploy configuration from platform YAML."""
    if not platform:
        # Auto-detect from workspace hint
        workspace = get_active_workspace()
        hint_file = workspace / ".omnisight" / "platform"
        if hint_file.exists():
            platform = hint_file.read_text().strip()
    if not platform:
        return None

    platform_dir = WORKSPACE_ROOT / "configs" / "platforms"
    profile = platform_dir / f"{platform}.yaml"
    if not profile.exists():
        return None

    data = yaml.safe_load(profile.read_text(encoding="utf-8")) or {}
    method = data.get("deploy_method", "")
    if not method:
        return None

    return {
        "method": method,
        "ip": data.get("deploy_target_ip", ""),
        "user": data.get("deploy_user", "root"),
        "path": data.get("deploy_path", "/opt/app"),
        "platform": platform,
    }


DEPLOY_TOOLS = [check_evk_connection, deploy_to_evk, list_uvc_devices]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. L2 Memory tools — context summarization
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Rough token estimate: 1 token ≈ 4 chars (English) / 2 chars (CJK)
_CHARS_PER_TOKEN = 3  # Conservative average for mixed EN/CJK content
_SUMMARY_TARGET_TOKENS = 300
_SUMMARY_TARGET_CHARS = _SUMMARY_TARGET_TOKENS * _CHARS_PER_TOKEN


@tool
async def summarize_state(
    conversation_text: str,
    max_summary_chars: int = _SUMMARY_TARGET_CHARS,
    include_system_state: bool = True,
) -> str:
    """Compress L2 working memory: summarize long conversation history into a concise digest.

    Call this tool when the context window is approaching capacity (80%+ usage).
    It produces a compact summary of what happened, decisions made, and current status,
    replacing verbose multi-turn history with a ~300-token digest.

    Args:
        conversation_text: The conversation history or context to summarize.
        max_summary_chars: Maximum characters for the output summary (default ~900).
        include_system_state: If True, append current system state snapshot.
    """
    if not conversation_text or not conversation_text.strip():
        return "[L2 SUMMARY] No conversation content to summarize."

    # Attempt LLM-based summarization
    try:
        from backend.agents.llm import get_llm
        llm = get_llm()
        if llm:
            from langchain_core.messages import SystemMessage, HumanMessage
            sys = SystemMessage(content=(
                "You are a concise summarizer for an embedded AI camera development system. "
                "Compress the following conversation into a structured digest with these sections:\n"
                "1. OBJECTIVE: What was the user trying to accomplish (1 line)\n"
                "2. ACTIONS TAKEN: Key tool executions and their results (bullet list, max 5)\n"
                "3. DECISIONS: Important decisions or conclusions reached\n"
                "4. CURRENT STATUS: Where things stand right now\n"
                "5. PENDING: What still needs to be done\n\n"
                f"Keep the entire summary under {max_summary_chars} characters. "
                "Use terse, technical language. No filler words."
            ))
            resp = llm.invoke([sys, HumanMessage(content=conversation_text[:8000])])
            summary = resp.content  # type: ignore[union-attr]
            if len(summary) > max_summary_chars:
                summary = summary[:max_summary_chars] + "..."
            result = f"[L2 SUMMARY]\n{summary}"
            if include_system_state:
                state_snap = _get_system_snapshot()
                if state_snap:
                    result += f"\n\n[SYSTEM STATE]\n{state_snap}"
            return result
    except Exception as exc:
        logger.warning("L2 summarize LLM failed, falling back to rule-based: %s", exc)

    # Rule-based fallback: extract key patterns from conversation text
    summary_parts = []
    lines = conversation_text.strip().split("\n")

    # Extract tool results
    tool_results = [l.strip() for l in lines if l.strip().startswith(("[OK]", "[PASS]", "[FAIL]", "[ERROR]"))]
    if tool_results:
        summary_parts.append("Tool Results:")
        for tr in tool_results[:5]:
            summary_parts.append(f"  {tr[:120]}")

    # Extract decisions / key statements
    decision_markers = ["decided", "conclusion", "agreed", "confirmed", "chosen", "selected", "fixed", "resolved"]
    decisions = [l.strip() for l in lines if any(m in l.lower() for m in decision_markers)]
    if decisions:
        summary_parts.append("Decisions:")
        for d in decisions[:3]:
            summary_parts.append(f"  {d[:120]}")

    # Extract errors
    errors = [l.strip() for l in lines if "[ERROR]" in l or "error:" in l.lower()]
    if errors:
        summary_parts.append("Errors:")
        for e in errors[:3]:
            summary_parts.append(f"  {e[:120]}")

    if not summary_parts:
        # Last resort: take first and last N lines
        head = lines[:3]
        tail = lines[-3:] if len(lines) > 6 else []
        summary_parts = [l[:120] for l in head]
        if tail:
            summary_parts.append("...")
            summary_parts.extend(l[:120] for l in tail)

    result = "[L2 SUMMARY] (rule-based)\n" + "\n".join(summary_parts)
    if len(result) > max_summary_chars:
        result = result[:max_summary_chars] + "..."

    if include_system_state:
        state_snap = _get_system_snapshot()
        if state_snap:
            result += f"\n\n[SYSTEM STATE]\n{state_snap}"

    return result


def _get_system_snapshot() -> str:
    """Get a compact system state snapshot for L2 context injection."""
    try:
        from backend.routers.invoke import _agents, _tasks
        agents_list = list(_agents.values())
        tasks_list = list(_tasks.values())
        running = sum(1 for a in agents_list if a.status.value == "running")
        idle = sum(1 for a in agents_list if a.status.value == "idle")
        pending = sum(1 for t in tasks_list if t.status.value == "backlog")
        in_prog = sum(1 for t in tasks_list if t.status.value in ("assigned", "in_progress"))
        completed = sum(1 for t in tasks_list if t.status.value == "completed")
        return (
            f"Agents: {len(agents_list)} ({running} running, {idle} idle) | "
            f"Tasks: {len(tasks_list)} ({pending} pending, {in_prog} active, {completed} done)"
        )
    except Exception:
        return ""


MEMORY_TOOLS = [summarize_state]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  10. L3 Episodic Memory tools — long-term knowledge base
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@tool
async def search_past_solutions(
    error_signature: str,
    soc_vendor: str = "",
    sdk_version: str = "",
    limit: int = 3,
) -> str:
    """Search L3 episodic memory for past solutions to similar errors.

    Call this tool when encountering an unfamiliar error — especially linker errors,
    SDK-specific build failures, or hardware configuration issues. The L3 memory
    stores solutions from previously merged Gerrit patchsets.

    IMPORTANT: Always verify that the returned solution's soc_vendor and sdk_version
    match your current environment before applying it.

    Args:
        error_signature: The error message or pattern to search for.
        soc_vendor: Filter by SoC vendor (e.g. 'rockchip', 'fullhan').
        sdk_version: Filter by SDK version (e.g. '1.2', '3.0').
        limit: Max number of results to return.
    """
    from backend import db

    try:
        results = await db.search_episodic_memory(
            query=error_signature,
            soc_vendor=soc_vendor,
            sdk_version=sdk_version,
            limit=limit,
        )
    except Exception as exc:
        return f"[ERROR] L3 search failed: {exc}"

    if not results:
        return f"[L3] No past solutions found for: {error_signature[:100]}"

    lines = [f"[L3] Found {len(results)} past solution(s):\n"]
    for i, r in enumerate(results, 1):
        vendor_info = f" | vendor={r['soc_vendor']}" if r.get("soc_vendor") else ""
        sdk_info = f" | sdk={r['sdk_version']}" if r.get("sdk_version") else ""
        hw_info = f" | hw={r['hardware_rev']}" if r.get("hardware_rev") else ""
        score = f" | quality={r.get('quality_score', 0):.1f}"
        lines.append(
            f"  {i}. Error: {r['error_signature'][:120]}\n"
            f"     Solution: {r['solution'][:300]}\n"
            f"     Meta:{vendor_info}{sdk_info}{hw_info}{score}\n"
        )
    return "\n".join(lines)


@tool
async def save_solution(
    error_signature: str,
    solution: str,
    soc_vendor: str = "",
    sdk_version: str = "",
    hardware_rev: str = "",
    gerrit_change_id: str = "",
    tags: list[str] | None = None,
) -> str:
    """Save a verified solution to L3 episodic memory.

    IMPORTANT: This should ONLY be called after a solution has been verified
    (e.g., Gerrit +2 merge, all tests passing). Do NOT save unverified attempts,
    failed fixes, or speculative solutions.

    Args:
        error_signature: The error message or pattern this solution addresses.
        solution: The fix description (what was changed and why).
        soc_vendor: SoC vendor (e.g. 'rockchip', 'fullhan', 'ambarella').
        sdk_version: SDK version this solution applies to.
        hardware_rev: Hardware revision / EVK board version.
        gerrit_change_id: Gerrit change ID (for traceability).
        tags: Classification tags (e.g. ['linker', 'v4l2', 'cmake']).
    """
    import uuid
    from backend import db

    if not error_signature or not solution:
        return "[ERROR] Both error_signature and solution are required."

    memory_id = f"mem-{uuid.uuid4().hex[:12]}"
    try:
        await db.insert_episodic_memory({
            "id": memory_id,
            "error_signature": error_signature,
            "solution": solution,
            "soc_vendor": soc_vendor,
            "sdk_version": sdk_version,
            "hardware_rev": hardware_rev,
            "gerrit_change_id": gerrit_change_id,
            "tags": tags or [],
            "quality_score": 1.0 if gerrit_change_id else 0.5,
        })
    except Exception as exc:
        return f"[ERROR] Failed to save to L3: {exc}"

    return (
        f"[L3] Solution saved (id={memory_id}): "
        f"{error_signature[:60]} → {solution[:60]}... "
        f"(vendor={soc_vendor or 'any'}, sdk={sdk_version or 'any'})"
    )


EPISODIC_TOOLS = [search_past_solutions, save_solution]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  11. Simulation tools
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SIMULATION_TIMEOUT = 120  # seconds — Valgrind/QEMU are slow


@tool
async def run_simulation(
    track: str, module: str, input_data: str = "", mock: bool = True,
    platform: str = "aarch64", model_path: str = "", framework: str = "",
    test_images: str = "",
) -> str:
    """Run simulation for a firmware, algorithm, or NPU inference module.

    Args:
        track: 'algo' (data-driven), 'hw' (peripheral mock/QEMU), or 'npu' (NPU model inference).
        module: Module name under src/ (e.g. 'core_algorithm', 'detect', 'face').
        input_data: Optional input file path relative to test_assets/.
        mock: For hw track, True=mock sysfs, False=QEMU cross-run.
        platform: Target platform profile (aarch64, armv7, riscv64, vendor-xxx).
        model_path: (npu track) Path to model file (.rknn, .tflite, .engine).
        framework: (npu track) Inference framework: rknn, tflite, tensorrt.
        test_images: (npu track) Path to test image dataset directory.
    """
    import json as _json
    import uuid
    from datetime import datetime as _dt

    from backend import db
    from backend.events import emit_simulation

    if track not in ("algo", "hw", "npu"):
        return "[ERROR] track must be 'algo', 'hw', or 'npu'"

    sim_id = f"sim-{uuid.uuid4().hex[:8]}"
    now = _dt.now().isoformat()

    # Insert running record
    try:
        await db.insert_simulation({
            "id": sim_id, "task_id": "", "agent_id": get_active_agent_id() or "",
            "track": track, "module": module, "status": "running",
            "tests_total": 0, "tests_passed": 0, "tests_failed": 0,
            "coverage_pct": 0.0, "valgrind_errors": 0, "duration_ms": 0,
            "report_json": "{}", "artifact_id": None, "created_at": now,
        })
    except Exception as exc:
        return f"[ERROR] Failed to initialize simulation record: {exc}"
    emit_simulation(sim_id, "start", f"{track}/{module} on {platform}")

    # Build command
    cmd_parts = [
        "/opt/omnisight/simulate.sh",
        f"--type={track}",
        f"--module={module}",
        f"--platform={platform}",
        f"--mock={'true' if mock else 'false'}",
        "--coverage-check=true",
    ]
    if input_data:
        cmd_parts.append(f"--input={input_data}")
    # NPU-specific arguments
    if track == "npu":
        if model_path:
            cmd_parts.append(f"--npu-model={model_path}")
        if framework:
            cmd_parts.append(f"--framework={framework}")
        if test_images:
            cmd_parts.append(f"--test-images={test_images}")
    cmd = " ".join(cmd_parts)

    # Execute in container or host
    raw_output = ""
    try:
        agent_id = get_active_agent_id()
        if agent_id:
            try:
                from backend.container import get_container, exec_in_container
                container = get_container(agent_id)
                if container:
                    rc, raw_output = await exec_in_container(
                        container.container_id, cmd, timeout=SIMULATION_TIMEOUT
                    )
                else:
                    raise RuntimeError("No container")
            except Exception:
                # Fallback to host
                workspace = get_active_workspace()
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=workspace,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=SIMULATION_TIMEOUT
                )
                raw_output = (stdout or b"").decode(errors="replace")
                if not raw_output.strip() and stderr:
                    raw_output = (stderr or b"").decode(errors="replace")
        else:
            workspace = get_active_workspace()
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=SIMULATION_TIMEOUT
            )
            raw_output = (stdout or b"").decode(errors="replace")
            if not raw_output.strip() and stderr:
                raw_output = (stderr or b"").decode(errors="replace")
    except asyncio.TimeoutError:
        await db.update_simulation(sim_id, {"status": "error", "report_json": '{"errors":["Timeout"]}'})
        emit_simulation(sim_id, "result", "Timeout", status="error")
        return f"[TIMEOUT] Simulation {sim_id} timed out after {SIMULATION_TIMEOUT}s"

    # Parse JSON report from stdout
    report = {}
    try:
        report = _json.loads(raw_output.strip())
    except (ValueError, _json.JSONDecodeError):
        await db.update_simulation(sim_id, {
            "status": "error",
            "report_json": _json.dumps({"errors": ["Failed to parse JSON output"], "raw": raw_output[:500]}),
        })
        emit_simulation(sim_id, "result", "JSON parse error", status="error")
        return f"[ERROR] Simulation {sim_id}: failed to parse JSON output. Raw: {raw_output[:300]}"

    # Extract structured fields
    status = report.get("status", "error")
    tests = report.get("tests", {})
    coverage = report.get("coverage", {})
    valgrind = report.get("valgrind", {})

    update_data = {
        "status": status,
        "tests_total": tests.get("total", 0),
        "tests_passed": tests.get("passed", 0),
        "tests_failed": tests.get("failed", 0),
        "coverage_pct": coverage.get("percentage", 0.0),
        "valgrind_errors": valgrind.get("errors", 0),
        "duration_ms": report.get("duration_ms", 0),
        "report_json": _json.dumps(report),
    }
    # NPU-specific fields
    if track == "npu":
        npu = report.get("npu", {})
        update_data.update({
            "npu_latency_ms": npu.get("latency_ms", 0.0),
            "npu_throughput_fps": npu.get("throughput_fps", 0.0),
            "accuracy_delta": npu.get("accuracy_delta", 0.0),
            "model_size_kb": npu.get("model_size_kb", 0),
            "npu_framework": npu.get("framework", framework or ""),
        })
    await db.update_simulation(sim_id, update_data)

    emit_simulation(sim_id, "result", f"{status}: {tests.get('passed', 0)}/{tests.get('total', 0)} tests",
                    status=status, track=track, module=module,
                    tests_total=tests.get("total", 0), tests_passed=tests.get("passed", 0),
                    tests_failed=tests.get("failed", 0))

    # Return concise summary (not full JSON — save tokens)
    errors = report.get("errors", [])
    error_str = f" Errors: {'; '.join(str(e) for e in errors[:3])}" if errors else ""
    valgrind_str = f" Valgrind: {valgrind.get('errors', 0)} error(s)." if valgrind.get("ran") else ""
    npu_str = ""
    if track == "npu":
        npu = report.get("npu", {})
        npu_str = (
            f" NPU: {npu.get('latency_ms', 0):.1f}ms/frame,"
            f" {npu.get('throughput_fps', 0):.1f}fps,"
            f" accuracy_delta={npu.get('accuracy_delta', 0):.2f}."
        )
    return (
        f"[{'PASS' if status == 'pass' else 'FAIL'}] Simulation {sim_id} ({track}/{module}): "
        f"{tests.get('passed', 0)}/{tests.get('total', 0)} tests passed, "
        f"coverage {coverage.get('percentage', 0):.0f}%, "
        f"duration {report.get('duration_ms', 0)}ms."
        f"{valgrind_str}{npu_str}{error_str}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tool registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FILE_TOOLS = [read_file, write_file, list_directory, read_yaml, write_yaml, search_in_files]
GIT_TOOLS = [git_status, git_log, git_diff, git_diff_staged, git_branch, git_add, git_commit, git_checkout_branch, git_push, git_remote_list, create_pr, git_add_remote]
BASH_TOOLS = [run_bash]
REVIEW_TOOLS = [gerrit_get_diff, gerrit_post_comment, gerrit_submit_review]
TASK_TOOLS = [get_next_task, update_task_status, add_task_comment]
REPORT_TOOLS = [generate_artifact_report]
SIMULATION_TOOLS = [run_simulation]

# Base tools available to most agents (excludes specialist tools: review, report, simulation)
ALL_TOOLS = FILE_TOOLS + GIT_TOOLS + BASH_TOOLS + TASK_TOOLS

# Complete registry of every tool for executor lookup (must include ALL tool categories)
TOOL_MAP = {t.name: t for t in ALL_TOOLS + REVIEW_TOOLS + REPORT_TOOLS + SIMULATION_TOOLS + PLATFORM_TOOLS + MEMORY_TOOLS + EPISODIC_TOOLS + DEPLOY_TOOLS}

AGENT_TOOLS: dict[str, list] = {
    "firmware":       ALL_TOOLS + SIMULATION_TOOLS + PLATFORM_TOOLS + MEMORY_TOOLS + EPISODIC_TOOLS + DEPLOY_TOOLS,
    "software":       ALL_TOOLS + SIMULATION_TOOLS + PLATFORM_TOOLS + MEMORY_TOOLS + EPISODIC_TOOLS,
    "validator":      FILE_TOOLS + GIT_TOOLS + [run_bash] + TASK_TOOLS + SIMULATION_TOOLS + PLATFORM_TOOLS + MEMORY_TOOLS + EPISODIC_TOOLS + DEPLOY_TOOLS,
    "reporter":       FILE_TOOLS + GIT_TOOLS + TASK_TOOLS + REPORT_TOOLS + MEMORY_TOOLS,
    "reviewer":       [read_file, list_directory, read_yaml, search_in_files] + [git_status, git_log, git_diff, git_diff_staged, git_branch] + REVIEW_TOOLS + [get_next_task, add_task_comment] + MEMORY_TOOLS,
    "general":        ALL_TOOLS + MEMORY_TOOLS + EPISODIC_TOOLS + DEPLOY_TOOLS,
    "custom":         ALL_TOOLS + MEMORY_TOOLS + EPISODIC_TOOLS + DEPLOY_TOOLS,
    "devops":         ALL_TOOLS + PLATFORM_TOOLS + MEMORY_TOOLS + EPISODIC_TOOLS + DEPLOY_TOOLS,
    "mechanical":     FILE_TOOLS + BASH_TOOLS + TASK_TOOLS + SIMULATION_TOOLS + MEMORY_TOOLS,
    "manufacturing":  FILE_TOOLS + BASH_TOOLS + TASK_TOOLS + SIMULATION_TOOLS + MEMORY_TOOLS,
}
