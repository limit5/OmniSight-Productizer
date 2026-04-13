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
import os
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


async def _run(cmd: str, cwd: Path | None = None, extra_env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run a shell command, return (returncode, stdout, stderr)."""
    env = None
    if extra_env:
        env = {**os.environ, **extra_env}
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd or _MAIN_REPO,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
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

    from backend.config import settings as _settings

    # Disk space check
    free_bytes = shutil.disk_usage(str(_WORKSPACES_ROOT)).free
    if free_bytes < 100 * 1024 * 1024:  # 100MB minimum
        raise RuntimeError(f"Insufficient disk space: {free_bytes // 1024 // 1024}MB free")

    source = repo_source or str(_MAIN_REPO)

    # Clean stale git lock before worktree operations
    source_lock = Path(source) / ".git" / "index.lock"
    if source_lock.exists():
        source_lock.unlink()
        logger.warning("Removed stale git lock before provision: %s", source_lock)
    is_local = not source.startswith("http") and not source.startswith("ssh://") and not source.startswith("git@")

    # Gerrit mode: use fresh clone even for local repos (full isolation)
    if _settings.gerrit_enabled and not repo_source:
        gerrit_url = f"ssh://{_settings.gerrit_ssh_host}:{_settings.gerrit_ssh_port}/{_settings.gerrit_project}"
        source = gerrit_url
        is_local = False

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
        # Clone external repo (with authentication)
        from backend.git_auth import get_auth_env
        auth_env = get_auth_env(source)

        if ws_path.exists():
            shutil.rmtree(ws_path, ignore_errors=True)
        rc, out, err = await _run(f'git clone "{source}" "{ws_path}"', extra_env=auth_env)
        if rc != 0:
            raise RuntimeError(f"Failed to clone: {err or out}")
        # Create and checkout branch
        await _run(f'git checkout -b "{branch}"', cwd=ws_path)
        logger.info("Workspace provisioned (clone): %s → %s", agent_id, ws_path)

    # Configure git user for this workspace
    await _run(f'git config user.name "Agent-{agent_id}"', cwd=ws_path)
    await _run(f'git config user.email "{agent_id}@omnisight.local"', cwd=ws_path)

    # Ensure :ro bind-mount directories are gitignored (prevents git add -A issues)
    gitignore = ws_path / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    additions = [e for e in ["/test_assets/"] if e not in existing]
    if additions:
        with open(gitignore, "a") as f:
            f.write("\n".join([""] + additions + [""]))
        logger.debug("Added %s to .gitignore in %s", additions, ws_path)

    # Write platform hint for container SDK mount detection
    omnisight_dir = ws_path / ".omnisight"
    omnisight_dir.mkdir(exist_ok=True)
    try:
        # Read target platform from hardware_manifest if available
        manifest = _MAIN_REPO / "configs" / "hardware_manifest.yaml"
        if manifest.is_file():
            import yaml
            mdata = yaml.safe_load(manifest.read_text()) or {}
            platform = mdata.get("vendor", {}).get("platform_profile", "") or \
                       mdata.get("project", {}).get("target_platform", "aarch64")
            if platform:
                (omnisight_dir / "platform").write_text(platform)
    except Exception:
        pass

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

    # Detect base branch for diff
    base = await _detect_base_branch(ws)

    # Pre-merge conflict check: test if branch can merge cleanly into base
    conflict_files: list[str] = []
    # Verify base branch exists before attempting merge test
    rc_check, _, _ = await _run(f'git rev-parse --verify {base} 2>/dev/null', cwd=ws)
    if rc_check == 0:
        rc_merge, merge_out, _ = await _run(
            f'git merge --no-commit --no-ff {base} 2>&1 || true', cwd=ws,
        )
        if "CONFLICT" in (merge_out or ""):
            for line in merge_out.splitlines():
                if "CONFLICT" in line and ":" in line:
                    conflict_files.append(line.split(":")[-1].strip())
            logger.warning("Merge conflict detected for agent %s: %s", agent_id, conflict_files)
        # Always abort test merge
        await _run("git merge --abort 2>/dev/null || true", cwd=ws)

    # Generate summary
    _, log_out, _ = await _run(
        f"git log --oneline {base}..{info.branch} 2>/dev/null || git log --oneline -5",
        cwd=ws,
    )
    _, diff_stat, _ = await _run(
        f"git diff --stat {base}..{info.branch} 2>/dev/null || git diff --stat HEAD~1",
        cwd=ws,
    )
    _, files_out, _ = await _run(
        f"git diff --name-only {base}..{info.branch} 2>/dev/null || git diff --name-only HEAD~1",
        cwd=ws,
    )

    info.status = "finalized"
    files_changed = [f.strip() for f in files_out.splitlines() if f.strip()]
    emit_workspace(agent_id, "finalized", f"{info.commit_count} commit(s), {len(files_changed)} file(s)")
    emit_agent_update(agent_id, "success", f"Work finalized on branch {info.branch}")

    result = {
        "branch": info.branch,
        "commit_count": info.commit_count,
        "commits": log_out,
        "diff_summary": diff_stat,
        "files_changed": files_changed,
        "conflict_files": conflict_files,
    }

    # Auto-generate handoff document
    try:
        from backend.handoff import generate_handoff, save_handoff
        handoff_content = generate_handoff(
            agent_id=agent_id,
            task_id=info.task_id,
            finalize_result=result,
        )
        await save_handoff(agent_id, info.task_id, handoff_content, workspace_path=ws)
        result["handoff"] = handoff_content
    except Exception as exc:
        logger.warning("Handoff generation failed: %s", exc)

    # Collect build artifacts before cleanup destroys the workspace
    try:
        collected = await _collect_build_artifacts(ws, agent_id, info.task_id)
        if collected:
            result["artifacts"] = collected
            logger.info("Collected %d build artifact(s) from %s", len(collected), ws)
    except Exception as exc:
        logger.warning("Artifact collection failed: %s", exc)

    return result


async def cleanup_stale_locks():
    """Remove .git/index.lock files left from interrupted operations."""
    # Main repo
    main_lock = _MAIN_REPO / ".git" / "index.lock"
    if main_lock.exists():
        main_lock.unlink()
        logger.warning("Removed stale git lock: %s", main_lock)
    # Agent workspaces
    if _WORKSPACES_ROOT.exists():
        for ws_dir in _WORKSPACES_ROOT.iterdir():
            if ws_dir.is_dir():
                for lock_name in ("index.lock", "HEAD.lock"):
                    lock = ws_dir / ".git" / lock_name
                    if lock.exists():
                        lock.unlink()
                        logger.warning("Removed stale lock: %s", lock)


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


async def _detect_default_remote(repo_path: Path) -> str:
    """Detect the primary remote (prefer 'origin', fallback to first)."""
    rc, out, _ = await _run("git remote", cwd=repo_path)
    if rc != 0 or not out.strip():
        return "origin"
    remotes = [r.strip() for r in out.splitlines() if r.strip()]
    return "origin" if "origin" in remotes else (remotes[0] if remotes else "origin")


async def _detect_base_branch(repo_path: Path) -> str:
    """Detect the default branch (main, master, develop, etc.)."""
    remote = await _detect_default_remote(repo_path)
    # Try symbolic-ref (most reliable)
    rc, out, _ = await _run(
        f"git symbolic-ref refs/remotes/{remote}/HEAD 2>/dev/null", cwd=repo_path,
    )
    if rc == 0 and out.strip():
        return out.strip().split("/")[-1]
    # Fallback: check common names
    for candidate in ("main", "master", "develop"):
        rc, _, _ = await _run(f"git rev-parse --verify {candidate} 2>/dev/null", cwd=repo_path)
        if rc == 0:
            return candidate
    return "main"


def list_workspaces() -> list[WorkspaceInfo]:
    """List all active workspaces."""
    return list(_workspaces.values())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Build artifact collection (Phase 39)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Directories to scan for build outputs (relative to workspace root)
_BUILD_OUTPUT_DIRS = ["build/output", "build/bin", "build", "out", "dist"]

# File extensions → ArtifactType mapping
_ARTIFACT_TYPE_MAP = {
    ".ko": "kernel_module", ".bin": "firmware", ".hex": "firmware",
    ".elf": "binary", ".so": "binary", ".a": "binary", ".o": "binary",
    ".rknn": "model", ".tflite": "model", ".engine": "model", ".onnx": "model",
    ".tar.gz": "archive", ".tgz": "archive", ".zip": "archive",
    ".deb": "sdk", ".rpm": "sdk",
    ".pdf": "pdf", ".html": "html", ".md": "markdown", ".log": "log",
}


def _guess_artifact_type(filename: str) -> str:
    """Guess artifact type from file extension."""
    name_lower = filename.lower()
    for ext, atype in _ARTIFACT_TYPE_MAP.items():
        if name_lower.endswith(ext):
            return atype
    return "binary"


async def _collect_build_artifacts(
    workspace: Path, agent_id: str, task_id: str | None,
) -> list[dict]:
    """Scan workspace for build outputs and copy them to .artifacts/.

    Returns list of artifact metadata dicts that were registered.
    """
    import hashlib
    import uuid
    from datetime import datetime

    from backend.routers.artifacts import get_artifacts_root

    artifacts_root = get_artifacts_root()
    task_dir = artifacts_root / (task_id or "general")
    task_dir.mkdir(parents=True, exist_ok=True)

    collected = []

    for build_dir_name in _BUILD_OUTPUT_DIRS:
        build_dir = workspace / build_dir_name
        if not build_dir.is_dir():
            continue

        for fpath in build_dir.rglob("*"):
            if not fpath.is_file():
                continue
            # Skip common non-artifact files
            if fpath.name in (".gitkeep", ".gitignore", "CMakeCache.txt", "Makefile"):
                continue

            try:
                # Skip tiny files (< 10 bytes — likely empty placeholders)
                if fpath.stat().st_size < 10:
                    continue

                # Compute checksum
                sha = hashlib.sha256()
                with open(fpath, "rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        sha.update(chunk)
                checksum = sha.hexdigest()

                # Copy to .artifacts/
                dest = task_dir / fpath.name
                # Avoid collision
                if dest.exists():
                    stem = dest.stem
                    suffix = dest.suffix
                    dest = task_dir / f"{stem}_{uuid.uuid4().hex[:4]}{suffix}"
                shutil.copy2(fpath, dest)
            except (FileNotFoundError, OSError) as exc:
                logger.debug("Artifact collection skipped %s: %s", fpath.name, exc)
                continue

            artifact_id = f"art-{uuid.uuid4().hex[:8]}"
            artifact_data = {
                "id": artifact_id,
                "task_id": task_id or "",
                "agent_id": agent_id,
                "name": fpath.name,
                "type": _guess_artifact_type(fpath.name),
                "file_path": str(dest),
                "size": dest.stat().st_size,
                "created_at": datetime.now().isoformat(),
                "version": "",
                "checksum": checksum,
            }

            try:
                from backend import db
                await db.insert_artifact(artifact_data)
            except Exception as exc:
                logger.warning("Failed to register artifact %s: %s", fpath.name, exc)
                continue

            # Emit SSE event
            try:
                from backend.events import bus
                bus.publish("artifact_created", {
                    "id": artifact_id,
                    "name": fpath.name,
                    "type": artifact_data["type"],
                    "task_id": task_id or "",
                    "agent_id": agent_id,
                    "size": artifact_data["size"],
                })
            except Exception:
                pass

            collected.append(artifact_data)
            logger.info("Artifact collected: %s (%d bytes, %s)", fpath.name, artifact_data["size"], artifact_data["type"])

        # Only scan the first existing build dir
        break

    return collected
