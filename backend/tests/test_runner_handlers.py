"""Tests for runner_handlers — auto-runner-sdk's tool layer.

Locks:
  - All 6 handlers happy path
  - Path safety: every path-taking handler rejects BASE_DIR escapes
    (relative climb + absolute outside)
  - Edit refuses non-unique old_string unless replace_all=True
  - Bash respects timeout + cwd
  - run_in_background is rejected (would orphan procs)
  - bind_to_dispatcher / make_runner_dispatcher wire all 6 names
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from backend.agents import runner_handlers
from backend.agents.runner_handlers import (
    bash_handler,
    bind_to_dispatcher,
    edit_handler,
    glob_handler,
    grep_handler,
    make_runner_dispatcher,
    read_handler,
    write_handler,
)
from backend.agents.tool_dispatcher import ToolDispatcher


@pytest.fixture
def base_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox BASE_DIR to a temp dir for the duration of the test."""
    monkeypatch.setattr(runner_handlers, "BASE_DIR", tmp_path.resolve())
    return tmp_path.resolve()


# ─── Read ────────────────────────────────────────────────────────


def test_read_returns_numbered_lines(base_dir: Path) -> None:
    f = base_dir / "hello.txt"
    f.write_text("alpha\nbeta\ngamma\n")
    out = read_handler({"file_path": str(f)})
    assert "1\talpha" in out
    assert "2\tbeta" in out
    assert "3\tgamma" in out


def test_read_honors_offset_and_limit(base_dir: Path) -> None:
    f = base_dir / "many.txt"
    f.write_text("\n".join(f"line{i}" for i in range(10)))
    # offset=3 means start at line 3 (1-indexed) → line2 (0-indexed)
    out = read_handler({"file_path": str(f), "offset": 3, "limit": 2})
    assert "3\tline2" in out
    assert "4\tline3" in out
    assert "line0" not in out
    assert "line5" not in out


def test_read_rejects_path_outside_base(
    base_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    f = outside / "secret.txt"
    f.write_text("nope")
    with pytest.raises(PermissionError):
        read_handler({"file_path": str(f)})


def test_read_missing_file_raises(base_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_handler({"file_path": str(base_dir / "ghost.txt")})


# ─── Write ───────────────────────────────────────────────────────


def test_write_creates_file_and_parent_dir(base_dir: Path) -> None:
    target = base_dir / "sub" / "nested" / "out.txt"
    res = write_handler({"file_path": str(target), "content": "hi"})
    assert target.read_text() == "hi"
    assert "Wrote 2 chars" in res


def test_write_overwrites_existing(base_dir: Path) -> None:
    f = base_dir / "x.txt"
    f.write_text("old")
    write_handler({"file_path": str(f), "content": "new"})
    assert f.read_text() == "new"


def test_write_rejects_path_outside_base(base_dir: Path) -> None:
    with pytest.raises(PermissionError):
        write_handler(
            {"file_path": "/etc/passwd_test", "content": "pwned"}
        )


def test_write_rejects_relative_climb(base_dir: Path) -> None:
    # ../../etc/foo gets resolved against BASE_DIR; if it climbs out → reject
    with pytest.raises(PermissionError):
        write_handler(
            {"file_path": "../../../tmp/foo.txt", "content": "x"}
        )


# ─── Edit ────────────────────────────────────────────────────────


def test_edit_replaces_unique_match(base_dir: Path) -> None:
    f = base_dir / "code.py"
    f.write_text("def foo():\n    return 1\n")
    edit_handler(
        {
            "file_path": str(f),
            "old_string": "return 1",
            "new_string": "return 2",
        }
    )
    assert "return 2" in f.read_text()


def test_edit_refuses_non_unique_without_replace_all(base_dir: Path) -> None:
    f = base_dir / "dup.py"
    f.write_text("x = 1\nx = 1\n")
    with pytest.raises(ValueError, match="not unique"):
        edit_handler(
            {"file_path": str(f), "old_string": "x = 1", "new_string": "x = 2"}
        )


def test_edit_replace_all_handles_multiple(base_dir: Path) -> None:
    f = base_dir / "dup.py"
    f.write_text("x = 1\nx = 1\n")
    res = edit_handler(
        {
            "file_path": str(f),
            "old_string": "x = 1",
            "new_string": "x = 2",
            "replace_all": True,
        }
    )
    assert f.read_text() == "x = 2\nx = 2\n"
    assert "Replaced 2 occurrence" in res


def test_edit_refuses_identical_strings(base_dir: Path) -> None:
    f = base_dir / "x.txt"
    f.write_text("hello")
    with pytest.raises(ValueError, match="identical"):
        edit_handler(
            {"file_path": str(f), "old_string": "hello", "new_string": "hello"}
        )


def test_edit_old_string_not_found(base_dir: Path) -> None:
    f = base_dir / "x.txt"
    f.write_text("hello")
    with pytest.raises(ValueError, match="not found"):
        edit_handler(
            {"file_path": str(f), "old_string": "missing", "new_string": "x"}
        )


def test_edit_rejects_path_outside_base(base_dir: Path) -> None:
    with pytest.raises(PermissionError):
        edit_handler(
            {
                "file_path": "/etc/hosts",
                "old_string": "127",
                "new_string": "x",
            }
        )


# ─── Bash ────────────────────────────────────────────────────────


def test_bash_returns_stdout_and_exit_code(base_dir: Path) -> None:
    out = bash_handler({"command": "echo hello"})
    assert "STDOUT:\nhello" in out
    assert "EXIT_CODE: 0" in out


def test_bash_runs_in_base_dir(base_dir: Path) -> None:
    out = bash_handler({"command": "pwd"})
    assert str(base_dir) in out


def test_bash_captures_nonzero_exit(base_dir: Path) -> None:
    out = bash_handler({"command": "false"})
    assert "EXIT_CODE: 1" in out


def test_bash_timeout_returns_message(base_dir: Path) -> None:
    out = bash_handler({"command": "sleep 5", "timeout": 1000})
    assert "timed out" in out


def test_bash_rejects_run_in_background_before_subprocess(
    base_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def fake_run(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(runner_handlers.subprocess, "run", fake_run)
    with pytest.raises(NotImplementedError, match="background"):
        bash_handler({"command": "true", "run_in_background": True})
    assert calls == []


@pytest.mark.parametrize(
    "bad_cmd",
    [
        "echo hi; rm -rf /",
        "echo hi | tee out",
        "echo hi && false",
        "echo $(whoami)",
        "echo `whoami`",
        "cat < /etc/passwd",
        "echo hi > out.txt",
        "echo hi\nrm -rf /",
    ],
)
def test_bash_rejects_shell_metacharacters(base_dir: Path, bad_cmd: str) -> None:
    """Shell metacharacters were the RCE vector pre-FX.1.4; reject outright."""
    with pytest.raises(ValueError, match="shell metacharacter"):
        bash_handler({"command": bad_cmd})


@pytest.mark.parametrize("bad_cmd", ["", "   ", "\t\n"])
def test_bash_rejects_empty_command(base_dir: Path, bad_cmd: str) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        bash_handler({"command": bad_cmd})


def test_bash_rejects_non_string_command(base_dir: Path) -> None:
    with pytest.raises(ValueError, match="must be a string"):
        bash_handler({"command": ["echo", "hi"]})


def test_bash_handles_quoted_args(base_dir: Path) -> None:
    """shlex.split honours quotes — multi-word quoted args stay intact."""
    out = bash_handler({"command": 'echo "hello world"'})
    assert "STDOUT:\nhello world" in out
    assert "EXIT_CODE: 0" in out


def test_bash_no_shell_means_glob_is_literal(base_dir: Path) -> None:
    """With shell=False, `*` is not expanded — it reaches the program literally."""
    out = bash_handler({"command": "echo *.py"})
    assert "STDOUT:\n*.py" in out


# ─── Grep ────────────────────────────────────────────────────────


def test_grep_finds_matches(base_dir: Path) -> None:
    (base_dir / "a.py").write_text("hello world\nbye world\n")
    (base_dir / "b.py").write_text("nothing here\n")
    out = grep_handler(
        {
            "pattern": "hello",
            "output_mode": "files_with_matches",
        }
    )
    assert "a.py" in out
    assert "b.py" not in out


def test_grep_no_matches_is_not_error(base_dir: Path) -> None:
    (base_dir / "a.py").write_text("nothing\n")
    out = grep_handler({"pattern": "zzzunmatchable"})
    # no error; just empty / minimal output
    assert "❌" not in out


def test_grep_count_mode(base_dir: Path) -> None:
    (base_dir / "a.py").write_text("foo\nfoo\nbar\n")
    out = grep_handler(
        {"pattern": "foo", "output_mode": "count", "path": str(base_dir)}
    )
    assert "2" in out


def test_grep_rejects_path_outside_base(
    base_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("else")
    with pytest.raises(PermissionError):
        grep_handler({"pattern": "x", "path": str(outside)})


# ─── Glob ────────────────────────────────────────────────────────


def test_glob_matches_pattern(base_dir: Path) -> None:
    (base_dir / "a.py").write_text("x")
    (base_dir / "b.py").write_text("x")
    (base_dir / "c.txt").write_text("x")
    out = glob_handler({"pattern": "*.py"})
    lines = [ln for ln in out.splitlines() if ln]
    assert any("a.py" in ln for ln in lines)
    assert any("b.py" in ln for ln in lines)
    assert not any("c.txt" in ln for ln in lines)


def test_glob_recursive(base_dir: Path) -> None:
    (base_dir / "deep").mkdir()
    (base_dir / "deep" / "x.py").write_text("x")
    out = glob_handler({"pattern": "**/*.py"})
    assert "deep/x.py" in out


def test_glob_rejects_path_outside_base(
    base_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("else")
    with pytest.raises(PermissionError):
        glob_handler({"pattern": "*", "path": str(outside)})


# ─── Registration ────────────────────────────────────────────────


def test_bind_to_dispatcher_registers_all_six() -> None:
    disp = ToolDispatcher()
    bind_to_dispatcher(disp)
    assert set(disp.registered_tools()) == {
        "Read",
        "Write",
        "Edit",
        "Bash",
        "Grep",
        "Glob",
    }


def test_make_runner_dispatcher_returns_fresh_instance() -> None:
    a = make_runner_dispatcher()
    b = make_runner_dispatcher()
    assert a is not b
    assert set(a.registered_tools()) == set(b.registered_tools())


def test_dispatcher_executes_read_handler_end_to_end(base_dir: Path) -> None:
    disp = make_runner_dispatcher()
    f = base_dir / "x.txt"
    f.write_text("alpha\nbeta\n")
    result = asyncio.run(
        disp.execute("tu_1", "Read", {"file_path": str(f)})
    )
    assert result.is_error is False
    assert "1\talpha" in result.content


def test_dispatcher_captures_handler_error(base_dir: Path) -> None:
    """Handler exceptions surface as is_error tool_results, not raises."""
    disp = make_runner_dispatcher()
    result = asyncio.run(
        disp.execute(
            "tu_2", "Read", {"file_path": str(base_dir / "ghost.txt")}
        )
    )
    assert result.is_error is True
    assert "FileNotFoundError" in result.content
