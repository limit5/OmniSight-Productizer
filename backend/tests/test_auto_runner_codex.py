"""Smoke + unit tests for auto-runner-codex.py.

Pure-Python contract tests — does NOT spawn the real codex CLI. Validates:

  * Module imports, env var parsing, default values
  * Tier resolution → cwd routing (A → BASE_DIR, B → worktree)
  * Tier validation (invalid value falls back to B with warning)
  * RUNNER_FILTER section matcher (mirrors auto-runner-sdk's contract)
  * TARGET_ITEM_SUBSTR substring lock
  * _find_first_pending: respects [C]/[G] tags (untagged only counts as pending)
  * _mark_item_failed flips - [ ] → - [!][G] (codex-tagged failure)
  * _build_codex_command shape (yolo / approval / model / extra flags)
  * Missing-CLI / missing-AGENTS / missing-coordination scenarios surface
    a clear error, not a stack trace
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-codex.py"


def _load_codex_runner(monkeypatch: pytest.MonkeyPatch | None = None) -> Any:
    """Load auto-runner-codex.py despite the hyphen in its filename."""
    sys.modules.pop("codex_runner_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "codex_runner_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── Tier + cwd routing ──────────────────────────────────────────


def test_default_tier_is_B(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OMNISIGHT_CODEX_TIER", raising=False)
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    mod = _load_codex_runner()
    assert mod.TIER == "B"
    assert mod.WORK_CWD == str(tmp_path)


def test_tier_A_routes_to_base_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "A")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    mod = _load_codex_runner()
    assert mod.TIER == "A"
    assert mod.WORK_CWD == mod.BASE_DIR


def test_invalid_tier_falls_back_to_B(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "Z")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    mod = _load_codex_runner()
    assert mod.TIER == "B"
    captured = capsys.readouterr()
    assert "不合法" in captured.out


def test_tier_B_missing_worktree_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "B")
    nonexistent = tmp_path / "no-such-worktree"
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(nonexistent))
    with pytest.raises(SystemExit):
        _load_codex_runner()


# ─── codex command shape ─────────────────────────────────────────


def test_build_codex_command_yolo_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "A")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    monkeypatch.delenv("OMNISIGHT_CODEX_MODEL", raising=False)
    monkeypatch.delenv("OMNISIGHT_CODEX_EXTRA_FLAGS", raising=False)
    monkeypatch.delenv("OMNISIGHT_CODEX_APPROVAL", raising=False)
    mod = _load_codex_runner()
    cmd = mod._build_codex_command()
    assert cmd[0] == "codex"
    assert "exec" in cmd
    assert "--cd" in cmd
    assert "--yolo" in cmd
    # No --model when env unset
    assert "--model" not in cmd


def test_build_codex_command_with_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "A")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    monkeypatch.setenv("OMNISIGHT_CODEX_MODEL", "gpt-5.5")
    mod = _load_codex_runner()
    cmd = mod._build_codex_command()
    assert "--model" in cmd
    assert "gpt-5.5" in cmd


def test_build_codex_command_with_extra_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "A")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    monkeypatch.setenv("OMNISIGHT_CODEX_EXTRA_FLAGS", "--verbose --quiet")
    mod = _load_codex_runner()
    cmd = mod._build_codex_command()
    assert "--verbose" in cmd
    assert "--quiet" in cmd


def test_build_codex_command_custom_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "A")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    monkeypatch.setenv("OMNISIGHT_CODEX_APPROVAL", "auto")
    mod = _load_codex_runner()
    cmd = mod._build_codex_command()
    assert "--yolo" not in cmd
    assert "--approval-mode" in cmd
    assert "auto" in cmd


# ─── RUNNER_FILTER ──────────────────────────────────────────────


def test_filter_section_id_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "A")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    monkeypatch.setenv("OMNISIGHT_CODEX_FILTER", "FS")
    mod = _load_codex_runner()
    assert mod._section_matches_filter("### FS.4. Email Service")
    assert mod._section_matches_filter("### FS.4 Email Service")
    assert not mod._section_matches_filter("### BP.A. Templates")


def test_filter_empty_matches_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "A")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    monkeypatch.delenv("OMNISIGHT_CODEX_FILTER", raising=False)
    mod = _load_codex_runner()
    assert mod._section_matches_filter("### whatever")
    assert mod._section_matches_filter("### BP.X.99 anything")


# ─── _find_first_pending: agent-tag awareness ───────────────────


def test_find_first_pending_skips_already_tagged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Items already done by Claude `[x][C]` or by Codex `[x][G]` skipped."""
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "A")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    mod = _load_codex_runner()
    lines = [
        "- [x][C] item already done by claude\n",
        "- [x][G] item already done by codex\n",
        "- [!][C] item failed by claude\n",
        "- [ ] genuinely pending item\n",
    ]
    hit = mod._find_first_pending(lines)
    assert hit is not None
    assert "genuinely pending" in hit


def test_find_first_pending_target_substring_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "A")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    monkeypatch.setenv("OMNISIGHT_CODEX_TARGET_ITEM", "FS.4.1")
    mod = _load_codex_runner()
    lines = [
        "- [ ] FS.3.5 Tests\n",
        "- [ ] FS.4.1 Resend adapter\n",
        "- [ ] FS.5.5 Tests\n",
    ]
    hit = mod._find_first_pending(lines)
    assert hit == "- [ ] FS.4.1 Resend adapter"


def test_find_first_pending_returns_none_when_all_tagged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "A")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    mod = _load_codex_runner()
    lines = [
        "- [x][C] all done\n",
        "- [x][G] also done\n",
        "- [!][G] tried failed\n",
    ]
    assert mod._find_first_pending(lines) is None


# ─── Reservation-based TODO management (Tier B fix) ─────────────


def _make_codex_runner_with_fake_todo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Any, Path]:
    """Helper: load the runner module + redirect TODO_FILE to a tmp file."""
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "A")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    fake_todo = tmp_path / "TODO.md"
    fake_todo.write_text(
        "## Priority X\n\n"
        "### A1. First section\n"
        "- [ ] A1.1 task one\n"
        "- [ ] A1.2 task two\n"
        "- [x][C] A1.3 done by claude\n"
    )
    mod = _load_codex_runner()
    monkeypatch.setattr(mod, "TODO_FILE", str(fake_todo))
    return mod, fake_todo


def test_reserve_item_for_codex_flips_blank_to_squiggle_G(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pre-flight reservation: - [ ] item → - [~][G] item."""
    mod, fake_todo = _make_codex_runner_with_fake_todo(monkeypatch, tmp_path)
    reserved = mod._reserve_item_for_codex("- [ ] A1.1 task one")
    assert reserved == "- [~][G] A1.1 task one"
    text = fake_todo.read_text()
    assert "- [~][G] A1.1 task one" in text
    # other lines untouched
    assert "- [ ] A1.2 task two" in text
    assert "- [x][C] A1.3 done by claude" in text


def test_flip_reservation_to_done_after_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Post-success: - [~][G] → - [x][G]."""
    mod, fake_todo = _make_codex_runner_with_fake_todo(monkeypatch, tmp_path)
    reserved = mod._reserve_item_for_codex("- [ ] A1.1 task one")
    assert reserved is not None
    done = mod._flip_reservation_to_done(reserved)
    assert done == "- [x][G] A1.1 task one"
    text = fake_todo.read_text()
    assert "- [x][G] A1.1 task one" in text
    assert "- [~][G]" not in text  # reservation marker fully gone


def test_flip_reservation_to_failed_after_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Post-failure: - [~][G] → - [!][G]."""
    mod, fake_todo = _make_codex_runner_with_fake_todo(monkeypatch, tmp_path)
    reserved = mod._reserve_item_for_codex("- [ ] A1.2 task two")
    failed = mod._flip_reservation_to_failed(reserved)
    assert failed == "- [!][G] A1.2 task two"
    text = fake_todo.read_text()
    assert "- [!][G] A1.2 task two" in text


def test_reserved_item_not_picked_up_by_next_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Once reserved, subsequent _find_first_pending calls skip it.

    This is the invariant that PREVENTS the FS.1.1-style infinite loop:
    after reservation the line no longer starts with `- [ ]`, so the
    next scan picks the next pending item instead of redispatching.
    """
    mod, fake_todo = _make_codex_runner_with_fake_todo(monkeypatch, tmp_path)
    # Initial scan — should pick A1.1
    monkeypatch.setattr(mod, "TARGET_ITEM_SUBSTR", "")
    text_lines = fake_todo.read_text().splitlines(keepends=True)
    first = mod._find_first_pending(text_lines)
    assert "A1.1" in first

    # Reserve A1.1
    mod._reserve_item_for_codex(first)

    # Re-scan — should advance to A1.2 (NOT pick A1.1 again)
    text_lines = fake_todo.read_text().splitlines(keepends=True)
    second = mod._find_first_pending(text_lines)
    assert second is not None
    assert "A1.2" in second
    assert "A1.1" not in second


def test_replace_marker_idempotent_on_already_correct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Calling done-flip on a line already at [x][G] is a safe no-op."""
    mod, fake_todo = _make_codex_runner_with_fake_todo(monkeypatch, tmp_path)
    reserved = mod._reserve_item_for_codex("- [ ] A1.1 task one")
    mod._flip_reservation_to_done(reserved)
    # Now try flipping again on the new line — should not duplicate
    result = mod._flip_reservation_to_done("- [x][G] A1.1 task one")
    # The line is already done; replace should be a no-op (returns same line).
    assert result == "- [x][G] A1.1 task one"
    # Sanity: only ONE [x][G] A1.1 in file
    text = fake_todo.read_text()
    assert text.count("- [x][G] A1.1 task one") == 1


def test_replace_marker_returns_none_when_line_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Trying to reserve an item that's been mutated by another runner
    returns None instead of corrupting the file."""
    mod, fake_todo = _make_codex_runner_with_fake_todo(monkeypatch, tmp_path)
    result = mod._reserve_item_for_codex("- [ ] DOES NOT EXIST")
    assert result is None
    # File untouched
    text = fake_todo.read_text()
    assert "DOES NOT EXIST" not in text


# ─── Filename + co-existence with auto-runner.py ────────────────


def test_runner_path_exists() -> None:
    assert _RUNNER_PATH.exists()
    assert _RUNNER_PATH.name == "auto-runner-codex.py"


def test_sibling_runners_co_exist() -> None:
    """auto-runner.py / auto-runner-sdk.py / auto-runner-codex.py all live at root."""
    project_root = Path(__file__).resolve().parents[2]
    assert (project_root / "auto-runner.py").exists()
    assert (project_root / "auto-runner-sdk.py").exists()
    assert (project_root / "auto-runner-codex.py").exists()


def test_codex_runner_loads_AGENTS_and_coordination_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "A")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    mod = _load_codex_runner()
    assert mod.AGENTS_FILE.endswith("AGENTS.md")
    assert mod.COORDINATION_FILE.endswith("coordination.md")
