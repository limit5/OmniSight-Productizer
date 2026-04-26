"""Y6 #282 row 4 — legacy flat workspace → 5-layer hierarchy migrator.

Locks the contract for ``scripts/migrate_workspace_hierarchy.py``:

* legacy ``{src}/{agent_id}/`` flat dirs move to
  ``{dst}/t-default/default/default/{safe(agent_id)}/legacy-hash/``
* a backward-compat symlink is left at the old path
* worktree admin block ``gitdir`` files are rewritten to point at the
  new workspace ``.git``
* operational sidecars (``_prewarm`` / ``_trash`` / any ``_*``) are
  skipped
* dirs without a ``.git`` are skipped (not real workspaces)
* the migration is idempotent — re-running on an already-migrated
  source is a no-op
* ``--remove-symlinks`` deletes only the compat symlinks pointing
  into the new tree, never operator-created symlinks aimed elsewhere

These are pure-Python tests against the module surface — no FastAPI /
DB / network. They run fast and cover every documented branch of the
script's state machine.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


# ── Load the script as an importable module ────────────────────────


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "migrate_workspace_hierarchy.py"


@pytest.fixture(scope="module")
def migrator():
    """Load the migrator script as a module so tests can call its
    functions directly. Loading via ``importlib.util.spec_from_file_location``
    keeps the script self-contained — ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location(
        "migrate_workspace_hierarchy_module", _SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── Fixture: fake legacy flat-layout workspace tree ────────────────


def _make_fake_flat_workspace(
    root: Path, agent_id: str, *, with_worktree_admin: bool = False,
    repo_root: Path | None = None,
) -> Path:
    """Build a minimal directory that looks like a legacy
    ``.agent_workspaces/{agent_id}/`` workspace.

    When ``with_worktree_admin`` is True, also fabricate a
    ``<repo_root>/.git/worktrees/<agent_id>/gitdir`` admin block and
    write the workspace's ``.git`` as a worktree pointer file —
    mirroring what ``git worktree add`` produces.
    """
    ws = root / agent_id
    ws.mkdir(parents=True)
    (ws / "README.md").write_text(f"hello from {agent_id}\n")

    if with_worktree_admin:
        assert repo_root is not None, "worktree admin needs a repo_root"
        admin = repo_root / ".git" / "worktrees" / agent_id
        admin.mkdir(parents=True)
        # The pointer file pre-migration points at the old workspace.
        (admin / "gitdir").write_text(str((ws / ".git").resolve()) + "\n")
        (ws / ".git").write_text(f"gitdir: {admin.resolve()}\n")
    else:
        # Plain clone: ``.git`` is a directory.
        (ws / ".git").mkdir()
        (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    return ws


# ── 1) Pure helpers ────────────────────────────────────────────────


def test_safe_path_component_keeps_safe_chars(migrator):
    assert migrator._safe_path_component("software-beta") == "software-beta"
    assert migrator._safe_path_component("agent_42") == "agent_42"


def test_safe_path_component_sanitises_unsafe(migrator):
    assert migrator._safe_path_component("../../escape") == "______escape"
    assert migrator._safe_path_component("agent;rm -rf /") == "agent_rm_-rf__"
    assert migrator._safe_path_component("") == "agent"


def test_resolve_target_uses_default_namespace(migrator, tmp_path):
    target = migrator._resolve_target(tmp_path, "alpha")
    expected = (
        tmp_path
        / migrator.DEFAULT_TENANT_ID
        / migrator.DEFAULT_PRODUCT_LINE
        / migrator.DEFAULT_PROJECT_ID
        / "alpha"
        / migrator.LEGACY_HASH_SENTINEL
    )
    assert target == expected


# ── 2) plan_migration filters operational sidecars + non-workspaces ─


def test_plan_skips_sidecar_dirs(migrator, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "_prewarm").mkdir()
    (src / "_trash").mkdir()
    _make_fake_flat_workspace(src, "real-agent")

    plan = migrator.plan_migration(src, tmp_path / "dst")
    agent_ids = [agent for agent, _, _ in plan]
    # _prewarm / _trash filtered out at plan time.
    assert "_prewarm" not in agent_ids
    assert "_trash" not in agent_ids
    assert "real-agent" in agent_ids


def test_plan_returns_empty_when_source_missing(migrator, tmp_path):
    """Missing source root is a no-op, not an error — operator may
    have already moved everything by hand."""
    assert migrator.plan_migration(tmp_path / "missing", tmp_path / "dst") == []


# ── 3) migrate() — happy path with worktree admin block ─────────────


def test_migrate_moves_workspace_and_updates_admin_block(migrator, tmp_path):
    repo = tmp_path / "fake_repo"
    (repo / ".git").mkdir(parents=True)
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"

    ws = _make_fake_flat_workspace(
        src, "alpha", with_worktree_admin=True, repo_root=repo,
    )
    admin = repo / ".git" / "worktrees" / "alpha"
    pre_pointer = (admin / "gitdir").read_text().strip()
    assert pre_pointer == str((ws / ".git").resolve())

    summary = migrator.migrate(src, dst)
    assert summary.moved_count == 1
    assert summary.failed_count == 0
    rec = summary.records[0]
    assert rec.status == "moved"
    assert rec.worktree_admin_updated is True

    # New workspace exists under the 5-layer path.
    new_ws = dst / "t-default" / "default" / "default" / "alpha" / "legacy-hash"
    assert new_ws.is_dir()
    assert (new_ws / "README.md").read_text() == "hello from alpha\n"
    # The .git pointer file was carried over by shutil.move.
    assert (new_ws / ".git").is_file()

    # Admin block now points at the new .git file.
    new_pointer = (admin / "gitdir").read_text().strip()
    assert new_pointer == str((new_ws / ".git").resolve())

    # Backward-compat symlink left at old path.
    old = src / "alpha"
    assert old.is_symlink()
    assert Path(os.readlink(old)).resolve() == new_ws.resolve()


def test_migrate_handles_plain_clone_without_admin_block(migrator, tmp_path):
    """For plain clones (``.git`` is a directory), no admin-block
    rewrite is needed — the migration must still succeed and leave a
    symlink behind."""
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"

    _make_fake_flat_workspace(src, "plain-clone", with_worktree_admin=False)
    summary = migrator.migrate(src, dst)

    assert summary.moved_count == 1
    rec = summary.records[0]
    assert rec.status == "moved"
    assert rec.worktree_admin_updated is False  # no admin block to update

    new_ws = dst / "t-default" / "default" / "default" / "plain-clone" / "legacy-hash"
    assert new_ws.is_dir()
    assert (new_ws / ".git").is_dir()
    assert (src / "plain-clone").is_symlink()


# ── 4) migrate() — skip branches ────────────────────────────────────


def test_migrate_skips_already_symlinked_source(migrator, tmp_path):
    """Re-running after a successful migration should be a no-op —
    the symlink at the old path is recognized as 'already moved'."""
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"
    target_dir = (
        dst / "t-default" / "default" / "default" / "alpha" / "legacy-hash"
    )
    target_dir.mkdir(parents=True)
    (target_dir / "README.md").write_text("already migrated\n")

    # Simulate a prior run that left a compat symlink behind.
    os.symlink(target_dir.resolve(), src / "alpha")

    summary = migrator.migrate(src, dst)
    assert len(summary.records) == 1
    assert summary.records[0].status == "skipped_already_symlink"
    # Symlink still exists, target untouched.
    assert (src / "alpha").is_symlink()
    assert (target_dir / "README.md").read_text() == "already migrated\n"


def test_migrate_skips_dir_without_dot_git(migrator, tmp_path):
    src = tmp_path / "legacy"
    src.mkdir()
    stray = src / "not-a-workspace"
    stray.mkdir()
    (stray / "random.txt").write_text("hello\n")

    summary = migrator.migrate(src, tmp_path / "dst")
    assert len(summary.records) == 1
    assert summary.records[0].status == "skipped_no_git"
    # Stray dir untouched.
    assert (stray / "random.txt").read_text() == "hello\n"


def test_migrate_skips_when_target_already_exists(migrator, tmp_path):
    """If the target path is already populated (operator-created or
    leftover from a partial prior run), the script must NOT overwrite
    — it logs the collision and skips."""
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"

    _make_fake_flat_workspace(src, "alpha", with_worktree_admin=False)
    # Pre-create a target collision.
    target_dir = (
        dst / "t-default" / "default" / "default" / "alpha" / "legacy-hash"
    )
    target_dir.mkdir(parents=True)
    (target_dir / "marker.txt").write_text("pre-existing\n")

    summary = migrator.migrate(src, dst)
    rec = summary.records[0]
    assert rec.status == "skipped_target_exists"
    assert "already exists" in rec.error
    # Target marker untouched.
    assert (target_dir / "marker.txt").read_text() == "pre-existing\n"
    # Source still in place — operator must intervene.
    assert (src / "alpha" / ".git").exists()


def test_migrate_skips_underscore_prefixed_dirs(migrator, tmp_path):
    """``_prewarm`` / ``_trash`` are NOT migrated — they belong to
    other modules' lifecycles."""
    src = tmp_path / "legacy"
    src.mkdir()
    (src / "_prewarm").mkdir()
    (src / "_prewarm" / "marker.txt").write_text("sandbox\n")

    summary = migrator.migrate(src, tmp_path / "dst")
    # _prewarm filtered out at plan stage — no record produced.
    assert all(r.agent_id != "_prewarm" for r in summary.records)
    # Sidecar dir untouched.
    assert (src / "_prewarm" / "marker.txt").read_text() == "sandbox\n"


# ── 5) Dry-run produces a plan without touching disk ────────────────


def test_migrate_dry_run_does_not_touch_disk(migrator, tmp_path):
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"

    ws = _make_fake_flat_workspace(src, "alpha", with_worktree_admin=False)
    pre_files = sorted(p.name for p in ws.iterdir())

    summary = migrator.migrate(src, dst, dry_run=True)
    assert summary.moved_count == 1
    assert summary.records[0].status == "moved"
    # Source still present, target NOT created, no symlink yet.
    assert ws.is_dir()
    assert not ws.is_symlink()
    assert sorted(p.name for p in ws.iterdir()) == pre_files
    assert not dst.exists()


# ── 6) --no-symlink suppresses the compat shim ──────────────────────


def test_migrate_no_symlink_mode_omits_backward_compat_symlink(migrator, tmp_path):
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"
    _make_fake_flat_workspace(src, "alpha", with_worktree_admin=False)

    summary = migrator.migrate(src, dst, create_symlink=False)
    rec = summary.records[0]
    assert rec.status == "moved_no_symlink"
    assert summary.moved_count == 1
    # Old path is gone, new path exists, no symlink trail.
    assert not (src / "alpha").exists()
    new_ws = dst / "t-default" / "default" / "default" / "alpha" / "legacy-hash"
    assert new_ws.is_dir()


# ── 7) --remove-symlinks deletes only our symlinks ──────────────────


def test_remove_symlinks_deletes_compat_links_into_target(migrator, tmp_path):
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"
    target_dir = (
        dst / "t-default" / "default" / "default" / "alpha" / "legacy-hash"
    )
    target_dir.mkdir(parents=True)
    (target_dir / "README.md").write_text("real workspace\n")

    # Compat symlink we'd have created.
    os.symlink(target_dir.resolve(), src / "alpha")

    summary = migrator.remove_symlinks(src, dst)
    assert len(summary.records) == 1
    rec = summary.records[0]
    assert rec.status == "symlink_removed"
    assert not (src / "alpha").exists()
    # Real workspace at target untouched.
    assert (target_dir / "README.md").read_text() == "real workspace\n"


def test_remove_symlinks_keeps_foreign_symlinks(migrator, tmp_path):
    """A symlink that points OUTSIDE our target tree is not ours and
    must be left alone."""
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"
    dst.mkdir()
    # Foreign symlink to a path outside dst.
    foreign_target = tmp_path / "elsewhere"
    foreign_target.mkdir()
    os.symlink(foreign_target.resolve(), src / "foreign-link")

    summary = migrator.remove_symlinks(src, dst)
    rec = summary.records[0]
    assert rec.status == "symlink_kept"
    assert (src / "foreign-link").is_symlink()
    assert Path(os.readlink(src / "foreign-link")).resolve() == foreign_target.resolve()


def test_remove_symlinks_dry_run_keeps_links(migrator, tmp_path):
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"
    target_dir = (
        dst / "t-default" / "default" / "default" / "alpha" / "legacy-hash"
    )
    target_dir.mkdir(parents=True)
    os.symlink(target_dir.resolve(), src / "alpha")

    summary = migrator.remove_symlinks(src, dst, dry_run=True)
    assert summary.records[0].status == "symlink_removed"
    # Dry-run = link still on disk.
    assert (src / "alpha").is_symlink()


# ── 8) Idempotency — successive runs converge ───────────────────────


def test_migrate_is_idempotent_across_runs(migrator, tmp_path):
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"
    _make_fake_flat_workspace(src, "alpha", with_worktree_admin=False)

    first = migrator.migrate(src, dst)
    assert first.moved_count == 1
    assert first.records[0].status == "moved"

    # Second run sees the symlink and short-circuits.
    second = migrator.migrate(src, dst)
    assert second.moved_count == 0
    assert len(second.records) == 1
    assert second.records[0].status == "skipped_already_symlink"


# ── 9) JSON output round-trips ──────────────────────────────────────


def test_summary_json_serialises(migrator, tmp_path):
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"
    _make_fake_flat_workspace(src, "alpha", with_worktree_admin=False)

    summary = migrator.migrate(src, dst, dry_run=True)
    blob = summary.to_json()
    import json as _json
    decoded = _json.loads(blob)
    assert decoded["mode"] == "migrate"
    assert decoded["dry_run"] is True
    assert decoded["moved"] == 1
    assert decoded["records"][0]["agent_id"] == "alpha"


# ── 10) Pathological agent_id is sanitised at the target side ──────


def test_pathological_agent_id_is_sanitised_in_target(migrator, tmp_path):
    """Legacy agent_id with shell metachars / .. must be regex-cleaned
    before joining into the new path; the source dir name is used
    as-is on the legacy side (it already passed mkdir there)."""
    src = tmp_path / "legacy"
    src.mkdir()
    dst = tmp_path / "new"
    weird = "agent..foo"  # `.` is sanitised → `_`
    _make_fake_flat_workspace(src, weird, with_worktree_admin=False)

    summary = migrator.migrate(src, dst)
    assert summary.moved_count == 1
    new_ws = dst / "t-default" / "default" / "default" / "agent__foo" / "legacy-hash"
    assert new_ws.is_dir()
