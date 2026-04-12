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
async def generate_artifact_report(template: str, title: str = "", context_json: str = "{}") -> str:
    """Generate a report from a template and save as an artifact.

    Available templates: compliance_report, test_summary.
    The context_json provides template variables as a JSON string.

    Args:
        template: Template name (e.g. "compliance_report", "test_summary").
        title: Report title.
        context_json: JSON string of template variables.
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
    result = await _gen(template, ctx, task_id="", agent_id=agent_id)
    if "error" in result:
        return f"[ERROR] {result['error']}"

    return f"[OK] Report generated: {result['name']} ({result['size']} bytes). Available templates: {list_templates()}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tool registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FILE_TOOLS = [read_file, write_file, list_directory, read_yaml, write_yaml, search_in_files]
GIT_TOOLS = [git_status, git_log, git_diff, git_diff_staged, git_branch, git_add, git_commit, git_checkout_branch, git_push, git_remote_list, create_pr, git_add_remote]
BASH_TOOLS = [run_bash]
REVIEW_TOOLS = [gerrit_get_diff, gerrit_post_comment, gerrit_submit_review]
TASK_TOOLS = [get_next_task, update_task_status, add_task_comment]
REPORT_TOOLS = [generate_artifact_report]

ALL_TOOLS = FILE_TOOLS + GIT_TOOLS + BASH_TOOLS + TASK_TOOLS

TOOL_MAP = {t.name: t for t in ALL_TOOLS + REVIEW_TOOLS + REPORT_TOOLS}

AGENT_TOOLS: dict[str, list] = {
    "firmware":  ALL_TOOLS,
    "software":  ALL_TOOLS,
    "validator":  FILE_TOOLS + GIT_TOOLS + [run_bash] + TASK_TOOLS,
    "reporter":   FILE_TOOLS + GIT_TOOLS + TASK_TOOLS + REPORT_TOOLS,
    "reviewer":   [read_file, list_directory, read_yaml, search_in_files] + [git_status, git_log, git_diff, git_diff_staged, git_branch] + REVIEW_TOOLS + [get_next_task, add_task_comment],
    "general":    ALL_TOOLS,
    "custom":     ALL_TOOLS,
    "devops":     ALL_TOOLS,
}
