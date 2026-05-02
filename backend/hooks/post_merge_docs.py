"""BP.J.2 — git ``post-merge`` hook for the self-healing docs watchdog.

Runs after every successful ``git merge`` (which includes ``git pull``
when the pull resolves to a merge or fast-forward). Calls
:mod:`backend.self_healing_docs` in ``--strict`` mode so a developer who
pulls in a branch that adds / modifies routes automatically sees their
working-copy ``openapi.json`` and ``docs/architecture.md`` refreshed,
**without** the hook silently overwriting either file when the merge
brings in *breaking* changes (removed routes / removed schemas) — those
need an explicit operator ack.

Why a separate file (not just calling the module directly from
``.git/hooks/post-merge``?)
─────────────────────────────────────────────────────────────────
1. Discoverability — the hook lives next to the rest of the
   Python codebase, gets reviewed in PRs, and tested by BP.J.3.
2. Friendly UX — :mod:`backend.self_healing_docs` exits with codes
   (0/1/2) tuned for CI. A developer sitting at a terminal after a
   ``git pull`` wants prose, colour, and concrete next-step
   instructions when the strict-mode refusal triggers. This shim
   translates the machine codes into operator language.
3. Fast-skip — most merges do not touch the FastAPI surface (e.g.
   ``docs/`` only, ``frontend/`` only, ``Justfile``). We peek at
   ``ORIG_HEAD..HEAD`` before importing the FastAPI app (which is
   slow — pulls in ~140 routers) so >90 % of merges return in a
   few hundred ms.

Module-global state audit (SOP 2026-04-21 rule)
───────────────────────────────────────────────
Zero mutable module-level state. Every helper takes its inputs
explicitly. Multi-worker safety is irrelevant — this module only
runs from a developer's git client, never from a uvicorn worker.
The actual write-side of the operation (``os.replace``-based atomic
file swap inside :mod:`backend.self_healing_docs`) is already
audited there.

Read-after-write timing
───────────────────────
The only read-after-write surface is the underlying
``self_healing_docs.run`` re-reading the new ``openapi.json``. This
hook just calls ``run()`` once; no concurrent reader on the same
working tree. ``git`` already serialises hook execution per repo
via ``.git/index.lock``.

Compat-fingerprint grep
───────────────────────
Pre-commit fingerprint grep (``_conn() / await conn.commit() /
datetime('now') / VALUES (?, ?``) — none can match this module; it
touches no DB and uses no compat shim.

Installation
────────────
The hook is **not** auto-installed (we don't shipping ``.git/hooks``
trojans). To opt in, a developer runs::

    python -m backend.hooks.post_merge_docs --install

…which writes a 3-line shim to ``.git/hooks/post-merge`` (the only
file under ``.git/`` we ever touch), backing up any existing hook
to ``.git/hooks/post-merge.bak.<timestamp>`` first. Uninstall is the
mirror image (``--uninstall``) and restores the most recent backup
when present.

CLI usage
─────────
::

    python -m backend.hooks.post_merge_docs              # run as git would
    python -m backend.hooks.post_merge_docs --force      # ignore fast-skip
    python -m backend.hooks.post_merge_docs --install    # set up .git/hooks/post-merge
    python -m backend.hooks.post_merge_docs --uninstall  # remove the shim
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# ── Paths ─────────────────────────────────────────────────────────────
# Resolved at import time. Same robustness rationale as
# :mod:`backend.self_healing_docs` — caller cwd may be anywhere.
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent
_GIT_DIR: Path = _REPO_ROOT / ".git"
_HOOK_PATH: Path = _GIT_DIR / "hooks" / "post-merge"

# Files that, when changed in a merge, justify re-running the
# self-healing docs check. Anything that contributes to the FastAPI
# OpenAPI surface (routers, schemas, app composition) belongs here.
# Keep the list intentionally permissive — false positive (run for
# nothing) is cheap; false negative (skip a real drift) means stale
# docs for a developer.
_RELEVANT_PATH_PREFIXES: tuple[str, ...] = (
    "backend/main.py",
    "backend/routers/",
    "backend/schemas",          # matches schemas.py and schemas/ dirs
    "backend/models",
    "backend/api/",
    "backend/agents/",          # some agents register routers
    "openapi.json",             # already-out-of-band edits
    "docs/architecture.md",     # ditto
)


# ── Fast-skip helper ─────────────────────────────────────────────────
def _changed_files_in_merge() -> list[str] | None:
    """Return the list of repo-relative paths touched by the just-finished merge.

    Git's ``post-merge`` hook is invoked with ``ORIG_HEAD`` pointing at
    the pre-merge tip and ``HEAD`` at the post-merge tip. We diff the
    two to skip cheaply when the merge didn't touch the FastAPI
    surface.

    Returns ``None`` if we cannot determine the change set — caller
    should treat that as "be safe, run the check anyway". Concrete
    cases that return ``None``: ``ORIG_HEAD`` missing (first clone,
    rebase mid-conflict), ``git`` not on PATH, repo not a git repo,
    permission errors. We never raise out of this helper because the
    hook must not crash the developer's terminal.
    """
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "ORIG_HEAD", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("post_merge_docs: cannot list changed files (%s)", exc)
        return None
    if out.returncode != 0:
        logger.debug(
            "post_merge_docs: git diff returned %d: %s",
            out.returncode,
            out.stderr.strip(),
        )
        return None
    return [line for line in out.stdout.splitlines() if line]


def _merge_touches_api(changed: Iterable[str]) -> bool:
    """True if any path in ``changed`` matches ``_RELEVANT_PATH_PREFIXES``."""
    for path in changed:
        for prefix in _RELEVANT_PATH_PREFIXES:
            if path.startswith(prefix):
                return True
    return False


# ── Pretty output helpers ────────────────────────────────────────────
# We avoid ANSI colour by default — many devs pipe git output through
# tools that mangle escape codes. Operators who want colour can flip
# the env knob; we honour it so CI logs stay grep-friendly.
_USE_COLOUR: bool = (
    sys.stderr.isatty()
    and os.environ.get("OMNISIGHT_HOOK_COLOUR", "auto") != "off"
)


def _c(code: str, text: str) -> str:
    if not _USE_COLOUR:
        return text
    return f"\033[{code}m{text}\033[0m"


def _emit(msg: str, *, prefix: str = "self-healing-docs") -> None:
    """Write a single line to stderr, prefixed for grep-ability.

    Stderr (not stdout) so the hook's chatter doesn't pollute any
    pipeline that captures git's stdout (rare but possible — e.g.
    ``git pull | tee``). Git itself already prints to stderr, so this
    matches the surrounding noise.
    """
    sys.stderr.write(f"[{prefix}] {msg}\n")
    sys.stderr.flush()


# ── Main entry ───────────────────────────────────────────────────────
def main(argv: Iterable[str] | None = None) -> int:
    """Run the post-merge check. Always returns 0 to git.

    Why always 0: the post-merge hook's exit code is **ignored** by
    git (the merge already succeeded by the time we run). But returning
    0 keeps the contract explicit for any wrapper that does check, and
    avoids mis-categorising a strict-mode refusal as a developer-fault
    failure. The strict-refusal signal is communicated **in prose** to
    the developer's terminal; the wrapper that calls this hook never
    needs to act on the exit code.
    """
    ap = argparse.ArgumentParser(
        prog="python -m backend.hooks.post_merge_docs",
        description="git post-merge hook that calls "
        "backend.self_healing_docs in strict mode.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Skip the fast path that bails out when the merge "
        "didn't touch any backend route / schema file.",
    )
    ap.add_argument(
        "--install",
        action="store_true",
        help="Write a shim to .git/hooks/post-merge and exit. "
        "Existing hook (if any) is backed up first.",
    )
    ap.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the shim from .git/hooks/post-merge and restore "
        "the most recent backup if present.",
    )
    args = ap.parse_args(list(argv) if argv is not None else None)

    # Hook installer / uninstaller bypass the actual run.
    if args.install:
        return _install_hook()
    if args.uninstall:
        return _uninstall_hook()

    return _run_hook(force=args.force)


def _run_hook(*, force: bool) -> int:
    """The actual post-merge body. Split out for unit-testability."""
    # ── Fast-skip ───────────────────────────────────────────────────
    if not force:
        changed = _changed_files_in_merge()
        if changed is None:
            _emit(
                "could not determine merge diff; running full check "
                "(use --force to silence)"
            )
        elif not _merge_touches_api(changed):
            # Silent — most merges hit this branch and developers
            # don't want a chirp on every pull.
            logger.debug(
                "post_merge_docs: merge of %d files does not touch API surface; skip",
                len(changed),
            )
            return 0

    # ── Real check ─────────────────────────────────────────────────
    # Lazy import — backend.main pulls in ~140 routers, ~2 s on a
    # warm box, ~6 s cold. We pay that cost only when the merge
    # actually touched the API surface, which is the whole point of
    # the fast-skip above.
    try:
        from backend.self_healing_docs import run as run_self_healing
    except ImportError as exc:
        # Backend env isn't set up (e.g. fresh clone, no `pip install`).
        # Don't crash the developer's terminal — just nudge them.
        _emit(
            f"backend not importable ({exc}); skipping self-healing docs check. "
            "Set up the backend venv and re-run "
            "`python -m backend.hooks.post_merge_docs --force` if you want."
        )
        return 0

    started = time.monotonic()
    try:
        result = run_self_healing(strict=True)
    except Exception as exc:  # pragma: no cover — defensive
        # Never let a crash of the watchdog block the developer.
        # Log loud, exit clean.
        _emit(
            _c("31", f"self-healing-docs crashed: {exc}. ")
            + "Your merge succeeded; please run "
            "`python -m backend.self_healing_docs --report-only` "
            "to investigate."
        )
        logger.exception("post_merge_docs: self_healing_docs.run() crashed")
        return 0
    elapsed = time.monotonic() - started

    report = result.report

    # ── Communicate outcome ────────────────────────────────────────
    if report.is_empty:
        # Silent on the no-drift path — just like ``git pull`` is
        # silent when there's nothing to do.
        logger.debug("post_merge_docs: no drift (%.2fs)", elapsed)
        return 0

    # Build a one-line summary first; the strict-refusal branch will
    # extend it with operator instructions.
    summary_parts: list[str] = []
    if report.added_routes:
        summary_parts.append(f"+{len(report.added_routes)} routes")
    if report.removed_routes:
        summary_parts.append(f"-{len(report.removed_routes)} routes")
    if report.modified_routes:
        summary_parts.append(f"~{len(report.modified_routes)} routes")
    if report.added_schemas:
        summary_parts.append(f"+{len(report.added_schemas)} schemas")
    if report.removed_schemas:
        summary_parts.append(f"-{len(report.removed_schemas)} schemas")
    summary = ", ".join(summary_parts)

    if result.refused_breaking:
        # Strict mode + breaking diff — DO NOT auto-write. Tell the
        # developer loudly, with the exact next-step command.
        _emit(_c("33;1", f"⚠ breaking API change in this merge: {summary}"))
        _emit(
            "  refused to auto-update openapi.json / docs/architecture.md "
            "because routes were REMOVED."
        )
        if report.removed_routes:
            shown = report.removed_routes[:5]
            for path, method in shown:
                _emit(f"    removed: {method} {path}")
            if len(report.removed_routes) > 5:
                _emit(f"    … and {len(report.removed_routes) - 5} more")
        if report.removed_schemas:
            _emit(
                "    removed schemas: "
                + ", ".join(report.removed_schemas[:5])
                + (" …" if len(report.removed_schemas) > 5 else "")
            )
        _emit(
            "  if this was intentional, ack with: "
            + _c("36", "python -m backend.self_healing_docs")
        )
        _emit(
            "  to dry-run first: "
            + _c("36", "python -m backend.self_healing_docs --report-only")
        )
        return 0

    # Non-breaking drift was auto-applied. Tell the developer so they
    # can include the regenerated artefacts in their next commit.
    _emit(_c("32", f"✓ refreshed docs ({summary}, {elapsed:.1f}s)"))
    written: list[str] = []
    if result.wrote_openapi:
        written.append("openapi.json")
    if result.wrote_architecture:
        written.append("docs/architecture.md")
    if written:
        _emit("  updated: " + ", ".join(written))
        _emit("  please `git add` & commit these alongside your next change.")
    return 0


# ── Hook installer ───────────────────────────────────────────────────
# A 3-line shell shim, intentionally minimal — all the logic lives in
# this Python module. Keeping the shim trivial means a developer
# upgrading the hook only needs to ``git pull``; no re-install needed
# unless the shim itself changes (which should ~never happen).
_HOOK_SHIM = """\
#!/usr/bin/env sh
# Auto-generated by `python -m backend.hooks.post_merge_docs --install`.
# Source of truth: backend/hooks/post_merge_docs.py
exec python3 -m backend.hooks.post_merge_docs "$@"
"""


def _install_hook() -> int:
    """Write the shim to ``.git/hooks/post-merge``. Backs up existing.

    Returns 0 on success, 1 on failure (printed to stderr).
    """
    if not _GIT_DIR.is_dir():
        _emit(
            _c("31", f"not a git repo (no {_GIT_DIR}); cannot install hook"),
            prefix="install",
        )
        return 1
    hooks_dir = _HOOK_PATH.parent
    hooks_dir.mkdir(parents=True, exist_ok=True)

    if _HOOK_PATH.exists() or _HOOK_PATH.is_symlink():
        existing = _HOOK_PATH.read_text(encoding="utf-8", errors="replace")
        if existing == _HOOK_SHIM:
            _emit("hook already installed; nothing to do.", prefix="install")
            return 0
        backup = _HOOK_PATH.with_name(
            f"post-merge.bak.{int(time.time())}"
        )
        _HOOK_PATH.replace(backup)
        _emit(f"backed up existing hook to {backup.name}", prefix="install")

    _HOOK_PATH.write_text(_HOOK_SHIM, encoding="utf-8")
    # rwxr-xr-x — git requires the hook to be executable.
    _HOOK_PATH.chmod(0o755)
    _emit(
        _c("32", f"installed hook to {_HOOK_PATH.relative_to(_REPO_ROOT)}"),
        prefix="install",
    )
    _emit(
        "to opt out later: python -m backend.hooks.post_merge_docs --uninstall",
        prefix="install",
    )
    return 0


def _uninstall_hook() -> int:
    """Remove the shim. Restore the most recent backup if found."""
    if not _HOOK_PATH.exists():
        _emit("no hook to uninstall.", prefix="uninstall")
        return 0
    current = _HOOK_PATH.read_text(encoding="utf-8", errors="replace")
    if current != _HOOK_SHIM:
        _emit(
            _c(
                "33",
                f"{_HOOK_PATH.relative_to(_REPO_ROOT)} is not our shim; "
                "leaving it alone.",
            ),
            prefix="uninstall",
        )
        return 1
    _HOOK_PATH.unlink()
    _emit("removed our shim.", prefix="uninstall")

    # Restore the most recent backup, if any.
    backups = sorted(
        _HOOK_PATH.parent.glob("post-merge.bak.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if backups:
        most_recent = backups[0]
        most_recent.replace(_HOOK_PATH)
        _emit(
            f"restored previous hook from {most_recent.name}",
            prefix="uninstall",
        )
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("OMNISIGHT_HOOK_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(main())
