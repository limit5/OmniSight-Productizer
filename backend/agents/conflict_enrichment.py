"""OP-717 — local 3-way merge to extract conflict markers + context.

The OP-715 daemon spawns a proactive merger thread on every
patchset-created event. To actually resolve the conflict (rather than
just file an abstain JIRA ticket), the merger needs the real
``<<<<<<<`` / ``=======`` / ``>>>>>>>`` markers from the conflicted
files plus surrounding context. Gerrit's REST API does not expose
this — it only reports ``mergeable: bool``. So we compute the markers
ourselves by attempting the merge in a local working clone.

This module sidesteps the ``_fetch_mergeable`` REST auth gap (OP-716):
a successful local merge with no conflicts is itself proof of
``mergeable=true``, and a merge that produces markers is proof of
``mergeable=false`` *plus* gives us the markers for the merger LLM.

Working clone strategy
----------------------

Lazy-init a single bare-of-checkouts clone per project at
``/tmp/op717-merger-work/<safe_project>/repo``. On first call:
``git clone <gerrit-ssh-url> repo``. On every call:

    git fetch origin develop
    git fetch origin refs/changes/<NN>/<change>/<ps>:refs/op717-attempt
    git checkout origin/develop
    git merge --no-commit --no-ff refs/op717-attempt

If the merge succeeds without conflicts: clean — return mergeable=True.
If conflicts: read each conflicted file (already on disk with the
markers), build :class:`ConflictFile` per file, then ``git merge --abort``
to leave the worktree clean for the next caller.

A per-project ``asyncio.Lock`` serialises calls so two concurrent
patchset events don't race against the same checkout. Each call's
total wall-time is bounded by the SSH fetch + merge time (typically
2-10 sec on a small repo, capped by ``_MERGE_TIMEOUT_SEC``).

Caps
----

If the merge produces more than ``_MAX_CONFLICT_FILES`` (5)
conflicted files, the result still reports mergeable=False but the
caller is expected to skip merger invocation — multi-file LLM
resolution is out of scope for the MVP and would burn cost on a
likely-irrelevant resolution.

Binary files / submodule conflicts are reported as conflict_files
entries with ``binary=True`` (caller decides how to surface). The
merger will refuse_no_conflict on binary content per its own logic.

SSH auth
--------

Reuses the same SSH key + user pattern the OP-715 daemon uses:
``OMNISIGHT_GIT_SSH_KEY_PATH`` env var + ``OMNISIGHT_GERRIT_SSH_HOST``
(of the form ``<user>@<host>``). No HTTP / LDAP creds needed —
purely SSH-protocol auth.
"""

from __future__ import annotations

import asyncio
import ast
import contextlib
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from backend.config import settings

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Knobs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_WORK_ROOT = Path(os.environ.get(
    "OMNISIGHT_OP717_WORK_ROOT", "/tmp/op717-merger-work",
)).expanduser().resolve()

_MERGE_TIMEOUT_SEC = 120         # whole-process budget per enrichment call
_GIT_TIMEOUT_SEC = 60            # per individual git subprocess
_MAX_CONFLICT_FILES = 5          # cap before we give up + skip merger
_CONTEXT_LINES = 20              # surrounding lines for file_context
_FILE_CONTENT_CAP = 200_000      # bytes — guard against giant blobs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class ConflictFile:
    path: str
    conflict_text: str           # full file content with <<<<<<< ... markers
    file_context: str            # 20 lines around the first conflict block
    binary: bool = False


@dataclass
class EnrichmentResult:
    """Outcome of an enrichment attempt."""
    mergeable: bool                       # True iff merge clean (no markers)
    conflict_files: list[ConflictFile] = field(default_factory=list)
    too_many: bool = False                # True iff len(conflict_files) > cap
    error: str = ""                       # non-empty if enrichment itself blew up
    head_subject: str = ""                # develop tip commit subject
    incoming_subject: str = ""            # incoming patchset commit subject
    sibling_file_contents: dict[str, str] = field(default_factory=dict)
    git_logs: dict[str, str] = field(default_factory=dict)
    symbol_table: dict[str, str] = field(default_factory=dict)


# Per-project locks so two concurrent calls on the same project
# don't fight over the same git checkout. Process-local; daemon is
# single-process.
#
# OP-1197 (2026-05-17 ~04:30) — switched from `asyncio.Lock` to
# `threading.Lock`. The bridge daemon's per-event handler threads
# each call ``asyncio.run(...)`` which creates a fresh event loop.
# ``asyncio.Lock`` objects retain a binding to whichever event loop
# they were FIRST acquired in; subsequent acquire from a different
# loop raises ``RuntimeError: <asyncio.locks.Lock ...> is bound to
# a different event loop``. The OP-1196 phase 3b backfill scanner
# triggered this on every backfilled candidate because it spawns N
# threads in rapid succession at startup — empirically observed on
# 2026-05-17 ~03:23 across all 10 backfilled candidates (685/686/
# 694/695/696/697/698/699/700/702). Live patchset-created events
# hit the same bug less often (only when two events arrive close
# enough that the first event's loop is still active) — the bug was
# always latent.
#
# ``threading.Lock`` is loop-agnostic. The critical section the lock
# protects is subprocess calls (git clone/fetch/merge/abort) which
# already block the calling thread anyway — the async-ness of
# ``asyncio.Lock`` added no concurrency benefit. We acquire the
# threading.Lock via ``asyncio.to_thread`` so the surrounding code
# stays async-friendly and the event loop isn't blocked while
# waiting for the lock (to_thread offloads the blocking acquire to
# the default executor).
_LOCKS: dict[str, "threading.Lock"] = {}
_LOCKS_GUARD = threading.Lock()


async def _lock_for(project: str) -> "threading.Lock":
    # threading.Lock acquire is microseconds-fast with no contention;
    # the GUARD just serialises the dict mutation. We acquire via
    # to_thread so the call is non-blocking from the loop's pov.
    await asyncio.to_thread(_LOCKS_GUARD.acquire)
    try:
        if project not in _LOCKS:
            _LOCKS[project] = threading.Lock()
        return _LOCKS[project]
    finally:
        _LOCKS_GUARD.release()


@contextlib.asynccontextmanager
async def _project_lock(project: str):
    """``async with _project_lock(project):`` — serialises enrich
    callers per-project across threads + event loops. Built atop
    ``threading.Lock`` so it works correctly when callers use
    ``asyncio.run`` per-thread (each thread has its own loop, but
    threading.Lock is shared)."""
    lock = await _lock_for(project)
    await asyncio.to_thread(lock.acquire)
    try:
        yield
    finally:
        lock.release()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def enrich_via_local_merge(
    change_number: int | str,
    patchset_revision: str,
    project: str,
    patchset_number: int | str = 0,
) -> EnrichmentResult:
    """Try to merge patchset_revision against origin/develop locally
    and return per-file conflict markers.

    Never raises. Returns ``EnrichmentResult(error=...)`` on any
    infrastructure failure (clone, fetch, git command). The caller
    is expected to log the error and skip the merger invocation.
    """
    if not change_number or not patchset_revision or not project:
        return EnrichmentResult(
            mergeable=False, error="missing change_number/revision/project",
        )

    try:
        return await asyncio.wait_for(
            _run(str(change_number), patchset_revision, project,
                 str(patchset_number) if patchset_number else "0"),
            timeout=_MERGE_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        return EnrichmentResult(
            mergeable=False,
            error=f"enrichment timeout after {_MERGE_TIMEOUT_SEC}s",
        )
    except Exception as exc:  # pragma: no cover — final safety net
        logger.exception("conflict_enrichment unhandled: %s", exc)
        return EnrichmentResult(
            mergeable=False, error=f"{type(exc).__name__}: {exc}",
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Implementation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _run(
    change_id: str, revision: str, project: str, ps_number: str,
) -> EnrichmentResult:
    async with _project_lock(project):
        repo_dir = _repo_dir_for(project)
        ssh_url = _ssh_url_for(project)
        ssh_command = _ssh_cmd_string()

        # Step 1: ensure clone exists
        if not (repo_dir / ".git").exists():
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            rc, _, err = await _run_git(
                None, ["git", "-c", f"core.sshCommand={ssh_command}",
                       "clone", "--quiet", ssh_url, str(repo_dir)],
            )
            if rc != 0:
                return EnrichmentResult(
                    mergeable=False, error=f"clone failed: {err[:300]}",
                )

        # Step 2: fetch develop + the patchset
        attempt_ref = f"refs/op717-attempt-{ps_number}"
        ps_ref = f"refs/changes/{change_id[-2:].zfill(2)}/{change_id}/{ps_number}"
        # Gerrit refs/changes layout: refs/changes/NN/CHANGE/PS where NN = last 2 digits of CHANGE.
        # We only have change_id (number) — fall back to fetch-by-revision if PS unknown.
        rc, _, err = await _run_git(
            repo_dir,
            ["git", "-c", f"core.sshCommand={ssh_command}", "fetch",
             "--quiet", "origin", "develop"],
        )
        if rc != 0:
            return EnrichmentResult(
                mergeable=False, error=f"fetch develop failed: {err[:300]}",
            )

        # Try fetching the specific PS ref. If that fails (e.g. ps_number=0
        # because caller didn't know it), fetch the change directly via
        # the revision SHA which Gerrit always serves on the remote.
        if ps_number and ps_number != "0":
            rc, _, err = await _run_git(
                repo_dir,
                ["git", "-c", f"core.sshCommand={ssh_command}", "fetch",
                 "--quiet", "origin", f"{ps_ref}:{attempt_ref}"],
            )
            if rc != 0:
                # Fall through to revision-direct fetch
                ps_number = "0"

        if not ps_number or ps_number == "0":
            rc, _, err = await _run_git(
                repo_dir,
                ["git", "-c", f"core.sshCommand={ssh_command}", "fetch",
                 "--quiet", "origin", revision],
            )
            if rc != 0:
                return EnrichmentResult(
                    mergeable=False,
                    error=f"fetch revision failed: {err[:300]}",
                )
            # Use FETCH_HEAD as the merge target.
            attempt_ref = "FETCH_HEAD"

        # Step 3: aggressively reset to develop tip (clean slate)
        await _run_git(repo_dir, ["git", "merge", "--abort"])  # if leftover
        await _run_git(repo_dir, ["git", "reset", "--hard", "HEAD"])
        rc, _, err = await _run_git(
            repo_dir,
            ["git", "checkout", "--quiet", "-B", "op717-base", "origin/develop"],
        )
        if rc != 0:
            return EnrichmentResult(
                mergeable=False, error=f"checkout develop failed: {err[:300]}",
            )

        # Capture commit subjects for the MergeConflictTask context
        head_subject = await _capture(
            repo_dir, ["git", "log", "-1", "--format=%s", "origin/develop"],
        )
        incoming_subject = await _capture(
            repo_dir, ["git", "log", "-1", "--format=%s", attempt_ref],
        )

        # Step 4: attempt merge
        rc, _, _ = await _run_git(
            repo_dir,
            ["git", "merge", "--no-commit", "--no-ff", attempt_ref],
        )

        if rc == 0:
            # Clean merge — bail out, mergeable=True
            await _run_git(repo_dir, ["git", "merge", "--abort"])
            return EnrichmentResult(
                mergeable=True,
                head_subject=head_subject,
                incoming_subject=incoming_subject,
            )

        # Step 5: enumerate conflicted files
        conflicted = await _capture_lines(
            repo_dir, ["git", "diff", "--name-only", "--diff-filter=U"],
        )
        conflicted = sorted(set(p for p in conflicted if p))

        if len(conflicted) > _MAX_CONFLICT_FILES:
            await _run_git(repo_dir, ["git", "merge", "--abort"])
            return EnrichmentResult(
                mergeable=False, too_many=True,
                head_subject=head_subject, incoming_subject=incoming_subject,
                conflict_files=[
                    ConflictFile(path=p, conflict_text="", file_context="")
                    for p in conflicted
                ],
            )

        changed = await _capture_lines(
            repo_dir,
            ["git", "diff", "--name-only", "origin/develop", attempt_ref],
        )
        changed = sorted(set(p for p in changed if p))

        # Step 6: read each conflict file's content + extract context
        files: list[ConflictFile] = []
        for rel in conflicted:
            cf = await _read_conflict_file(repo_dir, rel)
            if cf is not None:
                files.append(cf)
        sibling_file_contents: dict[str, str] = {}
        for rel in changed:
            if rel in conflicted:
                continue
            text = await _read_text_file(repo_dir, rel)
            if text:
                sibling_file_contents[rel] = text
        git_logs = {
            rel: await _git_log_for_conflict_region(repo_dir, rel, cf.conflict_text)
            for rel, cf in ((f.path, f) for f in files)
        }
        symbol_table = {
            rel: await _symbol_table_for_develop(repo_dir, rel)
            for rel in sorted(set([*conflicted, *changed]))
        }
        symbol_table = {k: v for k, v in symbol_table.items() if v}

        # Step 7: cleanup
        await _run_git(repo_dir, ["git", "merge", "--abort"])

        return EnrichmentResult(
            mergeable=False,
            conflict_files=files,
            head_subject=head_subject,
            incoming_subject=incoming_subject,
            sibling_file_contents=sibling_file_contents,
            git_logs=git_logs,
            symbol_table=symbol_table,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _repo_dir_for(project: str) -> Path:
    """Map ``omnisight/OmniSight-Productizer`` →
    ``/tmp/op717-merger-work/omnisight_OmniSight-Productizer/repo``.
    Slashes / colons / spaces all flattened to underscores."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", project)
    return _WORK_ROOT / safe / "repo"


def _ssh_url_for(project: str) -> str:
    """Build the gerrit SSH URL using the same env knobs the OP-715
    daemon uses (``OMNISIGHT_GERRIT_SSH_HOST`` of the form
    ``user@host`` or just ``host``, ``OMNISIGHT_GERRIT_SSH_PORT``
    optional)."""
    host = settings.gerrit_ssh_host or os.environ.get(
        "OMNISIGHT_GERRIT_SSH_HOST", "",
    )
    port = (
        settings.gerrit_ssh_port
        or int(os.environ.get("OMNISIGHT_GERRIT_SSH_PORT", "29418"))
    )
    return f"ssh://{host}:{port}/{project}"


def _ssh_cmd_string() -> str:
    """Build the GIT_SSH_COMMAND-equivalent string used via
    ``git -c core.sshCommand=...``. Pins the ed25519 key from
    ``OMNISIGHT_GIT_SSH_KEY_PATH`` (or the legacy default)."""
    key = (
        settings.git_ssh_key_path
        or os.environ.get("OMNISIGHT_GIT_SSH_KEY_PATH", "")
    )
    key_path = Path(key).expanduser() if key else None
    parts = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
    ]
    if key_path:
        parts.extend(["-i", str(key_path)])
    return " ".join(parts)


async def _run_git(
    cwd: Path | None, argv: list[str],
) -> tuple[int, str, str]:
    """Run a git subprocess, return (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_GIT_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"git timeout: {' '.join(argv[:4])}..."
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def _capture(cwd: Path, argv: list[str]) -> str:
    rc, out, _ = await _run_git(cwd, argv)
    return out if rc == 0 else ""


async def _capture_lines(cwd: Path, argv: list[str]) -> list[str]:
    out = await _capture(cwd, argv)
    return [l for l in out.splitlines() if l.strip()]


_BINARY_HINT = re.compile(rb"\x00")


async def _read_conflict_file(
    repo_dir: Path, rel_path: str,
) -> ConflictFile | None:
    """Read the working-tree copy of a conflicted file and slice out a
    20-line context window around the first ``<<<<<<<`` marker."""
    abs_path = repo_dir / rel_path
    try:
        raw = abs_path.read_bytes()
    except OSError:
        return None
    if len(raw) > _FILE_CONTENT_CAP:
        return ConflictFile(
            path=rel_path,
            conflict_text="<file too large to inline>",
            file_context="",
            binary=False,
        )
    if _BINARY_HINT.search(raw[:8192]):
        return ConflictFile(
            path=rel_path, conflict_text="<binary file>",
            file_context="", binary=True,
        )

    text = raw.decode("utf-8", errors="replace")
    context = _slice_context(text, _CONTEXT_LINES)
    return ConflictFile(
        path=rel_path,
        conflict_text=text,
        file_context=context,
    )


async def _read_text_file(repo_dir: Path, rel_path: str) -> str:
    abs_path = repo_dir / rel_path
    try:
        raw = abs_path.read_bytes()
    except OSError:
        return ""
    if len(raw) > _FILE_CONTENT_CAP:
        return "<file too large to inline>"
    if _BINARY_HINT.search(raw[:8192]):
        return "<binary file>"
    return raw.decode("utf-8", errors="replace")


async def _git_log_for_conflict_region(
    repo_dir: Path, rel_path: str, conflict_text: str,
) -> str:
    lines = conflict_text.splitlines()
    marker_idx = next(
        (
            idx for idx, line in enumerate(lines, start=1)
            if line.startswith("<<<<<<<")
        ),
        1,
    )
    start = max(1, marker_idx - 50)
    end = min(len(lines) or start, marker_idx + 50)
    commands = [
        ["git", "log", "--follow", "-n", "5", f"-L{start},{end}:{rel_path}"],
        ["git", "log", "-p", "--follow", "-n", "5", "--", rel_path],
    ]
    for cmd in commands:
        out = await _capture(repo_dir, cmd)
        if out.strip():
            return out.strip()
    return ""


async def _symbol_table_for_develop(repo_dir: Path, rel_path: str) -> str:
    if not rel_path.endswith(".py"):
        return ""
    content = await _capture(
        repo_dir, ["git", "show", f"origin/develop:{rel_path}"],
    )
    if not content.strip():
        return ""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return ""
    lines: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            continue
        calls: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                fn = child.func
                if isinstance(fn, ast.Name):
                    calls.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    calls.add(fn.attr)
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        lines.append(
            f"{kind} {node.name} line {node.lineno} calls: "
            f"{', '.join(sorted(calls)) if calls else '(none)'}"
        )
    return "\n".join(lines)


def _slice_context(text: str, n_lines: int) -> str:
    """Pull a window of ``n_lines`` around the first ``<<<<<<<`` marker.

    If no marker found (defensive — shouldn't happen if the file is
    actually conflicted), return the first ``n_lines`` lines.
    """
    lines = text.splitlines()
    marker_idx = next(
        (i for i, l in enumerate(lines) if l.startswith("<<<<<<<")),
        None,
    )
    if marker_idx is None:
        return "\n".join(lines[: n_lines * 2])
    start = max(0, marker_idx - n_lines)
    end = min(len(lines), marker_idx + n_lines * 3)
    return "\n".join(lines[start:end])


__all__ = [
    "ConflictFile",
    "EnrichmentResult",
    "enrich_via_local_merge",
]
