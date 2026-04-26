#!/usr/bin/env python3
"""Y6 #282 row 4 — legacy flat workspace → 5-layer hierarchy migrator.

Pre-Y6 every agent's workspace lived at a flat ``.agent_workspaces/
{agent_id}/`` path, regardless of tenant / product_line / project_id /
remote_url. Y6 row 1 introduced the canonical 5-layer layout
``{workspace_root}/{tenant_id}/{product_line}/{project_id}/{agent_id}/
{repo_url_hash}/`` (see ``backend/workspace.py``). This script is the
**one-shot migrator** that moves the legacy flat dirs to the new
location and leaves a backward-compat symlink at the old path so any
in-flight reference still resolves for the duration of one release.

Source / target defaults
------------------------

* **Source root** — ``<repo>/.agent_workspaces/`` (the value of
  ``backend.workspace._WORKSPACES_ROOT``). Override with
  ``--source``.
* **Target root** — ``<repo>/data/workspaces/`` (the value of
  ``backend.config.settings.workspace_root``, resolved relative to
  the project root if not absolute). Override with ``--target``.

Each legacy entry ``{src}/{agent_id}/`` becomes::

    {dst}/t-default/default/default/{safe(agent_id)}/legacy-hash/

The ``legacy-hash`` leaf is a literal sentinel (NOT
``sha256(remote_url)[:16]``): pre-Y6 we don't know what remote URL the
clone came from, so the standard hash leaf is unreachable. The
sentinel makes pre-Y6 workspaces grep-able by the GC reaper / quota
counter (rows 5–6) if they ever need to special-case them.

Tenant / product_line / project_id all collapse to the ``_DEFAULT_*``
constants because the legacy layout had no tenant context — every
pre-Y6 workspace was effectively in the default tenant's default
project.

Lifecycle (per the TODO row)
----------------------------

1. **This release** — operator runs the migrator. Each legacy
   workspace is moved + a symlink is left at the old path so
   anything still hardcoding ``.agent_workspaces/{agent_id}/`` keeps
   resolving for one full release window.
2. **Next release** — operator runs ``--remove-symlinks`` to delete
   the compatibility symlinks. After this, the old path is gone and
   only the new layout exists.

Operations
----------

* ``--dry-run`` — print the migration plan without touching disk.
* ``--remove-symlinks`` — second-stage cleanup: walk the source root
  and delete any symlinks created by the first stage. Does **not**
  touch real workspace dirs.
* ``--json`` — machine-readable summary (one record per workspace).
* ``--source <path>`` — override source root (default
  ``backend.workspace._WORKSPACES_ROOT``).
* ``--target <path>`` — override target root (default
  ``backend.config.settings.workspace_root`` resolved against the
  project root).

Safety
------

* **Idempotent**: re-running on an already-migrated source is a
  no-op. Existing symlinks are skipped, target collisions are
  reported and skipped (operator must intervene), and
  ``--remove-symlinks`` only deletes paths that actually point at
  the new location.
* **Worktree admin-block aware**: when a legacy workspace is a git
  worktree (``.git`` is a *file* containing
  ``gitdir: <repo>/.git/worktrees/<name>``), the matching admin
  block's ``gitdir`` file is rewritten to point at the new
  workspace's ``.git`` file. Without this fix the next ``git
  worktree prune`` would silently drop the admin block.
* **Operational sidecars are skipped**: any top-level entry whose
  name starts with ``_`` (``_prewarm``, ``_trash``, …) is left
  alone — these are not agent workspaces, they belong to other
  modules.
* **Dirs without a ``.git`` are skipped**: the script only moves
  things that look like real workspaces.

Module-global state audit (per
``docs/sop/implement_phase_step.md`` Step 1, type-1 answer): this
script reads ``backend.workspace._WORKSPACES_ROOT`` /
``backend.config.settings.workspace_root`` — both module-level
constants derived once at process boot from the same source, so
multi-worker concerns don't apply (the script runs offline as a
maintenance op anyway). It does NOT touch the in-process
``_workspaces`` registry — that registry is per-worker and would
only be populated if the backend were running, which the operator
has been instructed not to do during the migration.

Exit codes
----------

* ``0`` — migration completed (whether or not any rows moved).
* ``2`` — argparse / IO error.
* ``3`` — at least one workspace failed to move (target collision,
  permission denied, …); operator must intervene before re-running.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Make ``backend`` importable when invoked as ``./scripts/migrate_...``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger("migrate_workspace_hierarchy")


# ── Defaults that match the runtime ─────────────────────────────────
# Resolved lazily inside ``main()`` so importing this module never
# pulls in the full FastAPI app / settings (keeps the script lean and
# importable from tests).

DEFAULT_TENANT_ID = "t-default"
DEFAULT_PRODUCT_LINE = "default"
DEFAULT_PROJECT_ID = "default"
LEGACY_HASH_SENTINEL = "legacy-hash"


# ── Public dataclass surfaced to tests + JSON output ────────────────


@dataclass
class MigrationRecord:
    """One row per legacy workspace inspected.

    ``status`` values:
    * ``moved`` — dir successfully moved + symlink left behind.
    * ``moved_no_symlink`` — moved but symlink was suppressed
      (``--no-symlink`` or symlink creation failed).
    * ``skipped_already_symlink`` — source is already a compat
      symlink from a prior run; nothing to do.
    * ``skipped_target_exists`` — target path already populated;
      operator must resolve manually.
    * ``skipped_no_git`` — source dir has no ``.git`` entry; not a
      real workspace, not migrated.
    * ``skipped_sidecar`` — name starts with ``_``; reserved
      operational dir.
    * ``failed`` — IO error while moving; ``error`` carries the
      message.
    * ``symlink_removed`` — ``--remove-symlinks`` mode: compat
      symlink deleted.
    * ``symlink_kept`` — ``--remove-symlinks`` mode: path is not a
      symlink to the new location, left alone.
    """
    agent_id: str
    src: str
    dst: str
    status: str
    error: str = ""
    worktree_admin_updated: bool = False


@dataclass
class MigrationSummary:
    records: list[MigrationRecord] = field(default_factory=list)
    source_root: str = ""
    target_root: str = ""
    dry_run: bool = False
    mode: str = "migrate"

    @property
    def moved_count(self) -> int:
        return sum(
            1 for r in self.records
            if r.status in ("moved", "moved_no_symlink")
        )

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.records if r.status == "failed")

    def to_json(self) -> str:
        return json.dumps(
            {
                "source_root": self.source_root,
                "target_root": self.target_root,
                "dry_run": self.dry_run,
                "mode": self.mode,
                "moved": self.moved_count,
                "failed": self.failed_count,
                "records": [asdict(r) for r in self.records],
            },
            indent=2,
        )


# ── Helpers ─────────────────────────────────────────────────────────


_SAFE_COMPONENT = re.compile(r"[^a-zA-Z0-9_-]")


def _safe_path_component(value: str, *, fallback: str = "agent") -> str:
    """Mirror of ``backend.workspace._safe_path_component``.

    Kept as a local copy so the script does not have to import
    ``backend.workspace`` (which pulls a heavy dependency graph
    including events / audit). Behaviour is identical so the legacy
    agent_id maps to exactly the same target dir name as the runtime
    would compute.
    """
    if not value:
        return fallback
    cleaned = _SAFE_COMPONENT.sub("_", value)
    return cleaned or fallback


def _looks_like_workspace(path: Path) -> bool:
    """A real workspace has a ``.git`` entry (file for worktree, dir
    for plain clone). Anything else is a stray dir we leave alone."""
    return (path / ".git").exists()


def _resolve_target(target_root: Path, agent_id: str) -> Path:
    return (
        target_root
        / DEFAULT_TENANT_ID
        / DEFAULT_PRODUCT_LINE
        / DEFAULT_PROJECT_ID
        / _safe_path_component(agent_id)
        / LEGACY_HASH_SENTINEL
    )


def _read_worktree_admin_path(workspace_dot_git: Path) -> Path | None:
    """If ``<ws>/.git`` is a worktree pointer file, return the admin
    block dir (``<repo>/.git/worktrees/<name>/``). Returns ``None`` for
    plain clones (where ``.git`` is a directory) or for malformed
    pointer files.
    """
    if not workspace_dot_git.is_file():
        return None
    try:
        content = workspace_dot_git.read_text(errors="replace").strip()
    except OSError:
        return None
    # Format: ``gitdir: /abs/path/to/.git/worktrees/<name>``
    prefix = "gitdir:"
    if not content.startswith(prefix):
        return None
    admin_path = Path(content[len(prefix):].strip())
    return admin_path if admin_path.is_dir() else None


def _update_worktree_admin_block(
    admin_dir: Path, new_workspace_dir: Path,
) -> bool:
    """Rewrite the admin block's ``gitdir`` file to point at the new
    workspace's ``.git`` file. Returns True on success.
    """
    gitdir_file = admin_dir / "gitdir"
    if not gitdir_file.is_file():
        return False
    new_pointer = (new_workspace_dir / ".git").resolve()
    try:
        gitdir_file.write_text(f"{new_pointer}\n")
        return True
    except OSError as exc:
        logger.warning(
            "Failed to rewrite admin block gitdir %s: %s",
            gitdir_file, exc,
        )
        return False


# ── Core migration ──────────────────────────────────────────────────


def plan_migration(
    source_root: Path,
    target_root: Path,
) -> list[tuple[str, Path, Path]]:
    """Return ``(agent_id, src_dir, dst_dir)`` triples for every
    legacy flat-layout entry under ``source_root``. Operational
    sidecars and non-workspace strays are filtered out — the caller's
    migration loop only sees real workspaces.

    The ``agent_id`` returned is the **raw on-disk dir name**; the
    ``dst_dir`` already has the sanitisation applied (so a pathological
    legacy name like ``agent..foo`` maps to ``agent__foo`` under the
    new tree, matching what ``provision()`` would do).
    """
    if not source_root.exists():
        return []
    plan: list[tuple[str, Path, Path]] = []
    for entry in sorted(source_root.iterdir()):
        if not entry.is_dir() and not entry.is_symlink():
            continue
        if entry.name.startswith("_"):
            continue
        plan.append((entry.name, entry, _resolve_target(target_root, entry.name)))
    return plan


def migrate(
    source_root: Path,
    target_root: Path,
    *,
    dry_run: bool = False,
    create_symlink: bool = True,
) -> MigrationSummary:
    """Move every legacy flat workspace under ``source_root`` into the
    5-layer hierarchy under ``target_root``. See module docstring.
    """
    summary = MigrationSummary(
        source_root=str(source_root),
        target_root=str(target_root),
        dry_run=dry_run,
        mode="migrate",
    )

    if not source_root.exists():
        logger.info("Source root %s does not exist — nothing to migrate.", source_root)
        return summary

    for agent_id, src, dst in plan_migration(source_root, target_root):
        rec = MigrationRecord(
            agent_id=agent_id, src=str(src), dst=str(dst), status="",
        )

        # 1) Source is already a compat symlink — already migrated.
        if src.is_symlink():
            rec.status = "skipped_already_symlink"
            summary.records.append(rec)
            logger.info("[skip] %s already a symlink → %s", src, src.resolve())
            continue

        # 2) Not a real workspace.
        if not _looks_like_workspace(src):
            rec.status = "skipped_no_git"
            summary.records.append(rec)
            logger.info("[skip] %s has no .git — not a workspace", src)
            continue

        # 3) Target collision — operator must resolve.
        if dst.exists() or dst.is_symlink():
            rec.status = "skipped_target_exists"
            rec.error = f"target {dst} already exists"
            summary.records.append(rec)
            logger.warning(
                "[skip] target already exists for %s: %s — manual cleanup required",
                agent_id, dst,
            )
            continue

        if dry_run:
            rec.status = "moved" if create_symlink else "moved_no_symlink"
            summary.records.append(rec)
            logger.info("[dry-run] %s → %s", src, dst)
            continue

        # 4) Resolve worktree admin block BEFORE the move so we still
        #    have the legacy ``.git`` pointer to read.
        admin_dir = _read_worktree_admin_path(src / ".git")

        # 5) Ensure parent of target exists, then move.
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except (OSError, shutil.Error) as exc:
            rec.status = "failed"
            rec.error = f"move failed: {exc}"
            summary.records.append(rec)
            logger.error("[fail] move %s → %s: %s", src, dst, exc)
            continue

        # 6) Patch worktree admin block to point at the new workspace.
        if admin_dir is not None:
            rec.worktree_admin_updated = _update_worktree_admin_block(
                admin_dir, dst,
            )
            if rec.worktree_admin_updated:
                logger.info(
                    "  patched worktree admin block %s/gitdir → %s/.git",
                    admin_dir, dst,
                )
            else:
                logger.warning(
                    "  could not patch admin block %s — next `git worktree "
                    "prune` may drop it",
                    admin_dir,
                )

        # 7) Drop a backward-compat symlink at the old path.
        if create_symlink:
            try:
                os.symlink(dst.resolve(), src)
                rec.status = "moved"
                logger.info("[ok] %s → %s (symlink kept)", src, dst)
            except OSError as exc:
                rec.status = "moved_no_symlink"
                rec.error = f"symlink creation failed: {exc}"
                logger.warning(
                    "  symlink %s → %s failed: %s — old callers will break",
                    src, dst, exc,
                )
        else:
            rec.status = "moved_no_symlink"
            logger.info("[ok] %s → %s (no symlink)", src, dst)

        summary.records.append(rec)

    return summary


def remove_symlinks(
    source_root: Path,
    target_root: Path,
    *,
    dry_run: bool = False,
) -> MigrationSummary:
    """Second-stage cleanup: walk ``source_root`` and delete any
    symlink whose target sits under ``target_root``.

    A symlink that points elsewhere (operator-created, foreign) is
    left alone — we only delete what *we* would have created.
    """
    summary = MigrationSummary(
        source_root=str(source_root),
        target_root=str(target_root),
        dry_run=dry_run,
        mode="remove-symlinks",
    )

    if not source_root.exists():
        logger.info("Source root %s does not exist — nothing to remove.", source_root)
        return summary

    target_resolved = target_root.resolve() if target_root.exists() else target_root

    for entry in sorted(source_root.iterdir()):
        if entry.name.startswith("_"):
            continue
        if not entry.is_symlink():
            continue

        rec = MigrationRecord(
            agent_id=entry.name,
            src=str(entry),
            dst="",
            status="",
        )
        try:
            link_target = Path(os.readlink(entry))
        except OSError as exc:
            rec.status = "failed"
            rec.error = f"readlink failed: {exc}"
            summary.records.append(rec)
            continue
        # Resolve relative symlinks against the parent.
        if not link_target.is_absolute():
            link_target = (entry.parent / link_target).resolve()
        rec.dst = str(link_target)

        # Only delete symlinks that point INTO our target tree —
        # never touch operator-created symlinks aimed elsewhere.
        try:
            link_target.relative_to(target_resolved)
        except ValueError:
            rec.status = "symlink_kept"
            summary.records.append(rec)
            logger.info(
                "[skip] %s points outside target tree (%s) — leaving alone",
                entry, link_target,
            )
            continue

        if dry_run:
            rec.status = "symlink_removed"
            summary.records.append(rec)
            logger.info("[dry-run] would unlink %s → %s", entry, link_target)
            continue

        try:
            entry.unlink()
            rec.status = "symlink_removed"
            logger.info("[ok] removed compat symlink %s", entry)
        except OSError as exc:
            rec.status = "failed"
            rec.error = f"unlink failed: {exc}"
            logger.error("[fail] unlink %s: %s", entry, exc)
        summary.records.append(rec)

    return summary


# ── CLI ─────────────────────────────────────────────────────────────


def _default_source_root() -> Path:
    """Read ``backend.workspace._WORKSPACES_ROOT`` if available, else
    fall back to ``<project_root>/.agent_workspaces``. Lazy import so
    the script stays usable on a host where the full backend env is
    not installed (operators may run it from a tarball)."""
    try:
        from backend import workspace as _ws
        return Path(_ws._WORKSPACES_ROOT)
    except Exception:  # pragma: no cover — fallback for partial env
        project_root = Path(__file__).resolve().parents[1]
        return project_root / ".agent_workspaces"


def _default_target_root() -> Path:
    """Read ``backend.config.settings.workspace_root``, resolved
    against the project root if it is a relative path. Falls back to
    ``<project_root>/data/workspaces`` if the import fails."""
    project_root = Path(__file__).resolve().parents[1]
    try:
        from backend.config import settings
        candidate = Path(settings.workspace_root)
    except Exception:  # pragma: no cover
        candidate = Path("./data/workspaces")
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="migrate_workspace_hierarchy",
        description=(
            "Y6 #282 row 4 — move legacy flat-layout workspaces into "
            "the 5-layer hierarchy and leave a compat symlink behind."
        ),
    )
    p.add_argument(
        "--source", type=Path, default=None,
        help="Legacy flat workspaces root "
        "(default: backend.workspace._WORKSPACES_ROOT)",
    )
    p.add_argument(
        "--target", type=Path, default=None,
        help="New 5-layer workspaces root "
        "(default: backend.config.settings.workspace_root)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print plan without modifying disk.",
    )
    p.add_argument(
        "--no-symlink", action="store_true",
        help="Do NOT leave a backward-compat symlink at the old path.",
    )
    p.add_argument(
        "--remove-symlinks", action="store_true",
        help="Second-stage cleanup: delete the compat symlinks left "
        "behind by a prior migration run.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON summary on stdout.",
    )
    p.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-record log output (summary still prints).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
    )

    source = args.source or _default_source_root()
    target = args.target or _default_target_root()

    if args.remove_symlinks:
        summary = remove_symlinks(source, target, dry_run=args.dry_run)
    else:
        summary = migrate(
            source, target,
            dry_run=args.dry_run,
            create_symlink=not args.no_symlink,
        )

    if args.json:
        print(summary.to_json())
    else:
        print(
            f"\n[{summary.mode}] source={summary.source_root} "
            f"target={summary.target_root} dry_run={summary.dry_run}"
        )
        for rec in summary.records:
            extra = f" — {rec.error}" if rec.error else ""
            print(f"  {rec.status:30s} {rec.agent_id}{extra}")
        if summary.mode == "migrate":
            print(
                f"\nMoved: {summary.moved_count}, Failed: {summary.failed_count}, "
                f"Total inspected: {len(summary.records)}"
            )

    return 3 if summary.failed_count else 0


if __name__ == "__main__":
    sys.exit(main())
