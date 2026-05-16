"""Integration tests for C1 phase-boundary snapshots via real ``git stash``
(SP-B-X-002a / OP-1060).

Exercises :func:`backend.agents.runner_progress.take_phase_snapshot`
and :func:`record_phase` against a real git worktree, covering the AC
§C1 contract:

  1. ``git stash push -m <label> --include-untracked`` (not the
     hash-only ``git stash create`` form).
  2. After push, ``git rev-parse stash@{0}`` resolves the new entry.
  3. ``git stash list --format=%gd:%s`` shows the labeled row.
  4. On the next pickup, :func:`find_recovered_snapshot` matches the
     persisted row against the live stash listing and returns the
     :class:`Progress` that drives the ``[progress-recovered]`` comment.

The synthetic dead-runner scenario (``record_phase`` then unconditional
process exit, next process can still resume) is the §C1 design-doc
"kill -9 mid-pickup" case end-to-end.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pytest

from backend.agents import runner_progress as rp


# ── Fixture: a freshly-initialised git worktree ───────────────────────


def _git(cwd: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr.strip()}")
    return result.stdout


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A fresh git repo with one tracked file + one commit on ``main``.
    Phase-snapshot tests can dirty the worktree (modify the tracked
    file, drop in untracked files) without contaminating the host's
    repo state."""
    _git(tmp_path, ["init", "-q", "-b", "main"])
    # CI-friendly identity — without a user.name/email the commit below
    # raises and the whole module fails to import.
    _git(tmp_path, ["config", "user.name", "test-runner"])
    _git(tmp_path, ["config", "user.email", "test@invalid"])
    (tmp_path / "README.md").write_text("seed\n")
    _git(tmp_path, ["add", "README.md"])
    _git(tmp_path, ["commit", "-q", "-m", "seed"])
    return tmp_path


# ── Snapshot on a dirty worktree ───────────────────────────────────────


def test_take_phase_snapshot_pushes_labeled_stash_when_dirty(worktree: Path) -> None:
    (worktree / "README.md").write_text("dirty edit\n")
    (worktree / "untracked.txt").write_text("hello\n")

    snap = rp.take_phase_snapshot(
        worktree, "working", "OP-1060", iso="2026-05-14T00:00:00Z",
    )

    assert snap.dirty is True
    assert snap.stash_ref  # hash returned from `git rev-parse stash@{0}`
    assert snap.stash_ref_name == "stash@{0}"
    assert snap.label == "phase-snapshot @ working @ 2026-05-14T00:00:00Z @ OP-1060"

    # The label is grep-able in `git stash list` per AC §C1 step 3.
    listing = _git(worktree, ["stash", "list", "--format=%gd:%s"])
    assert snap.label in listing

    # `git stash push --include-untracked` cleans both tracked + untracked
    # from the worktree; the test asserts what the design relies on:
    # post-snapshot, no dirty paths remain to confuse the next phase's
    # dirty check.
    status = _git(worktree, ["status", "--porcelain", "-uall"])
    assert status.strip() == ""


def test_take_phase_snapshot_returns_clean_when_worktree_is_clean(worktree: Path) -> None:
    snap = rp.take_phase_snapshot(worktree, "picking_up", "OP-1060")
    assert snap.dirty is False
    assert snap.stash_ref == ""
    assert snap.label == ""
    # No stash entry created — the operator should see an empty list.
    listing = _git(worktree, ["stash", "list"])
    assert listing.strip() == ""


def test_take_phase_snapshot_uses_push_form_not_create_form(worktree: Path) -> None:
    """``git stash create`` returns a hash but does NOT update
    ``refs/stash`` — the resulting commit is unreferenced and GC-eligible
    on the next `git gc --auto`. AC §C1 mandates the ``push`` form so the
    snapshot is locatable via label. This test exercises the
    post-condition that distinguishes them: ``push`` leaves a reachable
    ``refs/stash``, ``create`` does not.
    """
    (worktree / "README.md").write_text("dirty\n")
    rp.take_phase_snapshot(worktree, "working", "OP-1060")
    # `refs/stash` exists iff at least one stash entry has been pushed.
    refs = _git(worktree, ["for-each-ref", "refs/stash"])
    assert refs.strip(), "refs/stash missing — `create` form was used instead of `push`"


def test_take_phase_snapshot_ignores_progress_txt_when_otherwise_clean(worktree: Path) -> None:
    """Regression guard for the ``_worktree_dirty`` carve-out: writing
    progress.txt itself must not trigger a spurious stash on the next
    phase boundary."""
    rp.write_progress_atomic(worktree, rp.Progress(
        ticket_key="OP-1060",
        phase_completed="picking_up",
        phase_snapshot_stash_ref="",
        phase_snapshot_stash_label="",
        phase_snapshot_stash_ref_name="",
        iso_utc="2026-05-14T00:00:00Z",
    ))
    snap = rp.take_phase_snapshot(worktree, "working", "OP-1060")
    assert snap.dirty is False
    assert _git(worktree, ["stash", "list"]).strip() == ""


# ── record_phase + progress.txt round-trip ─────────────────────────────


def test_record_phase_persists_pointer_to_just_pushed_stash(worktree: Path) -> None:
    (worktree / "README.md").write_text("dirty\n")

    snap = rp.record_phase(
        worktree, "working", "OP-1060", iso="2026-05-14T00:00:00Z",
    )

    assert snap.stash_ref  # the push happened
    persisted = rp.read_progress(worktree)
    assert persisted is not None
    assert persisted.ticket_key == "OP-1060"
    assert persisted.phase_completed == "working"
    assert persisted.phase_snapshot_stash_ref == snap.stash_ref
    assert persisted.phase_snapshot_stash_label == snap.label
    assert persisted.phase_snapshot_stash_ref_name == "stash@{0}"


def test_record_phase_clean_worktree_writes_row_with_empty_ref(worktree: Path) -> None:
    snap = rp.record_phase(
        worktree, "picking_up", "OP-1060", iso="2026-05-14T00:00:00Z",
    )
    assert snap.dirty is False
    persisted = rp.read_progress(worktree)
    assert persisted is not None
    assert persisted.phase_completed == "picking_up"
    assert persisted.phase_snapshot_stash_ref == ""
    assert persisted.phase_snapshot_stash_label == ""


# ── find_recovered_snapshot — end-to-end resume case ───────────────────


def test_find_recovered_snapshot_returns_row_when_label_still_in_stash_list(worktree: Path) -> None:
    (worktree / "README.md").write_text("dirty before crash\n")
    snap = rp.record_phase(worktree, "working", "OP-1060", iso="2026-05-14T00:00:00Z")
    # Simulate process boundary — a fresh invocation just runs find_*.
    recovered = rp.find_recovered_snapshot(worktree)
    assert recovered is not None
    assert recovered.ticket_key == "OP-1060"
    assert recovered.phase_snapshot_stash_ref == snap.stash_ref


def test_find_recovered_snapshot_returns_none_when_operator_dropped_stash(worktree: Path) -> None:
    """If the operator already ran ``git stash drop`` between runs,
    the persisted progress.txt points to a ref that no longer exists.
    The recovery surface must NOT cry wolf — return None so the runner
    skips the ``[progress-recovered]`` comment."""
    (worktree / "README.md").write_text("dirty\n")
    rp.record_phase(worktree, "working", "OP-1060", iso="2026-05-14T00:00:00Z")
    # Operator restored + dropped the stash before re-pickup.
    _git(worktree, ["stash", "drop", "stash@{0}"])
    assert rp.find_recovered_snapshot(worktree) is None


def test_synthetic_dead_runner_scenario_recovers_via_label(worktree: Path) -> None:
    """End-to-end §C1 "kill -9 mid-pickup" case.

    Phase A: runner is mid-working, made local edits, recorded the
    phase, then was SIGKILL'd. The OS leaves the progress.txt + the
    stash on disk; both survive the death of the Python process.

    Phase B: fresh runner pickup re-reads progress.txt, cross-checks
    against `git stash list`, and surfaces a [progress-recovered]
    comment (the runner main loop does the JIRA POST; this test only
    asserts the Progress row + comment string the runner would post).
    """
    # ── Phase A ────────────────────────────────────────────────────
    (worktree / "README.md").write_text("partial work\n")
    (worktree / "WIP.txt").write_text("untracked WIP scratchpad\n")
    snap_a = rp.record_phase(worktree, "working", "OP-1060", iso="2026-05-14T00:00:00Z")
    assert snap_a.dirty
    # Process death — we don't even bother to clean up. The fs state
    # below is exactly what a SIGKILL leaves behind.

    # ── Phase B ────────────────────────────────────────────────────
    recovered = rp.find_recovered_snapshot(worktree)
    assert recovered is not None
    assert recovered.phase_completed == "working"
    assert recovered.phase_snapshot_stash_ref == snap_a.stash_ref

    comment = rp.format_recovered_comment(recovered)
    assert "[progress-recovered]" in comment
    assert f"stash_ref={snap_a.stash_ref}" in comment
    assert "phase-snapshot @ working @ 2026-05-14T00:00:00Z @ OP-1060" in comment
    # The recipe the operator pastes — the AC §C1 line `Operator: run ...`.
    assert "git stash apply stash@{0}" in comment


# ── Stress: multiple phase boundaries in a single pickup ──────────────


def test_two_phase_boundaries_each_create_distinct_stash_entries(worktree: Path) -> None:
    (worktree / "README.md").write_text("phase-A edits\n")
    snap_a = rp.record_phase(worktree, "picking_up", "OP-1060", iso="2026-05-14T00:00:00Z")

    # Dirty the worktree again to drive the second snapshot.
    (worktree / "README.md").write_text("phase-B edits\n")
    snap_b = rp.record_phase(worktree, "working", "OP-1060", iso="2026-05-14T00:00:01Z")

    assert snap_a.stash_ref and snap_b.stash_ref
    assert snap_a.stash_ref != snap_b.stash_ref

    listing = _git(worktree, ["stash", "list", "--format=%gd:%s"])
    assert snap_a.label in listing
    assert snap_b.label in listing

    # progress.txt is overwritten each phase — it only points to the latest.
    persisted = rp.read_progress(worktree)
    assert persisted is not None
    assert persisted.phase_completed == "working"
    assert persisted.phase_snapshot_stash_ref == snap_b.stash_ref
