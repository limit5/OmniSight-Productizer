"""Unit tests for the C1 progress.txt schema + atomic-write surface
(SP-B-X-002a / OP-1060).

Covers the durability contract of :mod:`backend.agents.runner_progress`:
schema round-trip, fsync + rename atomicity, malformed-input
robustness, and the ``find_recovered_snapshot`` semantics that drive
the ``[progress-recovered]`` operator comment surface.

The git-side integration (real `git stash push` against a real
worktree) lives in ``tests/test_phase_snapshot_via_git_stash.py`` —
keeping the two suites split lets this one stay subprocess-free + fast
while the other covers the end-to-end crash scenario.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pytest

from backend.agents import runner_progress as rp


# ── Schema round-trip ──────────────────────────────────────────────────


def test_progress_round_trips_through_json() -> None:
    p = rp.Progress(
        ticket_key="OP-1060",
        phase_completed="working",
        phase_snapshot_stash_ref="abc1234",
        phase_snapshot_stash_label="phase-snapshot @ working @ 2026-05-14T00:00:00Z @ OP-1060",
        phase_snapshot_stash_ref_name="stash@{0}",
        iso_utc="2026-05-14T00:00:00Z",
    )
    decoded = rp.Progress.from_json(p.to_json())
    assert decoded == p


def test_progress_from_json_tolerates_missing_optional_fields() -> None:
    # AC §C1 only mandates phase_completed + phase_snapshot_stash_ref;
    # the supporting fields (label, ref_name, iso) default to "" so an
    # older row written before the schema was complete still parses.
    raw = json.dumps({
        "ticket_key": "OP-9",
        "phase_completed": "picking_up",
        "phase_snapshot_stash_ref": "",
    })
    parsed = rp.Progress.from_json(raw)
    assert parsed.ticket_key == "OP-9"
    assert parsed.phase_completed == "picking_up"
    assert parsed.phase_snapshot_stash_ref == ""
    assert parsed.phase_snapshot_stash_label == ""
    assert parsed.phase_snapshot_stash_ref_name == ""


def test_progress_from_json_rejects_non_object_root() -> None:
    with pytest.raises(ValueError, match="root is list"):
        rp.Progress.from_json("[1, 2, 3]")


# ── Label generation (AC §C1 format contract) ──────────────────────────


def test_build_stash_label_has_four_at_separated_fields() -> None:
    label = rp.build_stash_label("working", "OP-1060", iso="2026-05-14T00:00:00Z")
    parts = label.split(" @ ")
    assert parts == ["phase-snapshot", "working", "2026-05-14T00:00:00Z", "OP-1060"]


def test_build_stash_label_defaults_iso_to_utc_now() -> None:
    label = rp.build_stash_label("submitting", "OP-1")
    # Format `YYYY-MM-DDTHH:MM:SSZ` — Z-suffixed UTC, no fractional seconds.
    iso_field = label.split(" @ ")[2]
    assert iso_field.endswith("Z")
    assert len(iso_field) == 20


# ── Atomic write semantics (AC §C1) ────────────────────────────────────


def _row(ticket: str, phase: str, ref: str = "") -> rp.Progress:
    return rp.Progress(
        ticket_key=ticket,
        phase_completed=phase,
        phase_snapshot_stash_ref=ref,
        phase_snapshot_stash_label=(
            f"phase-snapshot @ {phase} @ 2026-05-14T00:00:00Z @ {ticket}" if ref else ""
        ),
        phase_snapshot_stash_ref_name="stash@{0}" if ref else "",
        iso_utc="2026-05-14T00:00:00Z",
    )


def test_write_progress_atomic_creates_file_and_no_tmp_residue(tmp_path: Path) -> None:
    rp.write_progress_atomic(tmp_path, _row("OP-1", "picking_up"))
    final = tmp_path / rp.PROGRESS_FILENAME
    tmp = tmp_path / (rp.PROGRESS_FILENAME + ".tmp")
    assert final.exists()
    # Atomic-rename contract: the .tmp must not survive a successful write.
    assert not tmp.exists()
    body = json.loads(final.read_text().strip())
    assert body["ticket_key"] == "OP-1"
    assert body["phase_completed"] == "picking_up"


def test_write_progress_atomic_overwrites_prior_row(tmp_path: Path) -> None:
    rp.write_progress_atomic(tmp_path, _row("OP-1", "picking_up"))
    rp.write_progress_atomic(tmp_path, _row("OP-1", "working", ref="abc1234"))
    parsed = rp.read_progress(tmp_path)
    assert parsed is not None
    assert parsed.phase_completed == "working"
    assert parsed.phase_snapshot_stash_ref == "abc1234"


def test_write_progress_atomic_truncates_when_new_row_shorter(tmp_path: Path) -> None:
    """Regression guard: a naive open(mode='w') overwrite truncates by
    default, but `os.open` requires `O_TRUNC` explicitly. If the write
    path forgets the flag, a shorter second row leaves stale trailing
    bytes that break JSON parsing."""
    rp.write_progress_atomic(tmp_path, _row("OP-1", "submitting", ref="a" * 40))
    rp.write_progress_atomic(tmp_path, _row("OP-1", "picking_up"))  # shorter row
    parsed = rp.read_progress(tmp_path)
    assert parsed is not None
    assert parsed.phase_completed == "picking_up"
    assert parsed.phase_snapshot_stash_ref == ""


# ── Read robustness ────────────────────────────────────────────────────


def test_read_progress_returns_none_when_file_absent(tmp_path: Path) -> None:
    assert rp.read_progress(tmp_path) is None


def test_read_progress_returns_none_on_empty_file(tmp_path: Path) -> None:
    (tmp_path / rp.PROGRESS_FILENAME).write_text("")
    assert rp.read_progress(tmp_path) is None


def test_read_progress_returns_none_on_malformed_json(tmp_path: Path) -> None:
    (tmp_path / rp.PROGRESS_FILENAME).write_text("not json {{{")
    # Malformed = torn write (host crash between fsync + rename). Must
    # not raise into the runner main loop; a fresh pickup recovers.
    assert rp.read_progress(tmp_path) is None


def test_read_progress_returns_none_when_root_is_list(tmp_path: Path) -> None:
    (tmp_path / rp.PROGRESS_FILENAME).write_text("[1, 2]")
    assert rp.read_progress(tmp_path) is None


# ── format_recovered_comment (operator contract) ───────────────────────


def test_format_recovered_comment_matches_ac_template() -> None:
    p = _row("OP-1060", "working", ref="abc1234def")
    comment = rp.format_recovered_comment(p)
    # All four AC §C1 anchors that an operator greps for must be present.
    assert "[progress-recovered]" in comment
    assert "phase=working" in comment
    assert "stash_ref=abc1234def" in comment
    assert 'stash_label="phase-snapshot @ working @ ' in comment
    assert "git stash apply" in comment


def test_format_recovered_comment_uses_ref_name_in_recipe_when_present() -> None:
    """Operators prefer ``stash@{0}`` over a raw hash in the recipe."""
    p = _row("OP-1060", "working", ref="abc1234")
    comment = rp.format_recovered_comment(p)
    assert "git stash apply stash@{0}" in comment


def test_format_recovered_comment_falls_back_to_hash_when_ref_name_missing() -> None:
    p = rp.Progress(
        ticket_key="OP-1060",
        phase_completed="working",
        phase_snapshot_stash_ref="abc1234",
        phase_snapshot_stash_label="phase-snapshot @ working @ ts @ OP-1060",
        phase_snapshot_stash_ref_name="",
        iso_utc="2026-05-14T00:00:00Z",
    )
    comment = rp.format_recovered_comment(p)
    assert "git stash apply abc1234" in comment


# ── find_recovered_snapshot — pre-git short-circuits ──────────────────


def test_find_recovered_snapshot_returns_none_when_no_progress(tmp_path: Path) -> None:
    assert rp.find_recovered_snapshot(tmp_path) is None


def test_find_recovered_snapshot_returns_none_when_ref_empty(tmp_path: Path) -> None:
    """A clean-worktree phase still writes a bookkeeping row, but with
    empty ``phase_snapshot_stash_ref``. The resume surface must NOT
    comment on it — there is nothing to apply."""
    rp.write_progress_atomic(tmp_path, _row("OP-1060", "picking_up"))
    assert rp.find_recovered_snapshot(tmp_path) is None


def test_find_recovered_snapshot_returns_none_when_label_empty(tmp_path: Path) -> None:
    bogus = rp.Progress(
        ticket_key="OP-1060",
        phase_completed="working",
        phase_snapshot_stash_ref="abc1234",
        phase_snapshot_stash_label="",  # malformed: ref present but no label
        phase_snapshot_stash_ref_name="stash@{0}",
        iso_utc="ts",
    )
    rp.write_progress_atomic(tmp_path, bogus)
    assert rp.find_recovered_snapshot(tmp_path) is None


# ── _label_in_stash_list — match semantics ────────────────────────────


def test_label_in_stash_list_matches_on_subject_suffix() -> None:
    label = "phase-snapshot @ working @ 2026-05-14T00:00:00Z @ OP-1060"
    listing = "\n".join([
        "stash@{0}:On develop: " + label,
        "stash@{1}:WIP on develop: phase-snapshot @ picking_up @ ts @ OP-1060",
    ])
    assert rp._label_in_stash_list(listing, label) is True


def test_label_in_stash_list_no_match_when_label_absent() -> None:
    listing = "stash@{0}:On develop: other unrelated stash"
    assert rp._label_in_stash_list(listing, "phase-snapshot @ x @ y @ z") is False


def test_label_in_stash_list_handles_empty_listing() -> None:
    assert rp._label_in_stash_list("", "phase-snapshot @ x @ y @ z") is False
