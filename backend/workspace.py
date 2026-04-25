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
    repo_id: str | None = None  # credential registry ID (for multi-repo lookup)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    commit_count: int = 0
    status: str = "active"  # active | finalized | cleaned
    # R8 #314: anchor commit SHA captured immediately after `git worktree add`.
    # This is the immutable "clean anchor" — even if the agent commits new work
    # on its branch, retry recreates the worktree branched off this SHA, so the
    # reset target is always the start-of-task state. None on external clones
    # where HEAD-after-clone equals the source HEAD (still valid, but legacy
    # workspaces predating this field also serialise as None).
    anchor_sha: str | None = None


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
    # Sanitize IDs for safe use in branch names and shell commands
    import re as _re
    safe_agent = _re.sub(r'[^a-zA-Z0-9_-]', '_', agent_id)
    safe_task = _re.sub(r'[^a-zA-Z0-9_-]', '_', task_id)
    branch = f"agent/{safe_agent}/{safe_task}"
    ws_path = _WORKSPACES_ROOT / safe_agent

    # Clean up existing workspace if any
    if agent_id in _workspaces:
        await cleanup(agent_id)

    emit_pipeline_phase("workspace_provision", f"Creating workspace for {agent_id}")

    from backend.config import settings as _settings

    # Preventive environment checks (non-blocking warnings + hard disk check)
    try:
        from backend.permission_errors import check_environment
        env_issues = await check_environment(str(_WORKSPACES_ROOT))
        for issue in env_issues:
            level = "warn" if issue["status"] == "warning" else "error"
            emit_pipeline_phase(
                "env_check",
                f"[{issue['status'].upper()}] {issue['check']}: {issue['detail']}",
            )
            if issue["status"] in ("error", "critical"):
                try:
                    from backend.events import emit_token_warning
                    emit_token_warning(level, f"Environment: {issue['detail']}. {issue.get('suggestion', '')}")
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("Preventive env check failed (non-critical): %s", exc)

    # Hard disk space check (blocks provision)
    free_bytes = shutil.disk_usage(str(_WORKSPACES_ROOT)).free
    if free_bytes < 100 * 1024 * 1024:  # 100MB minimum
        raise RuntimeError(f"Insufficient disk space: {free_bytes // 1024 // 1024}MB free")

    source = repo_source or str(_MAIN_REPO)

    # Clean stale git lock before worktree operations.
    # Stale-lock guard: only remove if the lock is older than 60s — otherwise
    # another git process likely holds it. Yanking a fresh lock corrupts the
    # peer git's transaction.
    source_lock = Path(source) / ".git" / "index.lock"
    if source_lock.exists():
        try:
            import time as _t
            age = _t.time() - source_lock.stat().st_mtime
        except OSError:
            age = 0
        if age >= 60:
            try:
                source_lock.unlink()
                logger.warning("Removed stale git lock (%.0fs old): %s", age, source_lock)
            except OSError as exc:
                logger.warning("Failed to remove git lock %s: %s", source_lock, exc)
        else:
            logger.info("Skipping fresh git lock (%.0fs old, likely held): %s", age, source_lock)
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
            try:
                shutil.rmtree(ws_path)
            except OSError as exc:
                logger.warning("Failed to remove existing workspace %s: %s", ws_path, exc)
        rc, out, err = await _run(
            f'git worktree add "{ws_path}" "{branch}"',
            cwd=Path(source),
        )
        if rc != 0:
            raise RuntimeError(f"Failed to create worktree: {err or out}")

        logger.info("Workspace provisioned (worktree): %s → %s", agent_id, ws_path)
    else:
        # Clone external repo (with authentication)
        # Validate source URL to prevent shell injection
        if any(c in source for c in ('`', '$', ';', '|', '&', '\n')):
            raise ValueError(f"Invalid characters in repo source URL: {source}")
        from backend.git_auth import get_auth_env
        auth_env = get_auth_env(source)

        if ws_path.exists():
            try:
                shutil.rmtree(ws_path)
            except OSError as exc:
                logger.warning("Failed to remove existing workspace %s: %s", ws_path, exc)
        rc, out, err = await _run(f'git clone "{source}" "{ws_path}"', extra_env=auth_env)
        if rc != 0:
            raise RuntimeError(f"Failed to clone: {err or out}")
        # Create and checkout branch
        await _run(f'git checkout -b "{branch}"', cwd=ws_path)
        logger.info("Workspace provisioned (clone): %s → %s", agent_id, ws_path)

    # R8 #314: capture anchor commit SHA *before* any agent activity touches
    # the worktree. This is the immutable retry target — see
    # docs/design/r8-idempotent-retry-worktree.md §4. Done before user.name /
    # user.email config so we cannot accidentally drift the anchor by commits
    # that this provision path itself emits.
    rc, anchor_sha, _ = await _run("git rev-parse HEAD", cwd=ws_path)
    anchor_sha = anchor_sha.strip() if rc == 0 else ""
    if not anchor_sha:
        # Repo with no commits (rare; new clone with detached HEAD allowed).
        # We log and continue — anchor stays None and retry falls back to
        # legacy clean+checkout per the migration policy.
        logger.warning(
            "Could not resolve anchor commit SHA for %s (rc=%d); retry will fall back",
            agent_id, rc,
        )
        anchor_sha = None

    # Configure git user for this workspace. H9: agent_id reaches a shell
    # via _run() so use safe_agent (already sanitized to [A-Za-z0-9_-]).
    await _run(f'git config user.name "Agent-{safe_agent}"', cwd=ws_path)
    await _run(f'git config user.email "{safe_agent}@omnisight.local"', cwd=ws_path)

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

    # Resolve repo_id from credential registry
    _repo_id = None
    try:
        from backend.git_credentials import find_credential_for_url
        cred = find_credential_for_url(source)
        if cred:
            _repo_id = cred.get("id")
    except Exception:
        pass

    info = WorkspaceInfo(
        agent_id=agent_id,
        task_id=task_id,
        branch=branch,
        path=ws_path,
        repo_source=source,
        repo_id=_repo_id,
        anchor_sha=anchor_sha,
    )
    _workspaces[agent_id] = info

    emit_workspace(
        agent_id,
        "provisioned",
        f"branch={branch}, path={ws_path}, anchor={anchor_sha or 'none'}",
    )
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


async def discard_and_recreate(
    agent_id: str,
    anchor_sha: str,
    reason: str = "retry",
) -> WorkspaceInfo:
    """R8 #314 retry primitive — reset workspace to its anchor commit.

    Replaces the whitepaper §三.2 ``git clean -fd`` + ``git checkout .``
    recipe with a *fresh-from-anchor* worktree per
    ``docs/design/r8-idempotent-retry-worktree.md``: destroy the old
    worktree (``git worktree remove --force`` then ``shutil.rmtree``
    fallback so a half-removed dir cannot block the recreate), drop the
    branch ref, then ``git worktree add -b <branch> <ws_path>
    <anchor_sha>`` to materialise a brand-new working tree at the
    immutable retry target. Agent commits made past the anchor are
    abandoned by design — the whole point of retry is "back to the
    pristine starting state".

    Same logical path is reused (``info.path``) so the registry entry,
    SSE subscribers, and any path-bound consumers stay coherent across
    retry — only the working-tree contents change.

    Args:
        agent_id: Owner of an active workspace already in the registry.
        anchor_sha: Immutable commit SHA captured by ``provision()``
            (``WorkspaceInfo.anchor_sha``). Required; transitional
            legacy NULL-fallback per design §5 is the *caller's*
            responsibility, not this function's.
        reason: Free-form short label ("retry", "rollback", ...) —
            surfaced on the ``workspace.retried`` SSE event so
            operators can tell scheduled retries from operator-driven
            ChatOps rollbacks. Audit-log persistence is row 2874's
            scope, not this row's.

    Returns:
        Same ``WorkspaceInfo`` instance with ``commit_count`` reset to
        0 and ``status`` back to "active". ``path``, ``branch`` and
        ``anchor_sha`` are unchanged so callers' references stay valid.

    Raises:
        KeyError: ``agent_id`` not in the active workspace registry.
        ValueError: ``anchor_sha`` is empty/whitespace.
        RuntimeError: ``git worktree add`` failed (anchor SHA not in
            object store, branch ref still locked, disk full, or the
            old worktree dir could not be cleared and now blocks the
            recreate).
    """
    info = _workspaces.get(agent_id)
    if info is None:
        raise KeyError(f"No active workspace for {agent_id}")
    if not anchor_sha or not anchor_sha.strip():
        raise ValueError(
            f"anchor_sha required for discard_and_recreate({agent_id!r}); "
            "legacy CATC payloads predating R8 must use the transitional "
            "fallback path before calling (see r8 design doc §5)."
        )

    anchor_sha = anchor_sha.strip()
    ws_path = info.path
    branch = info.branch
    source = info.repo_source

    # Snapshot the old branch tip *before* destroying anything so
    # downstream callers (audit row 2874) get a meaningful before-state.
    # Failures to read are expected (worktree may already be broken —
    # that's why we're here) and are silently squashed.
    old_branch_tip = ""
    if ws_path.exists():
        rc, out, _ = await _run("git rev-parse HEAD 2>/dev/null", cwd=ws_path)
        if rc == 0:
            old_branch_tip = out.strip()

    is_local = (
        not source.startswith("http")
        and not source.startswith("ssh://")
        and not source.startswith("git@")
    )
    src_repo = Path(source) if is_local and Path(source).is_dir() else _MAIN_REPO

    emit_pipeline_phase(
        "workspace_recreate",
        f"Discarding {agent_id} workspace, recreating from anchor {anchor_sha[:12]} ({reason})",
    )

    # Step 1: ``git worktree remove --force`` is preferred — it not
    # only deletes the working tree but also drops the
    # ``.git/worktrees/<name>/`` admin block. Failures (worktree
    # metadata already gone, lockfile present, dir externally rm'd)
    # are tolerated; Step 2 covers the dir-still-exists case.
    if ws_path.exists():
        await _run(
            f'git worktree remove --force "{ws_path}" 2>/dev/null',
            cwd=src_repo,
        )

    # Step 2: rmtree fallback. If git refused or the dir was orphaned
    # (no admin block, plain dir), we still need it gone before
    # ``git worktree add`` can re-create at the same path. Best-effort
    # — if rmtree itself fails the next step's add will surface a
    # clean RuntimeError instead of us masking it here.
    if ws_path.exists():
        try:
            shutil.rmtree(ws_path)
        except OSError as exc:
            logger.warning(
                "discard_and_recreate: rmtree fallback failed for %s: %s",
                ws_path, exc,
            )

    # Step 3: prune dangling ``.git/worktrees/`` admin entries that
    # don't have a working tree on disk anymore (covers the case where
    # someone rm -rf'd the workspace dir but git still thinks it
    # exists).
    await _run("git worktree prune", cwd=src_repo)

    # Step 4: drop the agent branch in the source repo. Without this
    # ``git worktree add -b <branch>`` would refuse with "already
    # exists". Agent commits past the anchor are abandoned by design;
    # they remain unreachable in the object store until ``git gc``.
    await _run(f'git branch -D "{branch}" 2>/dev/null', cwd=src_repo)

    # Step 5: materialise the fresh worktree branched at anchor_sha.
    if is_local and src_repo.is_dir():
        rc, out, err = await _run(
            f'git worktree add -b "{branch}" "{ws_path}" "{anchor_sha}"',
            cwd=src_repo,
        )
        if rc != 0:
            raise RuntimeError(
                f"discard_and_recreate({agent_id!r}): worktree add from "
                f"anchor {anchor_sha[:12]} failed: {err or out}"
            )
    else:
        # External clone path — re-clone, then check out the anchor on
        # the agent branch. Anchor was the post-clone HEAD originally,
        # so it is normally still reachable from origin/HEAD; ``fetch
        # origin <sha>`` is a defensive no-op when already present.
        if any(c in source for c in ('`', '$', ';', '|', '&', '\n')):
            raise ValueError(f"Invalid characters in repo source URL: {source}")
        from backend.git_auth import get_auth_env
        auth_env = get_auth_env(source)
        rc, out, err = await _run(
            f'git clone "{source}" "{ws_path}"', extra_env=auth_env,
        )
        if rc != 0:
            raise RuntimeError(
                f"discard_and_recreate({agent_id!r}): re-clone failed: "
                f"{err or out}"
            )
        await _run(
            f'git fetch origin "{anchor_sha}" 2>/dev/null || true',
            cwd=ws_path, extra_env=auth_env,
        )
        rc, out, err = await _run(
            f'git checkout -b "{branch}" "{anchor_sha}"', cwd=ws_path,
        )
        if rc != 0:
            raise RuntimeError(
                f"discard_and_recreate({agent_id!r}): checkout anchor "
                f"{anchor_sha[:12]} failed: {err or out}"
            )

    # Restore the per-workspace git identity that ``provision()`` set up.
    # Without this an immediate ``git commit`` from the agent would
    # inherit the host's global identity (or fail under strict-ident
    # mode). H9 sanitisation: agent_id reaches a shell so use safe_agent.
    import re as _re
    safe_agent = _re.sub(r'[^a-zA-Z0-9_-]', '_', agent_id)
    await _run(f'git config user.name "Agent-{safe_agent}"', cwd=ws_path)
    await _run(f'git config user.email "{safe_agent}@omnisight.local"', cwd=ws_path)

    # Restore the ``/test_assets/`` gitignore line so accidental
    # ``git add -A`` over a sandbox bind-mount doesn't try to track
    # the read-only ground-truth tree (CLAUDE.md Safety Rule).
    gitignore = ws_path / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    additions = [e for e in ["/test_assets/"] if e not in existing]
    if additions:
        with open(gitignore, "a") as f:
            f.write("\n".join([""] + additions + [""]))

    # Registry update — same path/branch/anchor, fresh contents. The
    # agent's prior commit_count is gone with the discarded branch.
    info.commit_count = 0
    info.status = "active"

    emit_workspace(
        agent_id,
        "retried",
        f"branch={branch}, anchor={anchor_sha[:12]}, "
        f"old_tip={old_branch_tip[:12] or 'none'}, reason={reason}",
    )
    emit_agent_update(
        agent_id, "running",
        f"Workspace recreated from anchor {anchor_sha[:12]}",
    )
    logger.info(
        "Workspace discarded+recreated: %s → %s (anchor=%s, reason=%s)",
        agent_id, ws_path, anchor_sha[:12], reason,
    )
    return info


async def cleanup_stale_locks():
    """Remove .git/index.lock files left from interrupted operations.

    Only locks older than 60 seconds are considered stale. Fresh locks are
    likely held by an active git process and yanking them causes corruption.
    """
    import time as _t

    def _maybe_unlink(p: Path) -> None:
        if not p.exists():
            return
        try:
            age = _t.time() - p.stat().st_mtime
        except OSError:
            return
        if age < 60:
            logger.debug("Skipping fresh lock (%.0fs): %s", age, p)
            return
        try:
            p.unlink()
            logger.warning("Removed stale lock (%.0fs old): %s", age, p)
        except OSError as exc:
            logger.warning("Failed to remove lock %s: %s", p, exc)

    _maybe_unlink(_MAIN_REPO / ".git" / "index.lock")
    if _WORKSPACES_ROOT.exists():
        for ws_dir in _WORKSPACES_ROOT.iterdir():
            if ws_dir.is_dir():
                for lock_name in ("index.lock", "HEAD.lock"):
                    _maybe_unlink(ws_dir / ".git" / lock_name)


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
            try:
                shutil.rmtree(ws)
            except OSError as exc:
                logger.warning("Failed to remove workspace dir %s: %s", ws, exc)

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
# Ordered: longer extensions first to prevent partial matches
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
    seen_paths: set[Path] = set()  # Deduplicate across overlapping build dirs

    for build_dir_name in _BUILD_OUTPUT_DIRS:
        build_dir = workspace / build_dir_name
        if not build_dir.is_dir():
            continue

        for fpath in build_dir.rglob("*"):
            # Skip files already collected from a more specific build dir
            resolved = fpath.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
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

            artifact_id = f"art-{uuid.uuid4().hex[:12]}"
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
                # SP-3.6a: worker context (agent finalize pipeline) —
                # acquire pool conn for the single insert.
                from backend import db
                from backend.db_pool import get_pool
                async with get_pool().acquire() as _conn:
                    await db.insert_artifact(_conn, artifact_data)
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


    return collected
