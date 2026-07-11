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
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from backend.llm_adapter import tool
from backend.db_pool import get_pool
from backend.sandbox_tier import Guild

logger = logging.getLogger(__name__)
__path__ = [str(Path(__file__).with_suffix(""))]

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


# Chat request context (Gap C, 2026-07-01): the user-facing chat endpoint
# sets (user_id, session_id, tenant_id) here before invoking the graph, so
# ``create_task`` can record WHO filed a ticket and WHERE to deliver the
# "done" notification back to. Contextvars propagate into the graph's task,
# so the tool sees the caller even though run_graph doesn't thread it.
_chat_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_chat_context", default=None
)


def set_chat_context(user_id: str, session_id: str, tenant_id: str = "") -> None:
    """Bind the current chat caller for tools that need it (create_task)."""
    _chat_context.set(
        {"user_id": user_id or "", "session_id": session_id or "", "tenant_id": tenant_id or ""}
    )


def get_chat_context() -> dict | None:
    """Return the current chat caller context, or None outside a chat turn."""
    return _chat_context.get()


# ─── Safety ───

_DANGEROUS_PATTERNS = re.compile(
    # ── Classic destruction / system-downing (pre-existing) ──
    r"(rm\s+-rf\s+/|mkfs|dd\s+if=|:(){ :|shutdown|reboot|halt"
    r"|>\s*/dev/sd|chmod\s+-R\s+777\s+/|curl.*\|\s*bash"
    r"|git\s+push\s+.*--force|git\s+push\s+.*-f\b"
    # ── C2 audit (2026-04-19): prompt-injection → exfiltration ──
    # Data-exfil targets: secret files that should never be read by agent.
    r"|(\.env\b|\b\.ssh/|id_rsa|id_ecdsa|id_ed25519|authorized_keys"
    r"|/etc/shadow|/etc/passwd|/etc/sudoers|/root/|\.aws/credentials"
    r"|\.kube/config)"
    # Outbound exfil sinks — curl/wget paired with a shell-variable
    # anywhere in the same command segment. Bare `curl https://pypi.org/`
    # is fine; `curl -d $TOKEN https://…` or `wget --header=$TOKEN …`
    # is the class we want to block, regardless of flag order. We stop
    # greedy matching at the first `|`, `&`, `;`, or newline so the
    # match is scoped to a single shell segment.
    r"|(curl|wget)[^|&;\n]*\$"
    r"|base64[^|]*\|\s*(curl|wget|nc|ssh)"
    r"|printenv\s*\||env\s*\|\s*(grep|curl|wget|nc)"
    # Reverse-shell patterns (gleaned from CVE commentary).
    r"|nc\s+-[a-z]*e\s|bash\s+-i\s*(<|>|&)|/dev/tcp/|socat.*exec"
    r"|mkfifo.*\|\s*nc|python[0-9]?\s+-c\s+.*socket\.socket"
    # Python/perl/ruby/node one-liner RCE via -c/-e with dangerous import.
    r"|(python[0-9]?|perl|ruby|node)\s+-[ce]\s+.*import\s+(os|subprocess|socket|pty)"
    # Docker-socket direct access (in case docker-socket-proxy is bypassed)
    r"|/var/run/docker\.sock)",
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
    try:
        from backend import pep_gateway as _pep
        dec = _pep.classify_native_read_path("read_file", {"path": path})
        if dec is not None:
            _action, rule, reason, _scope = dec
            return f"[BLOCKED] PEP denied read_file ({rule}): {reason}"
    except Exception:
        logger.warning("PEP native read-path guard raised", exc_info=True)
    target = _safe_path(path)
    if not target.exists():
        return f"[ERROR] File not found: {path}"
    if target.stat().st_size > 512_000:
        return f"[ERROR] File too large (>{512_000} bytes): {path}"
    return target.read_text(encoding="utf-8", errors="replace")


@tool
async def write_file(path: str, content: str) -> str:
    """Write content to a file in the workspace. Creates parent dirs.

    DEPRECATED for *modifying existing files* (Phase 67-B): overwriting
    a full file when you only changed a few lines wastes output tokens
    and invites hallucination. For edits, use `patch_file`. This tool
    will REFUSE to overwrite an existing file whose new body exceeds
    OMNISIGHT_PATCH_MAX_INLINE_LINES (default 50 lines). For genuinely
    new files use `create_file` — that path is uncapped.

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
    # Phase 67-B S2: deprecation interceptor for existing-file overwrites.
    if target.exists():
        import os as _os
        cap_raw = _os.environ.get("OMNISIGHT_PATCH_MAX_INLINE_LINES", "50")
        try:
            cap = max(1, int(cap_raw))
        except ValueError:
            cap = 50
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        if line_count > cap:
            # Trigger IIS L1 calibrate (best-effort) and reject.
            try:
                from backend import intelligence as _iis
                _iis.record_and_publish(
                    agent_id or "unknown",
                    code_pass=False,  # treat as a quality incident
                )
            except Exception:
                pass
            return (
                f"[REJECTED] write_file on existing file {path!r} with "
                f"{line_count} lines exceeds cap {cap}. Use `patch_file` "
                f"with SEARCH/REPLACE or unified diff for edits. See "
                f"docs/operations/patching.md."
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"[OK] Written {len(content)} bytes to {path}"


@tool
async def create_file(path: str, content: str) -> str:
    """Create a NEW file with full content. Refuses if the path
    already exists (use `patch_file` to edit existing files).

    Uncapped (unlike `write_file`) — generated fixtures, boilerplate
    __init__.py, README templates, etc. are legitimately full files.

    Args:
        path: Relative path from workspace root.
        content: Full file content to write.
    """
    agent_id = get_active_agent_id()
    if agent_id:
        try:
            from backend.codeowners import check_file_permission
            from backend.routers.agents import _agents
            agent = _agents.get(agent_id)
            if agent:
                allowed, reason = check_file_permission(
                    path, agent.type.value, agent.sub_type,
                )
                if not allowed:
                    return f"[BLOCKED] {reason}"
        except Exception:
            pass
    target = _safe_path(path)
    if target.exists():
        return (
            f"[REJECTED] create_file on existing path {path!r}. "
            f"Use `patch_file` to edit existing files."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"[OK] Created {len(content)} bytes at {path}"


@tool
async def patch_file(path: str, patch_kind: str, payload: str) -> str:
    """Apply a patch to an existing file. Preferred over `write_file`
    for any edit (see docs/operations/patching.md).

    Args:
        path: Relative path from workspace root.
        patch_kind: "search_replace" or "unified_diff".
        payload: For "search_replace" — one or more blocks of the form
            ``<<<<<<< SEARCH\\n...\\n=======\\n...\\n>>>>>>> REPLACE``.
            SEARCH must carry ≥3 lines of context and match exactly once.
            For "unified_diff" — standard --- / +++ / @@ hunk format.
    """
    agent_id = get_active_agent_id()
    if agent_id:
        try:
            from backend.codeowners import check_file_permission
            from backend.routers.agents import _agents
            agent = _agents.get(agent_id)
            if agent:
                allowed, reason = check_file_permission(
                    path, agent.type.value, agent.sub_type,
                )
                if not allowed:
                    return f"[BLOCKED] {reason}"
        except Exception:
            pass
    target = _safe_path(path)
    if not target.is_file():
        return (
            f"[REJECTED] patch_file on missing path {path!r}. "
            f"Use `create_file` for new files."
        )
    try:
        from backend.agents.tools_patch import apply_to_file, PatchError
        apply_to_file(target, patch_kind, payload)
    except PatchError as exc:
        # Quality signal: patch failure is an agent mistake. Feed IIS.
        try:
            from backend import intelligence as _iis
            _iis.record_and_publish(
                agent_id or "unknown", code_pass=False,
            )
        except Exception:
            pass
        return f"[PATCH-FAILED] {type(exc).__name__}: {exc}"
    return f"[OK] Patched {path} ({patch_kind})"


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
    # Fix-A S3': exec with argv to avoid shell interpolation on `cmd`.
    import shlex
    from backend.agents._shell_safe import run_exec
    rc, out_raw, err_raw = await run_exec(
        ["git", *shlex.split(cmd)],
        cwd=work, env=env, timeout=BASH_TIMEOUT,
    )
    out = out_raw.strip()
    err = err_raw.strip()
    if rc != 0:
        return f"[GIT ERROR] {err or out}"
    return out or err or "[OK]"


async def _get_remote_url(remote: str = "origin", cwd: Path | None = None) -> str:
    """Get the URL of a git remote."""
    work = cwd or get_active_workspace()
    # Fix-A S3': exec with argv.
    from backend.agents._shell_safe import run_exec
    rc, out, _ = await run_exec(
        ["git", "remote", "get-url", remote], cwd=work, timeout=5,
    )
    return out.strip() if rc == 0 else ""


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
                # C2 audit: no longer pre-escape here — `exec_in_container`
                # now shlex.quote()s the command itself (proper single-
                # quote wrap), which defeats $(...)/backtick/$VAR escapes
                # that the old `replace('"', '\\"')` missed.
                rc, output = await exec_in_container(container.container_id, command)
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
    # Fix-A S3': exec with argv. The old shell fallback (|| git diff --root)
    # is now an explicit Python try/except so `target` cannot break out of
    # the shell context.
    from backend.agents._shell_safe import run_exec
    rc, out_raw, err_raw = await run_exec(
        ["git", "diff", f"{target}~1..{target}"],
        cwd=workspace, timeout=BASH_TIMEOUT,
    )
    if rc != 0:
        rc, out_raw, err_raw = await run_exec(
            ["git", "diff", "--root", target],
            cwd=workspace, timeout=BASH_TIMEOUT,
        )
    out = out_raw.strip()
    if rc != 0:
        return f"[ERROR] {err_raw.strip()}"
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
    # SP-3.2: worker context — acquire a pool-scoped conn just for this read.
    try:
        async with get_pool().acquire() as _conn:
            comments = await db.list_task_comments(_conn, task.id, limit=3)
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
    # SP-3.2: worker context — acquire pool conn just for this write.
    try:
        async with get_pool().acquire() as _conn:
            await db.insert_task_comment(_conn, comment)
    except Exception as exc:
        return f"[ERROR] Failed to save comment: {exc}"
    return f"[OK] Comment added to task {task_id} by {author}"


# The 9-value area whitelist the runner's capability_matrix recognises.
# An unknown area silently dead-ends in an infinite pre-pickup loop
# (feedback_runner_recognized_areas), so we validate hard here rather
# than let a typo reach JIRA.
_RUNNER_AREAS = {
    "backend", "db", "devops", "docs", "embedded",
    "frontend", "security", "tests", "tooling",
}
_JIRA_PRIORITIES = {"Highest", "High", "Medium", "Low", "Lowest"}


@tool
async def create_task(
    title: str,
    summary: str,
    area: str,
    acceptance_criteria: str = "",
    capabilities: str = "",
    priority: str = "",
    character: str = "",
    skill: str = "",
    tier: str = "",
) -> str:
    """File a JIRA Story from an understood user intent.

    This is the bridge from a chat conversation to real autonomous work:
    once the user has CONFIRMED what they want built, call this ONCE to
    create the Story. Do not call it to explore options or before the user
    agrees on scope.

    TWO MODES:

    * **Gated (default, no ``character``).** SAFETY — the Story carries every
      discipline label EXCEPT the ``class:*`` label the runner's PICKUP_JQL
      requires, plus ``requires:operator-approval``. Visible + complete but
      NOT auto-dispatched until a human releases it. Never tell the user work
      has started — only that the task was filed for approval.
    * **Dispatched to a character (``character`` given).** The RPG un-weld:
      when the user explicitly picks a character (nova/pixel/sage/rex), the
      Story is dispatched to that persona — ``character:<slug>`` + the derived
      ``class:<brain>`` (routes to that character's runner), tier capped at the
      character's ceiling, optional in-guild ``skill:``. It IS runner-pickable.
      Only pass ``character`` when the user names one; the Gerrit human +2 gate
      still guards every merge.

    Args:
        title: Short imperative title (e.g. "RK3588 板級健康檢查工具").
        summary: What to build and why, in the user's own framing.
        area: ONE of backend/db/devops/docs/embedded/frontend/security/
            tests/tooling — the runner capability area. Pick the closest.
        acceptance_criteria: How to know it's done. If omitted, a 4-AC
            skeleton (Code/Deploy/Integration/Exercised) is inserted for
            the operator to fill in.
        capabilities: Comma-separated extra runner capabilities to enable
            (e.g. "gerrit_push"). Most code tasks need none.
        priority: Highest/High/Medium/Low/Lowest. Blank = project default.
        character: OPTIONAL RPG character slug the user explicitly assigned
            the work to (nova/pixel/sage/rex). When set, dispatches to that
            character (see mode above); when blank, files gated.
        skill: OPTIONAL skill_id to train (only with ``character``); must be
            in the character's guild or it's rejected.
        tier: OPTIONAL S/M/L/X (only with ``character``); defaults to the
            character's ceiling and may not exceed it.
    """
    area = (area or "").strip().lower()
    if area not in _RUNNER_AREAS:
        return (
            f"[ERROR] area must be one of {sorted(_RUNNER_AREAS)} — got "
            f"{area!r}. Pick the closest runner capability area."
        )
    if not title.strip() or not summary.strip():
        return "[ERROR] title and summary are both required."

    character = (character or "").strip().lower()
    skill = (skill or "").strip()
    tier = (tier or "").strip().upper()
    char_def = None
    if character:
        from backend.agents import character_registry
        # RECRUIT C2: filing is for NEW work — validate against the DB-merged
        # ACTIVE roster (retired characters stay resolvable at runtime only).
        roster = character_registry.load_characters()
        char_def = roster.get(character)
        if char_def is None:
            return (
                f"[ERROR] unknown character {character!r} — known active: "
                f"{sorted(roster)}"
            )
        if not tier:
            # Default to M (the common chat-task size), but never above the
            # character's ceiling (so rex/S defaults to S, not M).
            tier = "M" if character_registry.tier_within_ceiling("M", char_def.max_tier) else char_def.max_tier
        if tier not in character_registry.TIER_ORDER:
            return f"[ERROR] tier must be one of S/M/L/X — got {tier!r}."
        if not character_registry.tier_within_ceiling(tier, char_def.max_tier):
            return (
                f"[ERROR] tier {tier} exceeds character {character}'s ceiling "
                f"{char_def.max_tier} — pick a lower tier or a stronger character."
            )
        if skill:
            try:
                from backend.agents.skill_matrix import load_skill_matrix
                from backend.sandbox_tier import Guild
                guild_skills = {
                    d.skill_id
                    for d in load_skill_matrix().get(Guild(char_def.guild), [])
                }
            except Exception:  # noqa: BLE001 — matrix optional; skip skill on failure
                guild_skills = set()
            if skill not in guild_skills:
                return (
                    f"[ERROR] skill {skill!r} is not in character {character}'s "
                    f"guild ({char_def.guild}); allowed: {sorted(guild_skills)}."
                )

    labels = ["agent:auto", "type:feature", f"area:{area}", "op:orchestrator-filed"]
    if char_def is not None:
        # Dispatch to the chosen character: character: + derived class: routes it
        # to that persona's runner; tier caps capability; optional in-guild skill.
        labels += [f"character:{character}", f"class:{char_def.brain}", f"tier:{tier}"]
        if skill:
            labels.append(f"skill:{skill}")
        if char_def.brain.startswith("subscription-"):
            labels.append("capability:enable=gerrit_push")
    else:
        # Gated (safe default): no class: → not runner-pickable until a human releases.
        labels.append("requires:operator-approval")
    for cap in (c.strip() for c in capabilities.split(",")):
        if cap and f"capability:enable={cap}" not in labels:
            labels.append(f"capability:enable={cap}")

    ac = acceptance_criteria.strip() or (
        "- [ ] Code: <implementation merged>\n"
        "- [ ] Deploy: <where it runs / how it ships>\n"
        "- [ ] Integration: <wired into the calling system>\n"
        "- [ ] Exercised: <proven end-to-end on real input>"
    )
    if char_def is not None:
        footer = (
            f"_Filed by the OmniSight orchestrator from a user chat session and "
            f"DISPATCHED to character {char_def.display_name} "
            f"({character}, {char_def.brain}, guild {char_def.guild}, tier {tier})._"
        )
    else:
        footer = (
            f"_Filed by the OmniSight orchestrator from a user chat session. "
            f"GATED: add a class:* label (e.g. class:subscription-claude) to "
            f"release it to the runner fleet._"
        )
    description = (
        f"{summary.strip()}\n\n"
        f"h3. Acceptance Criteria (4-AC)\n{ac}\n\n"
        f"----\n"
        f"{footer}"
    )
    tagged_title = f"[OP][{area}] {title.strip()}"

    # Cross-turn idempotency (audit r2 codex#2): if THIS session already filed a
    # Story with this exact title in the last N minutes, return it instead of
    # creating a duplicate (metered: a refresh / double-submit / "file that
    # again" must not multiply GATED tickets). Fail-OPEN — any dedup-check error
    # falls through to a normal create, never blocking a legitimate file.
    _ctx = get_chat_context()
    if _ctx and _ctx.get("session_id"):
        try:
            import os as _os
            import time as _t2
            from backend import db as _db2
            _window = float(_os.getenv("OMNISIGHT_ORCH_DEDUP_WINDOW_S", "600"))
            async with get_pool().acquire() as _c2:
                _dup = await _db2.find_recent_orchestrator_task_by_title(
                    _c2,
                    tenant_id=_ctx.get("tenant_id", "") or "",
                    session_id=_ctx.get("session_id", ""),
                    title=tagged_title,
                    since=_t2.time() - _window,
                )
            if _dup and _dup.get("ticket_key"):
                return (
                    f"[OK] Already filed as {_dup['ticket_key']} moments ago "
                    f"({_dup.get('browse_url') or 'no url'}) — not creating a "
                    f"duplicate. Tell the user it's already on the board."
                )
        except Exception as _dexc:  # noqa: BLE001 — fail open
            logger.debug("create_task dedup check skipped: %s", _dexc)

    try:
        from backend.jira_adapter import build_default_jira_adapter
        adapter = build_default_jira_adapter()
        ref = await adapter.create_story(
            summary=tagged_title,
            description=description,
            labels=labels,
            priority=priority if priority in _JIRA_PRIORITIES else "",
        )
    except Exception as exc:  # noqa: BLE001 — surface, never crash the graph
        return f"[ERROR] Failed to create Story: {exc}"

    # Gap C: record the user↔ticket link so the delivery poller can notify
    # the filer in their chat when the runner finishes. Best-effort — a
    # persistence failure must never turn a successful file into an error.
    ctx = get_chat_context()
    if ctx and ctx.get("user_id"):
        try:
            import time as _t
            from backend import db as _db
            async with get_pool().acquire() as _conn:
                await _db.upsert_orchestrator_task(_conn, {
                    "id": f"otask-{__import__('uuid').uuid4().hex[:12]}",
                    "tenant_id": ctx.get("tenant_id", ""),
                    "user_id": ctx["user_id"],
                    "session_id": ctx.get("session_id", ""),
                    "ticket_key": ref.ticket,
                    "title": tagged_title,
                    "area": area,
                    "browse_url": ref.url,
                    "filed_at": _t.time(),
                })
        except Exception as exc:  # noqa: BLE001
            logger.warning("create_task: user↔ticket link not persisted for %s: %s", ref.ticket, exc)

    if char_def is not None:
        return (
            f"[OK] Filed Story {ref.ticket} — {tagged_title}\n"
            f"  {ref.url}\n"
            f"  labels: {', '.join(labels)}\n"
            f"  ▶ DISPATCHED to {char_def.display_name} ({character}) — the "
            f"{char_def.brain} runner will pick it up. Merges still need a human +2.\n"
            f"  ticket={ref.ticket}"  # machine-parseable (audit r2 rank 4): use for link_blocks
        )
    return (
        f"[OK] Filed gated Story {ref.ticket} — {tagged_title}\n"
        f"  {ref.url}\n"
        f"  labels: {', '.join(labels)}\n"
        f"  ⚠ GATED — not yet dispatched to the runner. To release it, "
        f"add a class:subscription-claude (or -codex) label, or re-file with a character.\n"
        f"  ticket={ref.ticket}"  # machine-parseable (audit r2 rank 4): use for link_blocks
    )


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
#  7.5. Build artifact registration tool
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@tool
async def register_build_artifact(
    file_path: str,
    name: str = "",
    artifact_type: str = "",
    task_id: str = "",
    version: str = "",
) -> str:
    """Register a compiled binary or build output as a downloadable artifact.

    Call this after a successful build to make the output available for download.
    The file is copied from the workspace to the persistent .artifacts/ directory.

    Args:
        file_path: Path to the file (relative to workspace root).
        name: Display name for the artifact. Defaults to filename.
        artifact_type: Type override (binary, firmware, kernel_module, sdk, model, archive).
                      Auto-detected from extension if empty.
        task_id: Associated task ID for tracking.
        version: Semantic version string (e.g. "1.0.0-rc1").
    """
    import hashlib
    import shutil
    import uuid
    from datetime import datetime

    from backend import db
    from backend.routers.artifacts import get_artifacts_root
    from backend.workspace import _guess_artifact_type

    # Validate path
    try:
        src = _safe_path(file_path)
    except PermissionError:
        return f"[BLOCKED] Path escapes workspace: {file_path}"

    if not src.exists():
        return f"[ERROR] File not found: {file_path}"
    if not src.is_file():
        return f"[ERROR] Not a file: {file_path}"

    # Compute checksum
    sha = hashlib.sha256()
    with open(src, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    checksum = sha.hexdigest()

    # Copy to .artifacts/
    artifacts_root = get_artifacts_root()
    task_dir = artifacts_root / (task_id or "general")
    task_dir.mkdir(parents=True, exist_ok=True)
    dest = task_dir / src.name
    if dest.exists():
        dest = task_dir / f"{dest.stem}_{uuid.uuid4().hex[:4]}{dest.suffix}"
    shutil.copy2(src, dest)

    # Determine type
    atype = artifact_type or _guess_artifact_type(src.name)
    display_name = name or src.name
    artifact_id = f"art-{uuid.uuid4().hex[:12]}"

    artifact_data = {
        "id": artifact_id,
        "task_id": task_id,
        "agent_id": get_active_agent_id() or "",
        "name": display_name,
        "type": atype,
        "file_path": str(dest),
        "size": dest.stat().st_size,
        "created_at": datetime.now().isoformat(),
        "version": version,
        "checksum": checksum,
    }

    try:
        # SP-3.6a: tool runs in agent orchestrator worker context —
        # no request conn in scope, acquire from pool for this single
        # write. tenant_id is derived from the active request's
        # contextvar inside db.insert_artifact().
        async with get_pool().acquire() as _conn:
            await db.insert_artifact(_conn, artifact_data)
    except Exception as exc:
        # File already copied — clean up orphan on DB failure
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return f"[ERROR] Failed to register artifact in DB: {exc}"

    # Emit SSE event
    try:
        from backend.events import bus
        bus.publish("artifact_created", {
            "id": artifact_id, "name": display_name, "type": atype,
            "task_id": task_id, "agent_id": artifact_data["agent_id"],
            "size": artifact_data["size"],
        })
    except Exception:
        pass

    return (
        f"[OK] Artifact registered: {display_name}\n"
        f"  ID: {artifact_id}\n"
        f"  Type: {atype}\n"
        f"  Size: {artifact_data['size']} bytes\n"
        f"  SHA-256: {checksum[:16]}...\n"
        f"  Download: GET /artifacts/{artifact_id}/download"
    )


ARTIFACT_TOOLS = [register_build_artifact]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _default_platform_for_host() -> str:
    """Decide the platform profile to use when the workspace has no
    `.omnisight/platform` hint.

    Pre-T1-A this was a hardcoded `"aarch64"` — meaning an AMD 9950X
    dev box would cheerfully draft a DAG for arm64 cross-compile by
    default, then fail at toolchain resolution. That was a leftover
    from when the project's only supported target was a Rockchip
    devkit.

    Now: prefer the `host_native` profile (Phase 59) which already
    ships — it uses the system gcc, no QEMU, no cross-compile dance.
    Falls back to `aarch64` only if `host_native.yaml` is missing
    from configs/platforms/ (someone pruned it), so legacy users
    don't suddenly fail to resolve a platform.

    This helper is the pre-Phase-64-C-LOCAL seam that unblocks the
    host==target happy path end-to-end.
    """
    try:
        from backend.sdk_provisioner import _platform_profile
        profile = _platform_profile("host_native")
        if profile is not None and profile.exists():
            return "host_native"
    except Exception:
        pass
    return "aarch64"


@tool
async def get_platform_config(platform: str = "") -> str:
    """Get build parameters (ARCH, CROSS_COMPILE, sysroot, cmake) for a platform.

    Args:
        platform: Platform profile name (e.g. 'aarch64', 'host_native',
                  'vendor-example'). If empty, reads from the workspace's
                  `.omnisight/platform` hint; if that file is missing, falls
                  back to `_default_platform_for_host()` (Phase 59's
                  `host_native` when available) — so software projects on
                  an x86_64 dev box don't silently plan a cross-compile.
    """

    if not platform:
        ws = get_active_workspace()
        hint = ws / ".omnisight" / "platform"
        platform = (
            hint.read_text().strip() if hint.exists()
            else _default_platform_for_host()
        )

    # Validate platform name to prevent path traversal via attacker-controlled hint.
    from backend.sdk_provisioner import _validate_platform_name, _platform_profile
    if not _validate_platform_name(platform):
        return f"[ERROR] Invalid platform name: {platform!r}"
    profile = _platform_profile(platform)
    if profile is None or not profile.exists():
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
        if not Path(sysroot).is_dir():
            lines.append("SYSROOT_MISSING=true")
            sdk_url = data.get("sdk_git_url", "")
            hint = f" (run: /sdks install {platform})" if sdk_url else " (set sdk_git_url or install manually)"
            lines.append(f"# WARNING: sysroot not found at {sysroot}{hint}")
    cmake_tc = data.get("cmake_toolchain_file", "")
    if cmake_tc:
        lines.append(f"CMAKE_TOOLCHAIN_FILE={cmake_tc}")
        if not Path(cmake_tc).is_file():
            lines.append("CMAKE_TOOLCHAIN_MISSING=true")
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


async def _get_deploy_info(platform: str = "") -> dict[str, Any] | None:
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
            from backend.llm_adapter import SystemMessage, HumanMessage
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

    # SP-3.12: agent-tool search is a worker context — acquire pool
    # conn for the search call. The inner access-count UPDATEs ride
    # the same conn, so a single acquire covers both read + write.
    try:
        async with get_pool().acquire() as _conn:
            results = await db.search_episodic_memory(
                _conn,
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
        async with get_pool().acquire() as _conn:
            await db.insert_episodic_memory(_conn, {
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


# ⚠ save_solution (the model-callable L3 WRITE) is intentionally NOT bound
# here. Removed 2026-07-11 as U6-0 H0.5a — finishing the episodic containment
# H0 started on the Sora side (OP-2591 / Gerrit #2062). Every guild loadout
# (_ARCHITECT_TOOLS / _DESIGN_TOOLS / _GENERAL_TOOLS / _DEVOPS_TOOLS /
# _INTEL_TOOLS + siblings) concatenates EPISODIC_TOOLS, so a specialist could
# otherwise self-author a high-ranking cross-user episodic row: save_solution
# mints quality_score = 1.0 from a RAW model-supplied gerrit_change_id with NO
# server-side +2 verification, into a GLOBAL, tenant-less episodic_memory
# table read back into prompts (rag_prefetch / search_past_solutions) — the
# same forgeable-provenance memory-poisoning loop H0 closed on Sora.
#
# save_solution the FUNCTION stays defined and importable from
# backend.agents.tools (the direct-ainvoke tiered-memory tests still exercise
# it). The legitimate "remember a verified rescue" write is UNAFFECTED: it
# runs server-side on real Gerrit merge in
# webhooks._save_merged_solution_to_l3 via db.insert_episodic_memory. The
# READ (search_past_solutions) stays in every loadout that had it. Re-binding
# save_solution as a model-callable tool requires the U6-0 provenance gate +
# fail-closed action-capability guard first.
EPISODIC_TOOLS = [search_past_solutions]


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
    # SP-3.8 (2026-04-20): tools run in agent orchestrator worker
    # context — no request conn. Each DB op acquires briefly from the
    # pool. The simulation state transitions (running → error/parse
    # error/final) are independent writes, not a transaction, so per-
    # op acquires are correct: a slow subprocess.run between updates
    # doesn't pin a pool conn.
    try:
        async with get_pool().acquire() as _conn:
            await db.insert_simulation(_conn, {
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
    # Fix-A S3': keep argv list for exec-based path; `cmd` kept for the
    # in-container legacy exec_in_container() signature which expects str.
    cmd_argv = cmd_parts
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
                # Fix-A S3': fallback to host via exec + argv (no shell).
                from backend.agents._shell_safe import run_exec
                workspace = get_active_workspace()
                rc, stdout_s, stderr_s = await run_exec(
                    cmd_argv, cwd=workspace, timeout=SIMULATION_TIMEOUT,
                )
                raw_output = stdout_s if stdout_s.strip() else stderr_s
        else:
            from backend.agents._shell_safe import run_exec
            workspace = get_active_workspace()
            rc, stdout_s, stderr_s = await run_exec(
                cmd_argv, cwd=workspace, timeout=SIMULATION_TIMEOUT,
            )
            raw_output = stdout_s if stdout_s.strip() else stderr_s
    except asyncio.TimeoutError:
        async with get_pool().acquire() as _conn:
            await db.update_simulation(_conn, sim_id, {
                "status": "error",
                "report_json": '{"errors":["Timeout"]}',
            })
        emit_simulation(sim_id, "result", "Timeout", status="error")
        return f"[TIMEOUT] Simulation {sim_id} timed out after {SIMULATION_TIMEOUT}s"

    # Parse JSON report from stdout
    report = {}
    try:
        report = _json.loads(raw_output.strip())
    except (ValueError, _json.JSONDecodeError):
        async with get_pool().acquire() as _conn:
            await db.update_simulation(_conn, sim_id, {
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
    async with get_pool().acquire() as _conn:
        await db.update_simulation(_conn, sim_id, update_data)

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
#  12. MCP (Model Context Protocol) tools — external skill catalogues
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Path to the OmniSight MCP server registry. Not routed through
# `get_active_workspace()` because the registry lives with project config,
# not inside agent-isolated workspaces.
_MCP_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "configs" / "mcp_servers.json"
_MCP_CALL_TIMEOUT = int(os.environ.get("OMNISIGHT_MCP_CALL_TIMEOUT", "60"))
_MCP_PROTOCOL_VERSION = "2024-11-05"


def _load_mcp_server_spec(name: str) -> dict[str, Any] | None:
    """Read the MCP registry JSON and return the named server spec, or None."""
    import json as _json
    if not _MCP_REGISTRY_PATH.is_file():
        return None
    try:
        data = _json.loads(_MCP_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    servers = data.get("mcpServers") or {}
    spec = servers.get(name)
    if not isinstance(spec, dict):
        return None
    return spec


async def _call_mcp_tool(
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout: int = _MCP_CALL_TIMEOUT,
) -> tuple[bool, str]:
    """Invoke a single MCP tool over stdio JSON-RPC. Returns (ok, text).

    Spawns the server subprocess from the spec in `configs/mcp_servers.json`,
    performs the MCP `initialize` → `notifications/initialized` → `tools/call`
    handshake, and returns the flattened text content of the result.

    Module-global state audit (SOP Step 1): none — each invocation is a
    self-contained subprocess + local asyncio.StreamReader/Writer pair; no
    module-level caches, pools, or singletons. Safe under
    `uvicorn --workers N`: every worker spawns its own subprocess.

    Why subprocess-per-call rather than a persistent connection: MCP servers
    are intentionally cheap to start (stdio + JSON-RPC); holding a persistent
    Node process per worker would complicate shutdown/cleanup with no latency
    win for a tool the agent calls on-demand, not in hot path.
    """
    import json as _json

    spec = _load_mcp_server_spec(server_name)
    if spec is None:
        return False, f"MCP server {server_name!r} not found in {_MCP_REGISTRY_PATH}"
    command = spec.get("command")
    args = list(spec.get("args") or [])
    env_overrides = spec.get("env") or {}
    if not command:
        return False, f"MCP server {server_name!r} missing 'command' in registry"

    env = os.environ.copy()
    env.update({k: str(v) for k, v in env_overrides.items()})

    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        return False, (
            f"MCP launcher {command!r} not found on PATH — install Node/npm "
            f"or adjust configs/mcp_servers.json for {server_name!r}"
        )
    except Exception as exc:
        return False, f"Failed to spawn MCP server {server_name!r}: {exc}"

    async def _send(payload: dict[str, Any]) -> None:
        line = (_json.dumps(payload) + "\n").encode("utf-8")
        proc.stdin.write(line)
        await proc.stdin.drain()

    async def _recv_response(req_id: int) -> dict[str, Any]:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                raise RuntimeError("MCP server closed stdout before responding")
            try:
                msg = _json.loads(raw.decode("utf-8").strip())
            except Exception:
                continue
            if msg.get("id") == req_id:
                return msg

    try:
        async def _do_call() -> dict[str, Any]:
            await _send({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "omnisight", "version": "1.0"},
                },
            })
            init_resp = await _recv_response(1)
            if "error" in init_resp:
                raise RuntimeError(f"initialize failed: {init_resp['error']}")
            await _send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            await _send({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            })
            return await _recv_response(2)

        response = await asyncio.wait_for(_do_call(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"MCP call timed out after {timeout}s ({server_name}/{tool_name})"
    except Exception as exc:
        proc.kill()
        await proc.wait()
        return False, f"MCP call failed ({server_name}/{tool_name}): {exc}"
    finally:
        if proc.returncode is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    if "error" in response:
        err = response["error"]
        return False, f"MCP error: {err.get('message', err)}"

    result = response.get("result") or {}
    if result.get("isError"):
        pieces = [
            item.get("text", "") for item in (result.get("content") or [])
            if item.get("type") == "text"
        ]
        return False, "MCP tool reported error: " + "\n".join(pieces).strip()

    content = result.get("content") or []
    texts = [item.get("text", "") for item in content if item.get("type") == "text"]
    return True, "\n".join(t for t in texts if t).strip()


@tool
async def android_skill_search(
    action: str = "search",
    query: str = "",
    skill_id: str = "",
    limit: int = 5,
) -> str:
    """Search or fetch Android Skills via the android-skills MCP server.

    Wraps the official skydoves/android-skills-mcp server (registered in
    `configs/mcp_servers.json`), which exposes Google's Android skill
    catalogue (Navigation 3 setup, AGP 9 migration, R8 config, ...). Use
    this before writing Kotlin/Compose/Gradle to pull authoritative
    best-practice guidance into the agent's context window.

    Args:
        action: "search" to find skills matching `query`,
                "list" to enumerate all available skills,
                "get" to retrieve the full body of a single skill.
        query: Free-text query for `action="search"` (e.g. "navigation3",
               "agp 9 migration"). Ignored for list/get.
        skill_id: Required for `action="get"`. The skill identifier (as
                  returned by `list`/`search`).
        limit: Max results to surface for `action="search"` (1–20).
    """
    action = (action or "search").strip().lower()
    if action not in {"search", "list", "get"}:
        return (
            f"[ERROR] android_skill_search: unknown action {action!r}. "
            f"Use 'search', 'list', or 'get'."
        )

    if action == "search":
        if not query.strip():
            return "[ERROR] android_skill_search: 'query' is required for action='search'."
        safe_limit = max(1, min(int(limit) if limit else 5, 20))
        tool_name = "search_skills"
        arguments: dict = {"query": query.strip(), "limit": safe_limit}
    elif action == "list":
        tool_name = "list_skills"
        arguments = {}
    else:  # "get"
        if not skill_id.strip():
            return "[ERROR] android_skill_search: 'skill_id' is required for action='get'."
        tool_name = "get_skill"
        arguments = {"skill_id": skill_id.strip()}

    ok, payload = await _call_mcp_tool("android-skills", tool_name, arguments)
    if not ok:
        return f"[ERROR] android-skills MCP: {payload}"
    if not payload:
        return f"[OK] android-skills/{tool_name}: (empty response)"

    max_chars = 20_000
    if len(payload) > max_chars:
        payload = payload[:max_chars] + f"\n... [truncated, {len(payload) - max_chars} more chars]"
    return f"[OK] android-skills/{tool_name}\n{payload}"


MCP_TOOLS = [android_skill_search]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  13. Web search tool (BP.N.4)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _web_search_host_allowed(
    url: str,
    *,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if not host:
        return True

    def matches(domain: str) -> bool:
        d = domain.strip().lower().removeprefix("www.")
        return bool(d) and (host == d or host.endswith(f".{d}"))

    blocked = [str(d) for d in (blocked_domains or []) if str(d).strip()]
    if any(matches(domain) for domain in blocked):
        return False

    allowed = [str(d) for d in (allowed_domains or []) if str(d).strip()]
    if allowed and not any(matches(domain) for domain in allowed):
        return False
    return True


async def _audit_web_search_query(
    *,
    query: str,
    provider: str,
    status: str,
    allowed_domains: list[str] | None,
    blocked_domains: list[str] | None,
    result_count: int | None = None,
    cost_usd_estimated: float | None = None,
    error: str | None = None,
    tenant_id: str | None = None,
    request_id: str | None = None,
) -> None:
    """Best-effort BP.N.5 trace for each WebSearch tool query."""
    from backend import audit as _audit
    from backend.db_context import current_tenant_id

    after = {
        "query": query,
        "provider": provider,
        "status": status,
        "tenant_id": tenant_id or current_tenant_id() or "t-default",
        "allowed_domains": list(allowed_domains or []),
        "blocked_domains": list(blocked_domains or []),
    }
    if result_count is not None:
        after["result_count"] = int(result_count)
    if cost_usd_estimated is not None:
        after["cost_usd_estimated"] = float(cost_usd_estimated)
    if request_id:
        after["request_id"] = request_id
    if error:
        after["error"] = error

    await _audit.log(
        action="web_search.query",
        entity_kind="web_search_query",
        entity_id=hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
        before=None,
        after=after,
        actor=f"agent:{get_active_agent_id() or 'unknown'}",
    )


@tool("WebSearch")
async def web_search(
    query: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> str:
    """Search the web through the configured BP.N provider.

    Module-global state audit: this tool has no process-local mutable state;
    every worker derives the same loadout from the module-constant
    ``AGENT_TOOLS`` mapping while rate/cost coordination stays inside
    ``backend.web_search``.

    Args:
        query: Search query.
        allowed_domains: Optional domain allow-list.
        blocked_domains: Optional domain block-list.
    """
    cleaned_query = str(query or "").strip()
    if not cleaned_query:
        return "[ERROR] WebSearch: query is required."

    from backend.config import settings
    from backend.db_context import current_tenant_id
    from backend.web_sanitizer import sanitize_web_content
    from backend.web_search import (
        WebSearchBudgetExceeded,
        WebSearchCredentialMissing,
        WebSearchRateLimited,
        make_web_search_client,
    )

    try:
        client = make_web_search_client(settings=settings)
    except Exception as exc:
        await _audit_web_search_query(
            query=cleaned_query,
            provider="unknown",
            status="config_error",
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            error=f"{type(exc).__name__}: {exc}",
        )
        return f"[ERROR] WebSearch: failed to configure provider: {type(exc).__name__}: {exc}"
    if client is None:
        await _audit_web_search_query(
            query=cleaned_query,
            provider="none",
            status="disabled",
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )
        return "[DISABLED] WebSearch: OMNISIGHT_WEB_SEARCH_PROVIDER=none."

    tenant_id = current_tenant_id() or "t-default"
    try:
        response = await asyncio.to_thread(
            client.search,
            cleaned_query,
            tenant_id=tenant_id,
            max_results=5,
            include_answer=True,
            audit=False,
        )
    except WebSearchRateLimited as exc:
        await _audit_web_search_query(
            query=cleaned_query,
            provider="tavily",
            status="rate_limited",
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            error=str(exc),
            tenant_id=tenant_id,
        )
        return f"[BLOCKED] WebSearch: {exc}"
    except WebSearchBudgetExceeded as exc:
        await _audit_web_search_query(
            query=cleaned_query,
            provider="tavily",
            status="budget_exceeded",
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            error=str(exc),
            tenant_id=tenant_id,
        )
        return f"[BLOCKED] WebSearch: {exc}"
    except WebSearchCredentialMissing as exc:
        await _audit_web_search_query(
            query=cleaned_query,
            provider="tavily",
            status="credential_missing",
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            error=str(exc),
            tenant_id=tenant_id,
        )
        return f"[ERROR] WebSearch: {exc}"

    if response.error:
        await _audit_web_search_query(
            query=cleaned_query,
            provider=response.provider,
            status="provider_error",
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            cost_usd_estimated=response.cost_usd_estimated,
            error=response.error,
            tenant_id=response.tenant_id,
            request_id=response.request_id,
        )
        return f"[ERROR] WebSearch: {response.error}"

    filtered = [
        result
        for result in response.results
        if _web_search_host_allowed(
            result.url,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
        )
    ]

    await _audit_web_search_query(
        query=cleaned_query,
        provider=response.provider,
        status="ok",
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
        result_count=len(filtered),
        cost_usd_estimated=response.cost_usd_estimated,
        tenant_id=response.tenant_id,
        request_id=response.request_id,
    )

    lines = [
        f"[OK] WebSearch: provider={response.provider} results={len(filtered)} query={response.query!r}",
        f"Cost estimate: ${response.cost_usd_estimated:.4f} · fetched_at={response.fetched_at}",
    ]
    if response.answer:
        lines.append("")
        lines.append("Answer:")
        lines.append(sanitize_web_content(response.answer).sanitized_text)
    for index, result in enumerate(filtered, 1):
        lines.append("")
        lines.append(f"{index}. {result.title}")
        lines.append(f"URL: {result.url}")
        if result.published_date:
            lines.append(f"Published: {result.published_date}")
        if result.content:
            lines.append(sanitize_web_content(result.content, source_url=result.url).sanitized_text)
    if response.results and not filtered:
        lines.append("")
        lines.append("No results remained after domain filtering.")
    return "\n".join(lines)


WEB_SEARCH_TOOLS = [web_search]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  14. Image generation tool (V9 #325 row 2708)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Cap on how long a single image-gen API call may take. Image APIs are
# synchronous from the caller's perspective; 120s matches SIMULATION_TIMEOUT.
IMAGE_GEN_TIMEOUT = int(os.environ.get("OMNISIGHT_IMAGE_GEN_TIMEOUT", "120"))

# Allowed size strings — narrow regex prevents weird inputs that the
# OpenAI API would reject anyway, and gives a clean error message.
_IMAGE_SIZE_RE = re.compile(r"^[1-9]\d{1,4}x[1-9]\d{1,4}$")
_IMAGE_EXT_OK = (".png", ".jpg", ".jpeg", ".webp")


@tool
async def image_generate(
    prompt: str,
    output_path: str = "",
    size: str = "1024x1024",
    provider: str = "openai",
    model: str = "",
    register_artifact: bool = True,
    task_id: str = "",
) -> str:
    """Generate an image from a text prompt and save it into the workspace.

    Calls OpenAI's Images API (default: ``gpt-image-1``). The returned
    bytes are written to ``output_path`` inside the active workspace and
    — when ``register_artifact=True`` — also copied to the artifact store
    so the workspace preview pane can render it via the existing
    ``GET /artifacts/{id}/download`` route.

    The agent should call this in coding flows that need an icon, banner,
    placeholder hero image, etc. Prefer concrete, art-direction-style
    prompts ("flat icon, single-color rocket, transparent bg, 256px") —
    they survive the round-trip far better than vague ones.

    Args:
        prompt: Description of the image to generate.
        output_path: Workspace-relative path. Defaults to
            ``public/generated/<sha8>.png`` so Vite/Next dev servers
            pick it up via the public/ static-asset convention.
        size: Image dimensions, e.g. ``1024x1024`` (default), ``1024x1536``,
              ``1536x1024``, ``512x512``.
        provider: ``"openai"`` (default). ``"anthropic"`` returns an
            explicit error — Anthropic does not currently expose a hosted
            image-generation endpoint (only image *input* / vision).
        model: Override model id. Default: ``gpt-image-1`` for openai.
        register_artifact: If True (default), also register as a
            downloadable artifact (``image`` type) so the preview pane
            can ``<img src="/artifacts/<id>/download" />`` it.
        task_id: Optional task id for artifact attribution.
    """
    import base64 as _base64
    import hashlib as _hashlib
    import uuid as _uuid
    from datetime import datetime as _dt

    provider = (provider or "openai").strip().lower()
    if provider not in {"openai", "anthropic"}:
        return (
            f"[ERROR] image_generate: unknown provider {provider!r}. "
            f"Use 'openai' or 'anthropic'."
        )
    if provider == "anthropic":
        return (
            "[ERROR] image_generate: Anthropic does not expose a hosted "
            "image-generation API (only vision input). Re-call with "
            "provider='openai' (default)."
        )

    if os.environ.get("OMNISIGHT_IMAGE_GEN_DISABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return "[BLOCKED] image_generate is disabled (OMNISIGHT_IMAGE_GEN_DISABLED is set)."

    if not prompt or not prompt.strip():
        return "[ERROR] image_generate: prompt is required."
    if len(prompt) > 4000:
        return "[ERROR] image_generate: prompt must be ≤ 4000 chars."

    if not _IMAGE_SIZE_RE.match(size):
        return f"[ERROR] image_generate: invalid size {size!r}. Use e.g. '1024x1024'."

    model = (model or "").strip() or "gpt-image-1"

    prompt_hash = _hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    if not output_path:
        output_path = f"public/generated/{prompt_hash}.png"
    elif not output_path.lower().endswith(_IMAGE_EXT_OK):
        output_path = output_path.rstrip("/") + f"/{prompt_hash}.png"

    try:
        target = _safe_path(output_path)
    except PermissionError as exc:
        return f"[BLOCKED] {exc}"

    # CODEOWNERS check (best-effort, like write_file).
    agent_id = get_active_agent_id()
    if agent_id:
        try:
            from backend.codeowners import check_file_permission
            from backend.routers.agents import _agents
            agent = _agents.get(agent_id)
            if agent:
                allowed, reason = check_file_permission(
                    output_path, agent.type.value, agent.sub_type,
                )
                if not allowed:
                    return f"[BLOCKED] {reason}"
        except Exception:
            pass

    # Resolve the OpenAI API key via the standard credential chain.
    try:
        from backend.llm_credential_resolver import (
            get_llm_credential,
        )
        cred = await get_llm_credential("openai")
    except Exception as exc:  # LLMCredentialMissingError or import/resolve issue
        cls = type(exc).__name__
        return f"[ERROR] image_generate: failed to resolve OpenAI credential ({cls}): {exc}"
    if not cred.api_key:
        return "[ERROR] image_generate: OpenAI API key is empty."

    # Lazy import keeps the openai SDK out of the import-time graph and
    # lets tests monkey-patch `sys.modules['openai']` cleanly.
    try:
        from openai import AsyncOpenAI  # type: ignore
    except ImportError:
        return "[ERROR] image_generate: 'openai' SDK not installed in this image."

    client = AsyncOpenAI(api_key=cred.api_key)

    try:
        resp = await asyncio.wait_for(
            client.images.generate(
                model=model,
                prompt=prompt,
                size=size,
                n=1,
            ),
            timeout=IMAGE_GEN_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return f"[TIMEOUT] image_generate: OpenAI image API call exceeded {IMAGE_GEN_TIMEOUT}s."
    except Exception as exc:
        # Don't leak the API key — `cred.api_key` is only in the local client.
        return f"[ERROR] image_generate: OpenAI call failed: {type(exc).__name__}: {exc}"

    data = getattr(resp, "data", None) or []
    if not data:
        return "[ERROR] image_generate: empty response from OpenAI."
    first = data[0]
    b64 = getattr(first, "b64_json", None)
    if b64 is None and isinstance(first, dict):
        b64 = first.get("b64_json")
    url = getattr(first, "url", None)
    if url is None and isinstance(first, dict):
        url = first.get("url")

    image_bytes: bytes | None = None
    if b64:
        try:
            image_bytes = _base64.b64decode(b64)
        except Exception as exc:
            return f"[ERROR] image_generate: failed to decode b64 data: {exc}"
    elif url:
        if not isinstance(url, str) or not url.startswith("https://"):
            return "[ERROR] image_generate: refusing non-HTTPS URL from provider."
        try:
            import urllib.request as _urlreq
            loop = asyncio.get_event_loop()
            image_bytes = await loop.run_in_executor(
                None,
                lambda: _urlreq.urlopen(url, timeout=30).read(),  # noqa: S310 (https-only above)
            )
        except Exception as exc:
            return f"[ERROR] image_generate: failed to download URL: {type(exc).__name__}: {exc}"
    if not image_bytes:
        return "[ERROR] image_generate: no image data in response."
    if len(image_bytes) > 25 * 1024 * 1024:  # 25 MB sanity ceiling
        return f"[ERROR] image_generate: returned image is too large ({len(image_bytes)} bytes)."

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(image_bytes)
    size_bytes = len(image_bytes)

    # Best-effort artifact registration so the preview pane can fetch
    # the image via /artifacts/<id>/download. Failures here are
    # non-fatal — the file already lives in the workspace.
    artifact_id = ""
    artifact_url = ""
    if register_artifact:
        try:
            import shutil as _shutil
            from backend import db as _db
            from backend.routers.artifacts import get_artifacts_root

            artifacts_root = get_artifacts_root()
            task_dir = artifacts_root / (task_id or "general")
            task_dir.mkdir(parents=True, exist_ok=True)
            dest = task_dir / target.name
            if dest.exists():
                dest = task_dir / f"{dest.stem}_{_uuid.uuid4().hex[:4]}{dest.suffix}"
            _shutil.copy2(target, dest)
            checksum = _hashlib.sha256(image_bytes).hexdigest()
            artifact_id = f"art-{_uuid.uuid4().hex[:12]}"
            artifact_data = {
                "id": artifact_id,
                "task_id": task_id,
                "agent_id": agent_id or "",
                "name": target.name,
                "type": "image",
                "file_path": str(dest),
                "size": size_bytes,
                "created_at": _dt.now().isoformat(),
                "version": "",
                "checksum": checksum,
            }
            try:
                async with get_pool().acquire() as _conn:
                    await _db.insert_artifact(_conn, artifact_data)
                artifact_url = f"/artifacts/{artifact_id}/download"
            except Exception as db_exc:
                logger.info(
                    "image_generate: artifact DB registration skipped (%s) — "
                    "file still saved at %s",
                    type(db_exc).__name__, output_path,
                )
                try:
                    dest.unlink(missing_ok=True)
                except Exception:
                    pass
                artifact_id = ""
        except Exception as exc:
            logger.info(
                "image_generate: artifact registration failed (%s) — non-fatal",
                type(exc).__name__,
            )
            artifact_id = ""

    # SSE — let the preview pane subscribe to `image_generated` and
    # render the new asset without polling the artifact list.
    try:
        from backend.events import bus
        bus.publish("image_generated", {
            "agent_id": agent_id or "",
            "task_id": task_id,
            "prompt": prompt[:200],
            "path": output_path,
            "size_bytes": size_bytes,
            "artifact_id": artifact_id,
            "artifact_url": artifact_url,
            "model": model,
            "provider": "openai",
            "image_size": size,
        })
    except Exception:
        pass

    lines = [
        f"[OK] image_generate: saved {size_bytes} bytes to {output_path}",
        f"  Provider: openai · Model: {model} · Size: {size}",
    ]
    if artifact_id:
        lines.append(f"  Artifact: {artifact_id} (preview: {artifact_url})")
    return "\n".join(lines)


IMAGE_TOOLS = [image_generate]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tool registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FILE_TOOLS = [read_file, write_file, list_directory, read_yaml, write_yaml, search_in_files]
GIT_TOOLS = [git_status, git_log, git_diff, git_diff_staged, git_branch, git_add, git_commit, git_checkout_branch, git_push, git_remote_list, create_pr, git_add_remote]
BASH_TOOLS = [run_bash]
REVIEW_TOOLS = [gerrit_get_diff, gerrit_post_comment, gerrit_submit_review]
# ── Sora supervisor observability (P1, docs/design/rpg/sora-supervisor-roadmap.md) ──
# Read-only "sight" tools bound to the orchestrator chat so Sora can see fleet
# health in-conversation. All fail-open (absent telemetry → a friendly note,
# never an exception) and touch NO worker/prod state.


@tool
async def supervisor_quota_status() -> str:
    """(Sora supervisor) Provider LLM quota + circuit-breaker health. Read-only.

    Returns each provider's 5h / weekly token counters and circuit state
    (closed=healthy, open=tripped). Use when asked about provider capacity,
    rate limits, or "are we throttled".
    """
    try:
        async with get_pool().acquire() as conn:
            rows = await conn.fetch(
                "SELECT provider, rolling_5h_tokens, weekly_tokens, circuit_state "
                "FROM provider_quota_state ORDER BY provider"
            )
    except Exception as exc:  # noqa: BLE001
        return f"[FAILED] quota state unavailable: {exc}"
    if not rows:
        return "[SUPERVISOR] no provider quota state recorded."
    lines = ["[SUPERVISOR] provider quota / circuit:"]
    for r in rows:
        lines.append(
            f"  {r['provider']}: circuit={r['circuit_state']} "
            f"5h={r['rolling_5h_tokens']} weekly={r['weekly_tokens']}"
        )
    return "\n".join(lines)


@tool
async def supervisor_recent_incidents(days: int = 7) -> str:
    """(Sora supervisor) Recent runner incidents grouped by failure class. Read-only.

    Args:
        days: Lookback window in days (default 7).
    Use when asked what's failing / breaking, or to diagnose fleet health.
    """
    try:
        d = max(1, min(int(days), 90))
        async with get_pool().acquire() as conn:
            total = await conn.fetchval(
                "SELECT count(*) FROM runner_incidents "
                "WHERE created_at > now() - make_interval(days => $1)", d,
            )
            rows = await conn.fetch(
                "SELECT failure_class, count(*) AS n FROM runner_incidents "
                "WHERE created_at > now() - make_interval(days => $1) "
                "GROUP BY failure_class ORDER BY n DESC LIMIT 8", d,
            )
    except Exception as exc:  # noqa: BLE001
        return f"[FAILED] incident data unavailable: {exc}"
    if not total:
        return f"[SUPERVISOR] no runner incidents in the last {d} day(s)."
    lines = [f"[SUPERVISOR] {total} incident(s) in the last {d} day(s):"]
    for r in rows:
        lines.append(f"  {r['failure_class'] or 'UNKNOWN'}: {r['n']}")
    return "\n".join(lines)


@tool
async def supervisor_delivery_summary() -> str:
    """(Sora supervisor) Successful runner deliveries by brain + latest. Read-only.

    Use when asked how much the team has shipped, throughput, or per-brain
    delivery counts.
    """
    try:
        async with get_pool().acquire() as conn:
            rows = await conn.fetch(
                "SELECT agent_class, count(*) AS n FROM runner_metrics "
                "WHERE outcome = 'success' GROUP BY agent_class ORDER BY n DESC"
            )
            latest = await conn.fetchrow(
                "SELECT ticket_key, completed_at FROM runner_metrics "
                "WHERE outcome = 'success' ORDER BY completed_at DESC NULLS LAST LIMIT 1"
            )
    except Exception as exc:  # noqa: BLE001
        return f"[FAILED] delivery data unavailable: {exc}"
    total = sum(int(r["n"]) for r in rows)
    if not total:
        return "[SUPERVISOR] no successful deliveries recorded yet."
    parts = ", ".join(f"{r['agent_class']}={r['n']}" for r in rows)
    tail = f" · latest {latest['ticket_key']}" if latest and latest["ticket_key"] else ""
    return f"[SUPERVISOR] {total} successful deliveries ({parts}){tail}"


@tool
async def supervisor_ticket_detail(ticket_key: str) -> str:
    """(Sora supervisor) Read one OP-* ticket's full state — status, assignee,
    labels, Blocks/blocked-by links, and the latest comment — so Sora can SEE a
    ticket before deciding to act. Read-only; fails open with a "[SUPERVISOR] ..."
    note on any error.

    Args:
        ticket_key: e.g. "OP-2530" (only OP-NNN accepted).
    """
    key = (ticket_key or "").strip().upper()
    if not _SUP_TICKET_RE.match(key):
        return f"[SUPERVISOR] refused: '{ticket_key}' is not an OP-NNN ticket key."
    try:
        from backend.jira_adapter import build_default_jira_adapter
        adapter = build_default_jira_adapter()
        st, body = await adapter._api(
            "GET",
            f"/rest/api/2/issue/{key}"
            "?fields=status,assignee,labels,issuelinks,summary",
        )
        if not (200 <= st < 300) or not isinstance(body, dict):
            return f"[FAILED] {key}: fetch returned HTTP {st}."
        fields = body.get("fields") or {}
        summary = fields.get("summary") or "(no summary)"
        status = ((fields.get("status") or {}).get("name")) or "?"
        assignee_obj = fields.get("assignee")
        assignee = (
            (assignee_obj.get("displayName") or assignee_obj.get("name") or "?")
            if isinstance(assignee_obj, dict) else "unassigned"
        )
        labels = list(fields.get("labels") or [])
        blocks, blocked_by = [], []
        for lk in fields.get("issuelinks") or []:
            if not isinstance(lk, dict):
                continue
            if (lk.get("type") or {}).get("name") != "Blocks":
                continue
            out = lk.get("outwardIssue")
            inw = lk.get("inwardIssue")
            if isinstance(out, dict) and out.get("key"):
                blocks.append(out["key"])  # this ticket blocks -> outward
            if isinstance(inw, dict) and inw.get("key"):
                blocked_by.append(inw["key"])  # this ticket blocked by -> inward
        # Latest comment.
        last_comment = "none"
        cst, cbody = await adapter._api(
            "GET", f"/rest/api/2/issue/{key}/comment",
        )
        if 200 <= cst < 300 and isinstance(cbody, dict):
            comments = cbody.get("comments") or []
            if comments:
                c = comments[-1]
                author = (
                    (c.get("author") or {}).get("displayName")
                    or (c.get("author") or {}).get("name") or "?"
                )
                snippet = " ".join((c.get("body") or "").split())[:160]
                last_comment = f"{author}: {snippet}"
        lines = [
            f"[SUPERVISOR] {key} — {summary}",
            f"  status: {status} | assignee: {assignee}",
            f"  labels: {labels or '[]'}",
            f"  blocks: {blocks or '[]'} | blocked by: {blocked_by or '[]'}",
            f"  last comment: {last_comment}",
        ]
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        return f"[FAILED] {key}: detail unavailable: {exc}"


@tool
async def supervisor_guild_capabilities() -> str:
    """(Sora supervisor) List the worker guilds and what each does, so Sora can
    pick the right guild when filing a task. Read-only; fails open.
    """
    try:
        from backend.a2a.agent_card import (
            _GUILD_DISPLAY_NAMES,
            _GUILD_DESCRIPTIONS,
        )
        lines = ["[SUPERVISOR] worker guilds:"]
        for guild, desc in _GUILD_DESCRIPTIONS.items():
            name = _GUILD_DISPLAY_NAMES.get(guild, getattr(guild, "value", guild))
            lines.append(f"  - {name}: {desc}")
        if len(lines) > 1:
            return "\n".join(lines)
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.agents.guild_registry import GUILDS
        names = ", ".join(sorted(GUILDS))
        return f"[SUPERVISOR] worker guilds: {names}"
    except Exception as exc:  # noqa: BLE001
        return f"[FAILED] guild list unavailable: {exc}"


@tool
async def supervisor_release_status() -> str:
    """(Sora supervisor / P5-observe) READ-ONLY view of the prod deploy state: the
    currently-deployed version (tag / git-sha / promotion-audit-id) plus any dangerous-action
    proposals awaiting approval. Use to answer "what's on prod / is anything
    pending approval". Pure read — NO side effects, NEVER triggers a deploy.

    Sourced from the deploy-overlay lock (the backend's own deployed identity,
    a cheap file read) + the proposed_actions table via the async pool — NOT the
    release_dashboard sync readers, which do async DB I/O through a sync engine
    and raise MissingGreenlet in this async context (audit r3 watch-tool fix).
    """
    try:
        lines = ["[SUPERVISOR] prod deploy state (read-only):"]
        # 1) currently-deployed version. The AUTHORITATIVE source is the actually-
        # running image digest (works even when the deploy-overlay lock isn't
        # mounted, as on prod). The overlay lock, WHEN present, adds the tag /
        # git-sha / promotion-audit + a drift check; when absent we still report
        # the running digest instead of a useless "unknown".
        try:
            from backend import api_versioning
            try:
                running = api_versioning.get_running_image_digest_backend()
            except Exception:  # noqa: BLE001
                running = None
            ov = api_versioning.get_deploy_overlay() or {}
            if ov:
                tag = ov.get("deployed_tag") or "unknown"
                sha = (ov.get("build_git_sha") or "")[:12] or "?"
                audit = ov.get("promotion_audit_id") or "?"
                lines.append(f"  deployed (per lock): tag={tag} sha={sha} audit={audit}")
                locked = ov.get("deployed_digest_backend")
                if running and locked and running != locked:
                    lines.append("  ⚠ running backend image != deploy lock (DEPLOY DRIFT — verify)")
            elif running:
                lines.append(f"  running backend image: {running[:23]}… "
                             f"(no deploy-overlay lock; the running digest is authoritative)")
            else:
                lines.append("  deployed version: unavailable (no overlay lock + no running digest)")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  deployed: (version unavailable: {exc})")
        # 2) dangerous-action proposals awaiting approval (async pool)
        try:
            from backend import db as _db
            async with get_pool().acquire() as conn:
                pend = await _db.list_proposed_actions(conn, status="pending", limit=10)
            if pend:
                lines.append(f"  ⏳ {len(pend)} action proposal(s) AWAITING APPROVAL:")
                for p in pend:
                    lines.append(f"    • {p.get('id')} — {p.get('title')} (by {p.get('proposed_by')})")
            else:
                lines.append("  no action proposals awaiting approval.")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  pending proposals: (unavailable: {exc})")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        return f"[FAILED] release status unavailable: {exc}"


# Bound to Sora's chat (nodes.conversation_node): read-only observe + L3 recall.
SUPERVISOR_OBSERVE_TOOLS = [
    supervisor_quota_status,
    supervisor_recent_incidents,
    supervisor_delivery_summary,
    supervisor_ticket_detail,
    supervisor_guild_capabilities,
    supervisor_release_status,     # P5 watch-only: prod release/deploy state (read-only)
]
SORA_SUPERVISOR_TOOLS = SUPERVISOR_OBSERVE_TOOLS + [search_past_solutions]


# ── Sora supervisor SAFE ACTIONS (P3, supervisor roadmap) ──────────────
# Reversible, idempotent, SELF-VERIFYING: each tool acts THEN re-reads to
# confirm the world reached the intended state, returning "[OK ...verified]"
# or "[FAILED ...]" — never claims success unverified. Scoped to OP-* JIRA
# tickets only; NO deploy / force-push / delete (those stay GATED for P5).
# Reuses the prod JIRA write path (backend.jira_adapter, same as create_task).

_SUP_TICKET_RE = re.compile(r"^OP-[0-9]+$")  # ASCII digits only (\d accepts Unicode)


# The REAL pickup-blocking wedge labels a rescue must clear (audit r2 rank 1
# BLOCKER). The circuit-trip gate is ``runner-stoploss:circuit-tripped-*``
# (pre_pickup_stoploss_ok refuses on it); ``runner-stoploss:revert-*`` is the
# §11-revert history that re-trips the breaker, so clearing the trip without it
# just re-arms on the next pickup. ``claim:*`` is the OP-977 fencing token that a
# self-revert leaves stale. We deliberately do NOT match the coordinator/bridge
# AUTO-MANAGED ``runner-blocked:*`` markers (waiting-/fe-be-mismatch/
# repo-unresolved/gerrit-setup-fail) — stripping those causes marker churn /
# duplicate incidents; use supervisor_set_labels for a specific one. (Bare
# "stoploss" was a DEAD branch — production only ever writes runner-stoploss:*.)
def _is_stale_runner_label(label: str) -> bool:
    if not isinstance(label, str):
        return False
    from backend.agents.runner_stoploss import (
        REVERT_LABEL_PREFIX, TRIPPED_LABEL_PREFIX,
    )
    return (
        label.startswith(TRIPPED_LABEL_PREFIX)   # runner-stoploss:circuit-tripped-*
        or label.startswith(REVERT_LABEL_PREFIX)  # runner-stoploss:revert-*
        or label.startswith("claim:")             # OP-977 fencing token
    )


def _labels_include_circuit_trip(labels) -> bool:
    """True if any label is a circuit-trip gate (so the rescue can warn that
    resetting an unfixed trip re-arms the pickup→§11-revert loop)."""
    from backend.agents.runner_stoploss import TRIPPED_LABEL_PREFIX
    return any(isinstance(l, str) and l.startswith(TRIPPED_LABEL_PREFIX) for l in (labels or []))


@tool
async def supervisor_requeue_ticket(ticket_key: str) -> str:
    """(Sora supervisor) Re-queue a stuck ticket by clearing its assignee so the
    runner PICKUP_JQL (which requires assignee EMPTY) can re-grab it. Reversible
    (re-assign) + self-verifying. Use for a ticket idling with a bot still assigned.

    Args:
        ticket_key: e.g. "OP-2530" (only OP-NNN accepted).
    """
    key = (ticket_key or "").strip().upper()
    if not _SUP_TICKET_RE.match(key):
        return f"[SUPERVISOR] refused: '{ticket_key}' is not an OP-NNN ticket key."
    try:
        from backend.jira_adapter import build_default_jira_adapter
        adapter = build_default_jira_adapter()
        st, _ = await adapter._api("PUT", f"/rest/api/2/issue/{key}/assignee", {"accountId": None})
        if not (200 <= st < 300):
            return (
                f"[FAILED] {key}: clear-assignee returned HTTP {st} — check the "
                f"ticket exists and you have permission; try supervisor_ticket_detail first."
            )
        # Verify assignee cleared AND check the OTHER pickup gates in the SAME read
        # (audit r2 rank 3): PICKUP_JQL needs status=To Do + a class:* label + NOT
        # tier:X + no circuit-trip. Clearing the assignee is necessary but NOT
        # sufficient — don't assert "runner can re-pick" while ignoring 6 of 7 gates.
        st2, body = await adapter._api("GET", f"/rest/api/2/issue/{key}?fields=assignee,status,labels")
        if not (200 <= st2 < 300) or not isinstance(body, dict):
            return (
                f"[FAILED] {key}: assignee-clear could not be verified (read HTTP "
                f"{st2}); re-check with supervisor_ticket_detail."
            )
        fields = body.get("fields") or {}
        assignee = fields.get("assignee")
        if assignee is not None:
            return (
                f"[FAILED] {key}: assignee still set after clear — try "
                f"supervisor_ticket_detail first to inspect the ticket."
            )
        status = ((fields.get("status") or {}).get("name")) or ""
        labels = list(fields.get("labels") or [])
        blockers = _residual_pickup_blockers(status, labels)
        if not blockers:
            return f"[OK] {key} re-queued — assignee cleared (verified); runner can re-pick."
        return (
            f"[OK] {key}: assignee cleared (verified) — but ⚠ STILL NOT PICKABLE. "
            f"Residual blocker(s): {'; '.join(b for b, _ in blockers)}. "
            f"Next: {' / '.join(r for _, r in blockers)}. (requeue alone is not enough here.)"
        )
    except Exception as exc:  # noqa: BLE001
        return (
            f"[FAILED] {key}: requeue error: {exc} — check the ticket exists and you "
            f"have permission; try supervisor_ticket_detail first."
        )


def _residual_pickup_blockers(status: str, labels: list) -> list[tuple[str, str]]:
    """Return [(blocker, remediation)] for why a ticket (assignee already empty)
    still won't be picked, per PICKUP_JQL + the stoploss gate. Empty = pickable."""
    labels = [l for l in labels if isinstance(l, str)]
    out: list[tuple[str, str]] = []
    if status not in {"To Do"}:
        out.append((f"status is {status!r} (needs 'To Do')",
                    "move it to To Do with supervisor_transition_ticket"))
    if _labels_include_circuit_trip(labels):
        out.append(("carries a runner-stoploss:circuit-tripped-* label",
                    "clear it with supervisor_strip_stale_labels (or supervisor_rescue_ticket)"))
    if "tier:X" in labels:
        out.append(("labelled tier:X (human-only)",
                    "an operator must handle tier:X work"))
    if not any(l.startswith("class:") for l in labels):
        out.append(("no class:* label (GATED Story)",
                    "a human must add a class:* label to release it to the fleet"))
    return out


@tool
async def supervisor_strip_stale_labels(ticket_key: str) -> str:
    """(Sora supervisor) Remove the runner labels that WEDGE a ticket — the
    circuit-trip gate (runner-stoploss:circuit-tripped-*) + its §11-revert
    history (runner-stoploss:revert-*) + stale OP-977 fencing tokens (claim:*).
    Idempotent (no-op if none) + reversible + self-verifying. Use when a
    reverted / circuit-tripped / stuck ticket won't re-pick. Does NOT touch the
    coordinator's auto-managed runner-blocked:* markers (use supervisor_set_labels
    for a specific one). NOTE: clearing a circuit-trip re-arms the ticket — if the
    ROOT CAUSE isn't fixed it will just trip again.

    Args:
        ticket_key: e.g. "OP-2530" (only OP-NNN accepted).
    """
    key = (ticket_key or "").strip().upper()
    if not _SUP_TICKET_RE.match(key):
        return f"[SUPERVISOR] refused: '{ticket_key}' is not an OP-NNN ticket key."
    try:
        from backend.jira_adapter import build_default_jira_adapter
        adapter = build_default_jira_adapter()
        st, body = await adapter._api("GET", f"/rest/api/2/issue/{key}?fields=labels")
        # Guard the READ (audit r2 rank 2): a 404/429/5xx returns an error body,
        # NOT a raise — an unchecked empty label list would be a false "nothing
        # to strip" / false verify on the exact wedged tickets this exists to fix.
        if not (200 <= st < 300) or not isinstance(body, dict):
            return (
                f"[FAILED] {key}: label read returned HTTP {st} — check the ticket "
                f"exists and you have permission; try supervisor_ticket_detail first."
            )
        labels = list((body.get("fields") or {}).get("labels") or [])
        stale = [l for l in labels if _is_stale_runner_label(l)]
        _trip_note = (
            " (⚠ a circuit-trip was cleared — if the root cause isn't fixed it will "
            "re-trip; consider inspecting WHY it tripped before releasing.)"
            if _labels_include_circuit_trip(stale) else ""
        )
        if not stale:
            return f"[OK] {key}: no stale runner labels present (nothing to strip)."
        # Use JIRA incremental update.labels REMOVE ops (not fields.labels full
        # replacement) so a label added concurrently between our GET and write
        # is NOT wiped (audit: full-replace could clobber a fresh claim:* mutex).
        st2, _ = await adapter._api(
            "PUT", f"/rest/api/2/issue/{key}",
            {"update": {"labels": [{"remove": l} for l in stale]}},
        )
        if not (200 <= st2 < 300):
            return (
                f"[FAILED] {key}: label update returned HTTP {st2} — check the ticket "
                f"exists and you have permission; try supervisor_ticket_detail first."
            )
        st3, body3 = await adapter._api("GET", f"/rest/api/2/issue/{key}?fields=labels")
        if not (200 <= st3 < 300) or not isinstance(body3, dict):
            return (
                f"[FAILED] {key}: stripped but could NOT verify (read returned HTTP "
                f"{st3}); re-check with supervisor_ticket_detail before relying on it."
            )
        now = list((body3.get("fields") or {}).get("labels") or [])
        # Target-SCOPED verify (audit r2 rank 7): confirm the labels WE removed are
        # gone — not a global "no stale label of any kind exists" scan, which would
        # false-[FAILED] on a claim:* a runner legitimately added between our reads
        # (and provoke a cross-turn re-strip that wipes the LIVE fencing token).
        still = [l for l in stale if l in now]
        if not still:
            return f"[OK] {key}: stripped {len(stale)} stale label(s) {stale} (verified).{_trip_note}"
        return (
            f"[FAILED] {key}: targeted labels remain after strip: {still} — try "
            f"supervisor_ticket_detail first to inspect the ticket."
        )
    except Exception as exc:  # noqa: BLE001
        return (
            f"[FAILED] {key}: strip-labels error: {exc} — check the ticket exists and "
            f"you have permission; try supervisor_ticket_detail first."
        )


@tool
async def supervisor_comment_ticket(ticket_key: str, text: str) -> str:
    """(Sora supervisor) Post a comment on an OP-* ticket (e.g. explain a rescue
    or leave a note). Self-verifying via the returned comment id.

    Args:
        ticket_key: e.g. "OP-2530" (only OP-NNN accepted).
        text: the comment body.
    """
    key = (ticket_key or "").strip().upper()
    if not _SUP_TICKET_RE.match(key):
        return f"[SUPERVISOR] refused: '{ticket_key}' is not an OP-NNN ticket key."
    if not (text or "").strip():
        return "[SUPERVISOR] refused: empty comment text."
    try:
        from backend.jira_adapter import build_default_jira_adapter
        adapter = build_default_jira_adapter()
        res = await adapter.comment(key, text)
        cid = res.get("id") if isinstance(res, dict) else None
        if not cid:
            return (
                f"[FAILED] {key}: comment call returned no id — check the ticket "
                f"exists and you have permission; try supervisor_ticket_detail first."
            )
        return f"[OK] {key}: comment posted (id={cid}, verified)."
    except Exception as exc:  # noqa: BLE001
        return (
            f"[FAILED] {key}: comment error: {exc} — check the ticket exists and you "
            f"have permission; try supervisor_ticket_detail first."
        )


@tool
async def supervisor_rescue_ticket(ticket_key: str, comment_text: str = "") -> str:
    """(Sora supervisor) ONE-SHOT rescue of a STUCK ticket — do the whole
    unwedge chain in a single call and return ONE combined verdict.

    Runs: inspect → strip the wedge labels (runner-stoploss:circuit-tripped-* /
    revert-* + stale claim:* fencing) → clear the assignee → (optional) post an
    audit comment → each sub-step self-verifies. PREFER THIS over chaining
    strip_stale_labels + requeue_ticket + comment_ticket yourself: it is cheaper
    (one call, not a 3-4 round chain), it won't leave the ticket half-fixed, and
    it reports exactly what changed — INCLUDING an honest "still not pickable"
    warning when a NON-label gate remains (wrong status, no class:* label, tier:X)
    that this tool deliberately will NOT touch. Reversible; it NEVER changes status
    or closes the ticket. Use ONLY when the operator says a ticket is stuck/idle —
    do not run it on a ticket a runner is actively working (it clears a live claim).

    Args:
        ticket_key: e.g. "OP-2533" (only OP-NNN accepted).
        comment_text: optional audit note to post after the rescue (skip if "").
    """
    key = (ticket_key or "").strip().upper()
    if not _SUP_TICKET_RE.match(key):
        return f"[SUPERVISOR] refused: '{ticket_key}' is not an OP-NNN ticket key."

    def _short(msg: str) -> str:
        # keep the status tag + the meaningful clause, drop the redundant key echo
        s = str(msg).replace(f"{key}: ", "").replace(f"{key} ", "").strip()
        return s.split(" — ")[0][:160]

    steps: list[str] = []
    all_ok = True
    # 1) inspect (also confirms the ticket exists + surfaces what we're fixing)
    detail = await supervisor_ticket_detail.ainvoke({"ticket_key": key})
    if str(detail).startswith(("[FAILED]", "[SUPERVISOR] ")) and "not an OP" not in str(detail):
        # a read failure here means we can't safely proceed
        if not str(detail).startswith("[SUPERVISOR] OP"):
            return (
                f"[FAILED] {key} rescue aborted: could not read the ticket "
                f"({_short(detail)}). Check it exists and you have permission."
            )
    # 2) strip stale runner labels (idempotent — no-op if none)
    strip = await supervisor_strip_stale_labels.ainvoke({"ticket_key": key})
    steps.append(f"labels: {_short(strip)}")
    if str(strip).startswith("[FAILED]"):
        all_ok = False
    # 3) clear assignee so PICKUP_JQL can re-grab (idempotent if already empty)
    requeue = await supervisor_requeue_ticket.ainvoke({"ticket_key": key})
    steps.append(f"requeue: {_short(requeue)}")
    requeue_str = str(requeue)
    if requeue_str.startswith("[FAILED]"):
        all_ok = False
    # requeue may succeed on the assignee-clear yet report the ticket is STILL NOT
    # PICKABLE because of a gate this tool won't touch (status/class:*/tier:X).
    not_pickable = "STILL NOT PICKABLE" in requeue_str
    # 4) optional audit comment
    if (comment_text or "").strip():
        cm = await supervisor_comment_ticket.ainvoke({"ticket_key": key, "text": comment_text})
        steps.append(f"comment: {_short(cm)}")
        if str(cm).startswith("[FAILED]"):
            all_ok = False
    body = "\n  • ".join(steps)
    if all_ok and not not_pickable:
        return f"[OK] {key} rescued (all steps verified):\n  • {body}"
    if all_ok and not_pickable:
        # every write succeeded, but a non-label gate still blocks pickup — report
        # honestly so Sora tells the operator instead of claiming full success.
        return (
            f"[OK] {key}: labels cleared + assignee cleared (verified), BUT the "
            f"ticket is STILL NOT PICKABLE — see the requeue line for the residual "
            f"gate (status / class:* / tier:X) and its remediation. The un-wedge "
            f"succeeded; the ticket needs that further step (which this tool won't "
            f"do automatically):\n  • {body}"
        )
    return (
        f"[FAILED] {key} rescue INCOMPLETE — at least one step failed:\n  • {body}\n"
        f"Inspect with supervisor_ticket_detail and retry only the failed step; "
        f"do NOT blindly re-run the whole rescue."
    )


# Intent word → candidate transition-name substrings, matched against the
# ticket's ACTUAL available transitions (JIRA workflows vary per project — this
# one uses 進行中 / 作業開始 / Won't Do / Force Close, NOT a generic "Done").
_TRANSITION_ALIASES: dict[str, tuple[str, ...]] = {
    "todo": ("to do", "backlog", "todo", "open", "reopen"),
    "in_progress": ("進行中", "in progress", "作業開始", "start", "in_progress"),
    "review": ("review", "submit", "審", "レビュー"),
    # "done" = genuine completion only — deliberately NOT "close"/"force close"
    # (those Archive the ticket, discouraged here). If a workflow has no real
    # done transition, target="done" honestly returns the available list.
    "done": ("done", "公開", "approve", "deploy", "完了", "resolve"),
    "wont_do": ("won't do", "wont do", "will not", "cancel", "reject", "force close"),
}


def _match_transition_name(target: str, available: dict[str, str]) -> str | None:
    """Return the available transition NAME matching ``target`` (a real name,
    an intent word, or a substring), else None. ``available`` = {name: id}."""
    t = (target or "").strip().lower()
    # 1) exact (case-insensitive) transition name
    for name in available:
        if name.lower() == t:
            return name
    # 2) intent-word alias → substring against available names
    for sub in _TRANSITION_ALIASES.get(t.replace(" ", "_"), ()):
        for name in available:
            if sub.lower() in name.lower():
                return name
    # 3) target itself as a loose substring
    for name in available:
        if t and t in name.lower():
            return name
    return None


@tool
async def supervisor_list_transitions(ticket_key: str) -> str:
    """(Sora supervisor) List the workflow transitions actually available on an
    OP-* ticket RIGHT NOW. Read-only. Call this BEFORE supervisor_transition_ticket
    if unsure — workflows differ per project (this one has no generic "Done").
    """
    key = (ticket_key or "").strip().upper()
    if not _SUP_TICKET_RE.match(key):
        return f"[SUPERVISOR] refused: '{ticket_key}' is not an OP-NNN ticket key."
    try:
        from backend.jira_adapter import build_default_jira_adapter
        adapter = build_default_jira_adapter()
        st, body = await adapter._api("GET", f"/rest/api/2/issue/{key}/transitions")
        if not (200 <= st < 300) or not isinstance(body, dict):
            return (
                f"[FAILED] {key}: transitions read returned HTTP {st} — check the "
                f"ticket exists; try supervisor_ticket_detail first."
            )
        names = [t.get("name") for t in (body.get("transitions") or [])]
        if not names:
            return f"[SUPERVISOR] {key}: no transitions available (may be terminal/closed)."
        return f"[SUPERVISOR] {key} available transitions: {names}"
    except Exception as exc:  # noqa: BLE001
        return f"[FAILED] {key}: transitions unavailable: {exc}"


# Defined after the observe bundle literal (it groups with the transition tool);
# register it into the read-only observe set + TOOL_MAP (built later) here.
# NOTE: SORA_SUPERVISOR_TOOLS (line ~3083) was concatenated BEFORE this append,
# so it doesn't see list_transitions via SUPERVISOR_OBSERVE_TOOLS — append to
# BOTH so Sora actually gets it bound in her conversational tool set.
SUPERVISOR_OBSERVE_TOOLS.append(supervisor_list_transitions)
SORA_SUPERVISOR_TOOLS.append(supervisor_list_transitions)


@tool
async def supervisor_transition_ticket(ticket_key: str, target: str) -> str:
    """(Sora supervisor) Move an OP-* ticket to another workflow state. Reads the
    ticket's ACTUAL available transitions and matches ``target`` against them
    (real transition name, or an intent word: todo/in_progress/review/done/
    wont_do), then self-verifies the status changed. Fail-closed with the
    available list on no match. NOTE: closing a ticket is often a MULTI-STEP path
    in this project — do NOT assume a single "done" closes it; call
    supervisor_list_transitions if unsure.

    Args:
        ticket_key: e.g. "OP-2530" (only OP-NNN accepted).
        target: a real transition name (e.g. "進行中", "Won't Do") or an intent
            word (todo / in_progress / review / done / wont_do).
    """
    key = (ticket_key or "").strip().upper()
    if not _SUP_TICKET_RE.match(key):
        return f"[SUPERVISOR] refused: '{ticket_key}' is not an OP-NNN ticket key."
    if not (target or "").strip():
        return "[SUPERVISOR] refused: empty target."
    try:
        from backend.jira_adapter import build_default_jira_adapter
        adapter = build_default_jira_adapter()
        st0, b0 = await adapter._api("GET", f"/rest/api/2/issue/{key}?fields=status")
        before = ((b0.get("fields") or {}).get("status") or {}).get("name") if isinstance(b0, dict) else None
        stt, tbody = await adapter._api("GET", f"/rest/api/2/issue/{key}/transitions")
        available = {t["name"]: t["id"] for t in (tbody.get("transitions") or [])} if isinstance(tbody, dict) else {}
        match = _match_transition_name(target, available)
        if not match:
            return (
                f"[FAILED] {key}: no transition matching '{target}'. Available now: "
                f"{list(available)}. Pick one of those (workflows differ per project)."
            )
        # Guard (audit): never let a loose intent word (e.g. "done") select a
        # TERMINAL/destructive transition (Force Close / Archive / Won't Do) —
        # closing/archiving must be named explicitly by the operator.
        # Normalize underscores→spaces so explicit intent words like "wont_do"
        # count as an explicit terminal request (matches "wont do" below).
        _tl = (target or "").strip().lower().replace("_", " ")
        _DESTRUCTIVE = ("force close", "archive", "won't do", "wont do", "cancel", "reject")
        if any(d in match.lower() for d in _DESTRUCTIVE) and not (
            any(d in _tl for d in _DESTRUCTIVE) or _tl == match.lower()
        ):
            return (
                f"[SUPERVISOR] refused: '{target}' resolved to the terminal transition "
                f"{match!r} (close/archive). To close/archive, pass the exact name "
                f"'{match}' explicitly. Available: {list(available)}."
            )
        sp, _ = await adapter._api("POST", f"/rest/api/2/issue/{key}/transitions", {"transition": {"id": available[match]}})
        if not (200 <= sp < 300):
            return f"[FAILED] {key}: transition '{match}' POST returned HTTP {sp}."
        st1, b1 = await adapter._api("GET", f"/rest/api/2/issue/{key}?fields=status")
        after = ((b1.get("fields") or {}).get("status") or {}).get("name") if isinstance(b1, dict) else None
        # Verification integrity (audit r2 codex#1): the POST returned 2xx, but
        # do NOT claim success unless the re-read CONFIRMS the status moved. A
        # failed/malformed verify read, or an unchanged status, is [FAILED] —
        # never a false [OK] (a write-capable supervisor must not tell the
        # operator a transition happened when it did not).
        if not (200 <= st1 < 300) or after is None:
            return (
                f"[FAILED] {key}: transition '{match}' POSTed but the status re-read "
                f"returned HTTP {st1} (could not confirm). Re-check with "
                f"supervisor_ticket_detail before relying on it."
            )
        if after != before:
            return f"[OK] {key}: transitioned via '{match}' — status {before!r} → {after!r} (verified)."
        return (
            f"[FAILED] {key}: transition '{match}' POSTed but status is still "
            f"{after!r} (unchanged) — it may already be satisfied, or the move did "
            f"not apply. Verify with supervisor_ticket_detail; do not assume it moved."
        )
    except Exception as exc:  # noqa: BLE001
        return (
            f"[FAILED] {key}: transition error: {exc} — try "
            f"supervisor_list_transitions first to see valid moves."
        )


@tool
async def supervisor_set_labels(
    ticket_key: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> str:
    """(Sora supervisor) Add and/or remove labels on an OP-* ticket. Idempotent
    (adding an existing / removing an absent label is a no-op) + self-verifying
    (re-reads labels to confirm). Refuses if both add and remove are empty.

    Args:
        ticket_key: e.g. "OP-2530" (only OP-NNN accepted).
        add: labels to add (optional).
        remove: labels to remove (optional).
    """
    key = (ticket_key or "").strip().upper()
    if not _SUP_TICKET_RE.match(key):
        return f"[SUPERVISOR] refused: '{ticket_key}' is not an OP-NNN ticket key."
    add_set = [l for l in (add or []) if l]
    # A label in BOTH add and remove is contradictory — let ADD win (drop it
    # from remove) so we never emit conflicting/order-dependent JIRA ops.
    rem_set = [l for l in (remove or []) if l and l not in add_set]
    # de-dupe within each set (JIRA rejects duplicate ops on some versions)
    add_set = list(dict.fromkeys(add_set))
    rem_set = list(dict.fromkeys(rem_set))
    if not add_set and not rem_set:
        return "[SUPERVISOR] refused: nothing to do (both add and remove empty)."
    try:
        from backend.jira_adapter import build_default_jira_adapter
        adapter = build_default_jira_adapter()
        st, body = await adapter._api("GET", f"/rest/api/2/issue/{key}?fields=labels")
        if not (200 <= st < 300) or not isinstance(body, dict):
            return (
                f"[FAILED] {key}: label read returned HTTP {st} — check the ticket "
                f"exists; try supervisor_ticket_detail first."
            )
        # (read above doubles as an existence/permission check)
        # Use JIRA incremental update.labels ADD/REMOVE ops (not fields.labels
        # full replacement) so a label changed concurrently between our GET and
        # write is NOT clobbered (audit: full-replace could wipe a fresh claim:*
        # mutex or another supervisor's edit).
        ops = [{"remove": l} for l in rem_set] + [{"add": l} for l in add_set]
        st2, _ = await adapter._api(
            "PUT", f"/rest/api/2/issue/{key}", {"update": {"labels": ops}},
        )
        if not (200 <= st2 < 300):
            return f"[FAILED] {key}: label update returned HTTP {st2}."
        st3, body3 = await adapter._api("GET", f"/rest/api/2/issue/{key}?fields=labels")
        now = list((body3.get("fields") or {}).get("labels") or []) if isinstance(body3, dict) else []
        ok = all(l in now for l in add_set) and all(l not in now for l in rem_set)
        if ok:
            return f"[OK] {key}: labels now {now} (verified)."
        return (
            f"[FAILED] {key}: labels are {now} after update, expected add={add_set} "
            f"remove={rem_set} to hold; try supervisor_ticket_detail first."
        )
    except Exception as exc:  # noqa: BLE001
        return (
            f"[FAILED] {key}: set-labels error: {exc} — check the ticket exists and "
            f"you have permission; try supervisor_ticket_detail first."
        )


# Bound to Sora's chat behind OMNISIGHT_ORCHESTRATOR_ACTION_TOOLS (default on).
# ⚠ save_solution (model-callable L3 write) was REMOVED from this live Sora set
# 2026-07-11 (U6-0 step-0 containment). Rationale: its write trusts a raw
# model-supplied gerrit_change_id and mints quality_score=1.0 with NO server
# +2 verification (see save_solution above), and episodic_memory is a GLOBAL
# table (db.py, no user_id/tenant_id) read back into prompts (rag_prefetch /
# search_past_solutions) → a model-forgeable, cross-user memory-poisoning
# write-loop. The legitimate "remember a verified rescue" write is UNAFFECTED:
# it is produced SERVER-SIDE on real Gerrit merge
# (webhooks._save_merged_solution_to_l3). Sora keeps the READ
# (search_past_solutions). Re-binding a model write requires the U6-0
# provenance gate + fail-closed action-capability guard (design doc, same date).
SORA_ACTION_TOOLS = [
    supervisor_rescue_ticket,       # compound one-shot (prefer for stuck-ticket rescue)
    supervisor_requeue_ticket,
    supervisor_strip_stale_labels,
    supervisor_comment_ticket,
    supervisor_transition_ticket,
    supervisor_set_labels,
]


# ── Sora supervisor PLANNING (P4, supervisor roadmap) ──────────────────
# Decomposition is persona-driven (Sora files N GATED Stories via create_task
# per the epic-decomposition SOP granularity rules); the one WRITE primitive
# planning needs is wiring the dependency graph between the filed Stories.


@tool
async def supervisor_link_blocks(blocker_key: str, blocked_key: str) -> str:
    """(Sora supervisor / planning) Create a JIRA "Blocks" dependency: blocker_key
    BLOCKS blocked_key (blocked_key waits for blocker_key). Self-verifying +
    idempotent. Use to wire dependencies between decomposed Stories (schema-first
    lands before its consumers, etc.).

    Args:
        blocker_key: the ticket that must land first (e.g. "OP-2540").
        blocked_key: the ticket that waits (e.g. "OP-2541").
    """
    a = (blocker_key or "").strip().upper()
    b = (blocked_key or "").strip().upper()
    if not _SUP_TICKET_RE.match(a) or not _SUP_TICKET_RE.match(b):
        return f"[SUPERVISOR] refused: both keys must be OP-NNN (got {blocker_key!r}, {blocked_key!r})."
    if a == b:
        return f"[SUPERVISOR] refused: a ticket cannot block itself ({a})."
    try:
        from backend.jira_adapter import build_default_jira_adapter
        adapter = build_default_jira_adapter()

        async def _summary(k: str) -> str:
            # Echo each ticket's title so a WRONG-KEY link is human-visible (audit
            # r2 rank 4: link_blocks otherwise self-verifies a link onto whatever
            # keys it was given — including a mis-scraped/invented real ticket).
            try:
                _st, bd = await adapter._api("GET", f"/rest/api/2/issue/{k}?fields=summary")
                if not (200 <= _st < 300) or not isinstance(bd, dict):
                    return "?"
                return (((bd.get("fields") or {}).get("summary")) or "?")[:60]
            except Exception:  # noqa: BLE001
                return "?"

        async def _linked() -> bool:
            _st, body = await adapter._api("GET", f"/rest/api/2/issue/{b}?fields=issuelinks")
            links = (body.get("fields") or {}).get("issuelinks") or [] if isinstance(body, dict) else []
            for lk in links:
                if (lk.get("type") or {}).get("name") != "Blocks":
                    continue
                if (lk.get("inwardIssue") or {}).get("key") == a:  # b is blocked BY a
                    return True
            return False

        if await _linked():
            sa, sb = await _summary(a), await _summary(b)
            return f"[OK] {a} «{sa}» already blocks {b} «{sb}» (verified, no-op). If those aren't the intended stories, unlink."
        # inwardIssue = BLOCKER, outwardIssue = BLOCKED (SOP §Blocks links;
        # this direction is a known trap — hence the verify below).
        st, _ = await adapter._api("POST", "/rest/api/2/issueLink", {
            "type": {"name": "Blocks"},
            "inwardIssue": {"key": a},
            "outwardIssue": {"key": b},
        })
        if not (200 <= st < 300):
            return f"[FAILED] link {a}->{b}: issueLink POST returned HTTP {st}."
        if await _linked():
            sa, sb = await _summary(a), await _summary(b)
            return f"[OK] {a} «{sa}» now blocks {b} «{sb}» (verified). If those aren't the intended stories, unlink."
        return f"[FAILED] {a}->{b}: link not present after POST (check direction)."
    except Exception as exc:  # noqa: BLE001
        return f"[FAILED] link {a}->{b}: {exc}"


SORA_PLANNING_TOOLS = [supervisor_link_blocks]


# ── Sora P5: propose-and-approve gate for DANGEROUS actions ────────────
# Sora can only PROPOSE (deploy / promote / restart / rollback) — a proposal is
# persisted as 'pending' and a HUMAN must approve before anything runs, and
# execution always goes through the existing reversible machinery (release-train
# / systemctl), never raw shell. These tools NEVER execute. The execution path is
# a separate, operator-gated, supervised step. Kept behind
# OMNISIGHT_ORCHESTRATOR_P5_PROPOSE (default on — proposing is harmless: it only
# files a record a human must then act on).

_P5_ACTION_KINDS: dict[str, tuple[str, str]] = {
    # kind: (preview template hint, blast-radius)
    "deploy": (
        "run the GATED release-train to deploy the given tag/digest to prod "
        "(candidate → staging → canary → promote → zero-downtime rolling deploy)",
        "prod backend+frontend, zero-downtime rolling; REVERSIBLE by redeploying "
        "the previous validated digest",
    ),
    "promote": (
        "promote a validated staging bundle to a release tag (cosign-signed); "
        "registry retag only — no prod change until a subsequent deploy",
        "registry tag only; no live prod impact on its own",
    ),
    "restart": (
        "restart a systemd service via `systemctl --user restart <service>`",
        "the named service only; a critical service (backend/caddy) means a brief "
        "disruption — a non-critical one (slo-monitor, staging) is low-impact",
    ),
    "rollback": (
        "redeploy the PREVIOUS validated prod image digest (RT-20 image-tag-only "
        "rollback)",
        "prod rolling redeploy back to a known-good digest; REVERSIBLE",
    ),
}


@tool
async def propose_action(action_kind: str, params_json: str = "{}", rationale: str = "") -> str:
    """(Sora P5) PROPOSE a dangerous operation for HUMAN approval — deploy /
    promote / restart / rollback. This does NOT execute anything: it files a
    pending proposal (with a preview + blast-radius) that the operator must
    approve; only then does it run, and only through the existing reversible
    release-train / systemctl machinery. Use when the operator asks you to
    deploy/restart/etc. — you PREPARE it, they approve it. NEVER claim the action
    ran; say it's filed and awaiting approval.

    Args:
        action_kind: one of deploy / promote / restart / rollback.
        params_json: JSON string of specifics, e.g. '{"tag":"v0.7.34"}' (deploy),
            '{"service":"omnisight-slo-monitor"}' (restart), '{"to_tag":"v0.7.33"}'
            (rollback). Keep it minimal and concrete.
        rationale: one line on WHY (shown to the operator on the approval card).
    """
    import json
    kind = (action_kind or "").strip().lower()
    if kind not in _P5_ACTION_KINDS:
        return (
            f"[SUPERVISOR] refused: unknown action_kind {action_kind!r}; "
            f"allowed: {sorted(_P5_ACTION_KINDS)}."
        )
    try:
        params = json.loads(params_json) if params_json else {}
        if not isinstance(params, dict):
            return "[SUPERVISOR] refused: params_json must be a JSON object."
    except (ValueError, TypeError):
        return f"[SUPERVISOR] refused: params_json is not valid JSON: {params_json!r}"

    preview_hint, blast = _P5_ACTION_KINDS[kind]
    # a compact, human-readable title + preview for the approval card
    _p = ", ".join(f"{k}={v}" for k, v in params.items()) or "(no params)"
    title = f"{kind}: {_p}"
    preview = f"On approval, Sora will {preview_hint}. Params: {_p}."
    if rationale.strip():
        preview += f"\nWhy: {rationale.strip()}"

    try:
        import time as _t
        import uuid as _uuid
        from backend import db as _db
        ctx = get_chat_context() or {}
        pid = f"pa-{_uuid.uuid4().hex[:12]}"
        async with get_pool().acquire() as conn:
            await _db.insert_proposed_action(conn, {
                "id": pid,
                "tenant_id": ctx.get("tenant_id", ""),
                "user_id": ctx.get("user_id", ""),
                "session_id": ctx.get("session_id", ""),
                "action_kind": kind,
                "params": json.dumps(params, sort_keys=True),
                "title": title,
                "preview": preview,
                "blast_radius": blast,
                "proposed_by": "sora",
                "proposed_at": _t.time(),
            })
        return (
            f"[OK] Proposal filed (id={pid}) — AWAITING OPERATOR APPROVAL. "
            f"Nothing has been executed.\n  action: {title}\n  blast radius: {blast}\n"
            f"  Tell the operator this is proposed, NOT done; it runs only after "
            f"they approve it."
        )
    except Exception as exc:  # noqa: BLE001
        return f"[FAILED] could not file the proposal: {exc}"


@tool
async def list_pending_actions() -> str:
    """(Sora P5) READ-ONLY: list the dangerous-action proposals still AWAITING
    operator approval (deploy/promote/restart/rollback Sora has proposed). Pure
    read — no side effects."""
    try:
        from backend import db as _db
        async with get_pool().acquire() as conn:
            rows = await _db.list_proposed_actions(conn, status="pending", limit=25)
        if not rows:
            return "[SUPERVISOR] no pending action proposals."
        lines = [f"[SUPERVISOR] {len(rows)} pending proposal(s) awaiting approval:"]
        for r in rows:
            lines.append(f"  • {r.get('id')} — {r.get('title')} (by {r.get('proposed_by')})")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        return f"[FAILED] pending-actions list unavailable: {exc}"


SORA_P5_PROPOSE_TOOLS = [propose_action, list_pending_actions]


TASK_TOOLS = [get_next_task, update_task_status, add_task_comment]
# Orchestration tools are the user-facing planner's lever to turn an
# understood intent into real runner work. Deliberately NOT folded into
# ALL_TOOLS / TASK_TOOLS — only the user-facing guilds (general, devops)
# may file Stories; specialists execute, they don't queue new work.
ORCHESTRATION_TOOLS = [create_task]
REPORT_TOOLS = [generate_artifact_report]
SIMULATION_TOOLS = [run_simulation]

# Base tools available to most agents (excludes specialist tools: review, report, simulation)
ALL_TOOLS = FILE_TOOLS + GIT_TOOLS + BASH_TOOLS + TASK_TOOLS

# Complete registry of every tool for executor lookup (must include ALL tool categories)
TOOL_MAP = {t.name: t for t in ALL_TOOLS + ORCHESTRATION_TOOLS + REVIEW_TOOLS + REPORT_TOOLS + SIMULATION_TOOLS + PLATFORM_TOOLS + MEMORY_TOOLS + EPISODIC_TOOLS + DEPLOY_TOOLS + ARTIFACT_TOOLS + MCP_TOOLS + WEB_SEARCH_TOOLS + IMAGE_TOOLS + SUPERVISOR_OBSERVE_TOOLS + SORA_ACTION_TOOLS + SORA_PLANNING_TOOLS + SORA_P5_PROPOSE_TOOLS}

_ARCHITECT_TOOLS = ALL_TOOLS + MEMORY_TOOLS + EPISODIC_TOOLS + WEB_SEARCH_TOOLS
_DESIGN_TOOLS = ALL_TOOLS + MEMORY_TOOLS + EPISODIC_TOOLS
_FIRMWARE_TOOLS = (
    ALL_TOOLS
    + SIMULATION_TOOLS
    + PLATFORM_TOOLS
    + MEMORY_TOOLS
    + EPISODIC_TOOLS
    + DEPLOY_TOOLS
    + ARTIFACT_TOOLS
)
_SOFTWARE_TOOLS = (
    ALL_TOOLS
    + SIMULATION_TOOLS
    + PLATFORM_TOOLS
    + MEMORY_TOOLS
    + EPISODIC_TOOLS
    + ARTIFACT_TOOLS
    + MCP_TOOLS
    + IMAGE_TOOLS
)
_VALIDATOR_TOOLS = (
    FILE_TOOLS
    + GIT_TOOLS
    + [run_bash]
    + TASK_TOOLS
    + SIMULATION_TOOLS
    + PLATFORM_TOOLS
    + MEMORY_TOOLS
    + EPISODIC_TOOLS
    + DEPLOY_TOOLS
    + ARTIFACT_TOOLS
)
_REPORTER_TOOLS = FILE_TOOLS + GIT_TOOLS + TASK_TOOLS + REPORT_TOOLS + MEMORY_TOOLS + ARTIFACT_TOOLS
_REVIEWER_TOOLS = (
    [read_file, list_directory, read_yaml, search_in_files]
    + [git_status, git_log, git_diff, git_diff_staged, git_branch]
    + REVIEW_TOOLS
    + [get_next_task, add_task_comment]
    + MEMORY_TOOLS
)
# create_task (ORCHESTRATION_TOOLS) is deliberately NOT bound to the
# specialist guilds. Filing happens ONLY on the conversational path
# (conversation_node binds it via extra_tools), which surfaces the tool's
# REAL result; the heavy specialist pipeline once hallucinated a fake
# ticket id instead of calling the tool (dogfood 2026-06-30).
# ORCHESTRATION_TOOLS stays in TOOL_MAP so conversation_node's executor
# can still resolve create_task by name.
_GENERAL_TOOLS = ALL_TOOLS + MEMORY_TOOLS + EPISODIC_TOOLS + DEPLOY_TOOLS + ARTIFACT_TOOLS + MCP_TOOLS + IMAGE_TOOLS
_DEVOPS_TOOLS = ALL_TOOLS + PLATFORM_TOOLS + MEMORY_TOOLS + EPISODIC_TOOLS + DEPLOY_TOOLS + ARTIFACT_TOOLS
_MECHANICAL_TOOLS = FILE_TOOLS + BASH_TOOLS + TASK_TOOLS + SIMULATION_TOOLS + MEMORY_TOOLS + ARTIFACT_TOOLS
# BP.N.4: WebSearch is opt-in for latest-knowledge guilds only.
_INTEL_TOOLS = ALL_TOOLS + MEMORY_TOOLS + EPISODIC_TOOLS + WEB_SEARCH_TOOLS

GUILD_TOOLS: dict[str, list] = {
    Guild.architect.value: _ARCHITECT_TOOLS,
    Guild.sa_sd.value: _DESIGN_TOOLS,
    Guild.ux.value: _GENERAL_TOOLS,
    Guild.pm.value: _GENERAL_TOOLS,
    Guild.gateway.value: _SOFTWARE_TOOLS,
    Guild.bsp.value: _FIRMWARE_TOOLS,
    Guild.hal.value: _FIRMWARE_TOOLS,
    Guild.algo_cv.value: _SOFTWARE_TOOLS,
    Guild.optical.value: _MECHANICAL_TOOLS,
    Guild.isp.value: _FIRMWARE_TOOLS,
    Guild.audio.value: _FIRMWARE_TOOLS,
    Guild.frontend.value: _SOFTWARE_TOOLS,
    Guild.backend.value: _SOFTWARE_TOOLS,
    Guild.sre.value: _DEVOPS_TOOLS,
    Guild.qa.value: _VALIDATOR_TOOLS,
    Guild.auditor.value: _REVIEWER_TOOLS,
    Guild.red_team.value: _REVIEWER_TOOLS,
    Guild.forensics.value: _REVIEWER_TOOLS,
    Guild.intel.value: _INTEL_TOOLS,
    Guild.reporter.value: _REPORTER_TOOLS,
    Guild.custom.value: _GENERAL_TOOLS,
}

_AGENT_TOOL_ALIASES: dict[str, list] = {
    "firmware": GUILD_TOOLS[Guild.bsp.value],
    "software": GUILD_TOOLS[Guild.backend.value],
    "validator": GUILD_TOOLS[Guild.qa.value],
    "reviewer": GUILD_TOOLS[Guild.auditor.value],
    "general": GUILD_TOOLS[Guild.custom.value],
    "devops": GUILD_TOOLS[Guild.sre.value],
    "mechanical": GUILD_TOOLS[Guild.optical.value],
    "manufacturing": GUILD_TOOLS[Guild.optical.value],
}

AGENT_TOOLS: dict[str, list] = {**GUILD_TOOLS, **_AGENT_TOOL_ALIASES}
