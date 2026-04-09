"""Isolated Workspace Manager — git worktree based.

Each agent gets its own worktree + branch when assigned a task.
Worktrees share the same .git object store so they're fast to create
and use minimal disk space.

Lifecycle:
  1. provision(agent_id, repo_url, task_id)  → creates worktree + branch
  2. Agent works inside its workspace (file/git/bash tools scoped to it)
  3. finalize(agent_id)                      → commits, generates diff summary
  4. cleanup(agent_id)                       → removes worktree + branch (optional)
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from backend.events import emit_agent_update, emit_pipeline_phase, emit_workspace

logger = logging.getLogger(__name__)

# Base directory for all agent worktrees
_WORKSPACES_ROOT = Path(__file__).resolve().parent.parent / ".agent_workspaces"
_WORKSPACES_ROOT.mkdir(exist_ok=True)

# The main repo to create worktrees from (the project itself)
_MAIN_REPO = Path(__file__).resolve().parent.parent

PROVISION_TIMEOUT = 30  # seconds


@dataclass
class WorkspaceInfo:
    """Tracks an active agent workspace."""
    agent_id: str
    task_id: str
    branch: str
    path: Path
    repo_source: str  # path or url of the source repo
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    commit_count: int = 0
    status: str = "active"  # active | finalized | cleaned


# Registry of active workspaces
_workspaces: dict[str, WorkspaceInfo] = {}


async def _run(cmd: str, cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a shell command, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd or _MAIN_REPO,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROVISION_TIMEOUT)
    return (
        proc.returncode or 0,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


async def provision(
    agent_id: str,
    task_id: str,
    repo_source: str | None = None,
) -> WorkspaceInfo:
    """Create an isolated workspace for an agent.

    Uses git worktree for instant provisioning from the main repo,
    or git clone for external repos.
    """
    branch = f"agent/{agent_id}/{task_id}"
    ws_path = _WORKSPACES_ROOT / agent_id

    # Clean up existing workspace if any
    if agent_id in _workspaces:
        await cleanup(agent_id)

    emit_pipeline_phase("workspace_provision", f"Creating workspace for {agent_id}")

    source = repo_source or str(_MAIN_REPO)
    is_local = not source.startswith("http")

    if is_local and Path(source).is_dir():
        # Use git worktree (fast, shares object store)
        # First create the branch from current HEAD
        rc, out, err = await _run(f'git branch "{branch}" HEAD 2>/dev/null; echo ok', cwd=Path(source))

        # Create worktree
        if ws_path.exists():
            shutil.rmtree(ws_path, ignore_errors=True)
        rc, out, err = await _run(
            f'git worktree add "{ws_path}" "{branch}"',
            cwd=Path(source),
        )
        if rc != 0:
            raise RuntimeError(f"Failed to create worktree: {err or out}")

        logger.info("Workspace provisioned (worktree): %s → %s", agent_id, ws_path)
    else:
        # Clone external repo
        if ws_path.exists():
            shutil.rmtree(ws_path, ignore_errors=True)
        rc, out, err = await _run(f'git clone "{source}" "{ws_path}"')
        if rc != 0:
            raise RuntimeError(f"Failed to clone: {err or out}")
        # Create and checkout branch
        await _run(f'git checkout -b "{branch}"', cwd=ws_path)
        logger.info("Workspace provisioned (clone): %s → %s", agent_id, ws_path)

    # Configure git user for this workspace
    await _run(f'git config user.name "Agent-{agent_id}"', cwd=ws_path)
    await _run(f'git config user.email "{agent_id}@omnisight.local"', cwd=ws_path)

    info = WorkspaceInfo(
        agent_id=agent_id,
        task_id=task_id,
        branch=branch,
        path=ws_path,
        repo_source=source,
    )
    _workspaces[agent_id] = info

    emit_workspace(agent_id, "provisioned", f"branch={branch}, path={ws_path}")
    emit_agent_update(agent_id, "running", f"Workspace ready: branch={branch}")
    return info


async def finalize(agent_id: str) -> dict:
    """Finalize workspace: stage all changes, commit, generate summary.

    Returns a dict with branch, commit_count, diff_summary, files_changed.
    """
    info = _workspaces.get(agent_id)
    if not info:
        return {"error": f"No workspace for {agent_id}"}

    ws = info.path
    emit_pipeline_phase("workspace_finalize", f"Finalizing workspace for {agent_id}")

    # Stage all changes
    await _run("git add -A", cwd=ws)

    # Check if there are changes to commit
    rc, status_out, _ = await _run("git status --porcelain", cwd=ws)
    if not status_out.strip():
        info.status = "finalized"
        return {
            "branch": info.branch,
            "commit_count": 0,
            "diff_summary": "No changes made.",
            "files_changed": [],
        }

    # Commit
    commit_msg = f"[Agent {agent_id}] Task {info.task_id} — auto-commit"
    await _run(f'git commit -m "{commit_msg}"', cwd=ws)
    info.commit_count += 1

    # Generate summary
    _, log_out, _ = await _run(
        f"git log --oneline main..{info.branch} 2>/dev/null || git log --oneline -5",
        cwd=ws,
    )
    _, diff_stat, _ = await _run(
        f"git diff --stat main..{info.branch} 2>/dev/null || git diff --stat HEAD~1",
        cwd=ws,
    )
    _, files_out, _ = await _run(
        f"git diff --name-only main..{info.branch} 2>/dev/null || git diff --name-only HEAD~1",
        cwd=ws,
    )

    info.status = "finalized"
    emit_workspace(agent_id, "finalized", f"{info.commit_count} commit(s), {len([f for f in files_out.splitlines() if f.strip()])} file(s)")
    emit_agent_update(agent_id, "success", f"Work finalized on branch {info.branch}")

    return {
        "branch": info.branch,
        "commit_count": info.commit_count,
        "commits": log_out,
        "diff_summary": diff_stat,
        "files_changed": [f.strip() for f in files_out.splitlines() if f.strip()],
    }


async def cleanup(agent_id: str) -> bool:
    """Remove workspace and prune worktree. Returns True if cleaned."""
    info = _workspaces.pop(agent_id, None)
    if not info:
        return False

    ws = info.path
    if ws.exists():
        # Remove worktree
        await _run(f'git worktree remove "{ws}" --force 2>/dev/null', cwd=_MAIN_REPO)
        # Fallback: if worktree remove fails, just delete the directory
        if ws.exists():
            shutil.rmtree(ws, ignore_errors=True)

    # Prune worktree list
    await _run("git worktree prune", cwd=_MAIN_REPO)

    info.status = "cleaned"
    emit_workspace(agent_id, "cleaned", "worktree removed")
    logger.info("Workspace cleaned: %s", agent_id)
    return True


def get_workspace(agent_id: str) -> WorkspaceInfo | None:
    """Get workspace info for an agent."""
    return _workspaces.get(agent_id)


def get_workspace_path(agent_id: str) -> Path | None:
    """Get the filesystem path for an agent's workspace."""
    info = _workspaces.get(agent_id)
    return info.path if info else None


def list_workspaces() -> list[WorkspaceInfo]:
    """List all active workspaces."""
    return list(_workspaces.values())
