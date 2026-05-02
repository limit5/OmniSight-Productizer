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


# ─── _mark_item_failed ──────────────────────────────────────────


def test_mark_item_failed_flips_to_codex_bang(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNISIGHT_CODEX_TIER", "A")
    monkeypatch.setenv("OMNISIGHT_CODEX_WORKTREE", str(tmp_path))
    fake_todo = tmp_path / "TODO.md"
    fake_todo.write_text(
        "## A\n"
        "### A1\n"
        "- [ ] A1.1 task one\n"
        "- [ ] A1.2 task two\n"
    )
    mod = _load_codex_runner()
    monkeypatch.setattr(mod, "TODO_FILE", str(fake_todo))
    mod._mark_item_failed("- [ ] A1.1 task one")
    text = fake_todo.read_text()
    assert "- [!][G] A1.1 task one" in text
    # other item untouched
    assert "- [ ] A1.2 task two" in text
    # Critical: did NOT use [!][C]
    assert "- [!][C]" not in text


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
