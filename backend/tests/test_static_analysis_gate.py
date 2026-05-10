"""OP-829 static-analysis pre-flight gate tests."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.agents import static_analysis_gate as gate
from backend.agents.tool_dispatcher import StructuredToolError, ToolDispatcher

REPO_ROOT = Path(__file__).resolve().parents[2]


def _completed(
    cmd: list[str],
    *,
    rc: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, rc, stdout, stderr)


class _Runner:
    def __init__(self, outputs: dict[str, subprocess.CompletedProcess[str]]) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return self.outputs.get(cmd[0], _completed(cmd))


@pytest.fixture(autouse=True)
def _all_linters_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_clean_lint_passes_and_dispatches_python_linters(tmp_path: Path) -> None:
    target = tmp_path / "ok.py"
    target.write_text("x: int = 1\n")
    progress = tmp_path / "progress.txt"
    runner = _Runner({})

    result = gate.run_static_analysis(
        [target],
        worktree_root=tmp_path,
        progress_path=progress,
        runner=runner,
    )

    assert result.status == "clean"
    assert result.diagnostics == ()
    assert [call[0] for call in runner.calls] == ["ruff", "mypy"]
    assert not progress.exists()


def test_dirty_first_round_feedback_then_clean_resolves(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text("print(1)\n")
    progress = tmp_path / "progress.txt"
    runner = _Runner(
        {
            "ruff": _completed(
                ["ruff"],
                rc=1,
                stdout=json.dumps(
                    [
                        {
                            "filename": str(target),
                            "location": {"row": 1, "column": 1},
                            "code": "T201",
                            "message": "print found",
                        }
                    ]
                ),
            ),
        }
    )

    dirty = gate.run_static_analysis(
        [target],
        worktree_root=tmp_path,
        progress_path=progress,
        runner=runner,
    )
    clean = gate.run_static_analysis(
        [target],
        worktree_root=tmp_path,
        progress_path=progress,
        runner=_Runner({}),
    )

    assert dirty.status == "dirty"
    assert dirty.lint_round_counter == 1
    assert dirty.diagnostics[0].to_dict() == {
        "file": str(target),
        "line": 1,
        "col": 1,
        "code": "T201",
        "message": "print found",
    }
    assert "lint_round_counter=1" in progress.read_text()
    assert clean.status == "clean"
    assert clean.lint_round_counter == 1


def test_dirty_third_round_sets_lint_partial_and_persists_cap(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text("x = ''\n")
    progress = tmp_path / "progress.txt"
    progress.write_text("lint_round_counter=2\n")
    runner = _Runner(
        {
            "mypy": _completed(
                ["mypy"],
                rc=1,
                stdout=f"{target}:1:5: error: Incompatible types [assignment]\n",
            )
        }
    )

    result = gate.run_static_analysis(
        [target],
        worktree_root=tmp_path,
        progress_path=progress,
        runner=runner,
    )

    assert result.status == "lint_partial"
    assert result.lint_partial is True
    assert result.lint_round_counter == 3
    assert "lint_partial=true" in progress.read_text()


def test_corrupt_progress_counter_resets_to_zero(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text("print(1)\n")
    progress = tmp_path / "progress.txt"
    progress.write_text("lint_round_counter=not-an-int\n")
    runner = _Runner(
        {
            "ruff": _completed(
                ["ruff"],
                rc=1,
                stdout=json.dumps(
                    [
                        {
                            "filename": str(target),
                            "location": {"row": 1, "column": 1},
                            "code": "T201",
                            "message": "print found",
                        }
                    ]
                ),
            ),
        }
    )

    result = gate.run_static_analysis(
        [target],
        worktree_root=tmp_path,
        progress_path=progress,
        runner=runner,
    )

    assert result.lint_round_counter == 1
    assert "lint_round_counter=1" in progress.read_text()


def test_missing_binary_degrades_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "ok.py"
    target.write_text("x = 1\n")
    monkeypatch.setattr(gate.shutil, "which", lambda _name: None)

    result = gate.run_static_analysis(
        [target],
        worktree_root=tmp_path,
        runner=_Runner({}),
    )

    assert result.status == "clean"
    assert result.diagnostics == ()
    assert result.warnings == (
        "linter_binary_missing: ruff",
        "linter_binary_missing: mypy",
    )


def test_config_conflict_warns_and_uses_defaults(tmp_path: Path) -> None:
    target = tmp_path / "bad.ts"
    target.write_text("const x: string = 1\n")
    runner = _Runner(
        {
            "eslint": _completed(
                ["eslint"],
                rc=2,
                stderr="Error: failed to load config; config conflict",
            ),
        }
    )

    result = gate.run_static_analysis(
        [target],
        worktree_root=tmp_path,
        runner=runner,
    )

    assert result.status == "clean"
    assert result.warnings == ("linter_config_conflict: eslint; using defaults",)


def test_target_outside_worktree_raises_sandbox_boundary_violation(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "x.py"
    outside.write_text("x = 1\n")

    with pytest.raises(StructuredToolError) as exc:
        gate.run_static_analysis([outside], worktree_root=tmp_path)

    assert exc.value.error_code == "sandbox_boundary_violation"
    assert "lint_target_outside_worktree" in exc.value.hint


def test_text_editor_wrapper_feeds_structured_lint_output_to_next_turn(
    tmp_path: Path,
) -> None:
    target = tmp_path / "bad.ts"
    target.write_text("const x: number = 1\n")
    runner = _Runner(
        {
            "tsc": _completed(
                ["tsc"],
                rc=1,
                stdout=f"{target}(1,7): error TS2322: Type 'string' is not assignable\n",
            )
        }
    )

    def write_handler(_payload: dict[str, Any]) -> str:
        return "replaced 1 occurrence"

    wrapped = gate.wrap_text_editor_with_static_analysis(
        write_handler,
        worktree_root=tmp_path,
        progress_path=tmp_path / "progress.txt",
        runner=runner,
    )
    output = wrapped({"command": "str_replace", "path": str(target)})

    assert "static_analysis_gate:" in output
    payload = json.loads(output.split("static_analysis_gate:\n", 1)[1])
    assert payload["status"] == "dirty"
    assert payload["diagnostics"][0]["code"] == "TS2322"


def test_s1_launcher_registers_linting_text_editor(tmp_path: Path) -> None:
    name = "s1_launcher_static_analysis_under_test"
    spec = importlib.util.spec_from_file_location(
        name,
        REPO_ROOT / "scripts" / "run_s1_via_anthropic_sdk.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)

    dispatcher = ToolDispatcher()
    mod.bind_built_in_tools_with_static_analysis(
        dispatcher,
        worktree_root=tmp_path,
        progress_path=tmp_path / "progress.txt",
    )

    assert dispatcher.has_handler("str_replace_based_edit_tool")
    assert dispatcher.has_handler("bash")
    assert dispatcher.has_handler("code_execution")


def test_s1_launcher_marks_lint_partial_commit_subject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "s1_launcher_lint_partial_under_test"
    spec = importlib.util.spec_from_file_location(
        name,
        REPO_ROOT / "scripts" / "run_s1_via_anthropic_sdk.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    progress = tmp_path / "progress.txt"
    progress.write_text("lint_round_counter=3\nlint_partial=true\n")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:3] == ["git", "log", "-1"]:
            return _completed(cmd, stdout="[OP-829] implement gate\n\nbody")
        return _completed(cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert mod._mark_lint_partial_commit(tmp_path, progress) is True
    assert calls[-1] == [
        "git",
        "commit",
        "--amend",
        "-m",
        "[lint_partial] [OP-829] implement gate\n\nbody",
    ]
