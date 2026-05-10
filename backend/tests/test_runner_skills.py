"""OP-812 runner skill primitive contract tests."""

from __future__ import annotations

import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

from backend.agents.skills_loader import load_default_scopes, make_skill_handler
from backend.agents.tool_dispatcher import ToolDispatcher


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _prepend_path(monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    monkeypatch.setenv(
        "PATH",
        f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
    )


def _dispatcher() -> ToolDispatcher:
    registry = load_default_scopes(
        PROJECT_ROOT,
        home=Path("/__no_home_skills_for_runner_skill_tests__"),
    )
    dispatcher = ToolDispatcher()
    dispatcher.register("Skill", make_skill_handler(registry))
    return dispatcher


def test_runner_skill_files_are_registered() -> None:
    registry = load_default_scopes(
        PROJECT_ROOT,
        home=Path("/__no_home_skills_for_runner_skill_tests__"),
    )

    assert registry.has("run_tests")
    assert registry.has("lint_changed")
    assert registry.has("fmt")
    assert registry.get("run_tests").source_path == (
        PROJECT_ROOT / "scripts" / "skills" / "run_tests.skill"
    )


@pytest.mark.asyncio
async def test_run_tests_skill_invokable_via_skill_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pytest = tmp_path / "pytest"
    _write_executable(
        fake_pytest,
        """
        #!/usr/bin/env python3
        print("= 2 passed, 1 failed, 1 error in 0.12s =")
        """,
    )
    monkeypatch.setenv("OMNISIGHT_RUN_TESTS_PYTEST", str(fake_pytest))

    result = await _dispatcher().execute(
        "tu-run-tests",
        "Skill",
        {"skill": "run_tests", "args": {"test_path": "backend/tests/test_x.py"}},
    )

    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload["passed"] == 2
    assert payload["failed"] == 1
    assert payload["errors"] == ["1 error"]
    assert isinstance(payload["duration_s"], float)


@pytest.mark.asyncio
async def test_lint_changed_skill_invokable_via_skill_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "git",
        """
        #!/usr/bin/env python3
        print("backend/a.py")
        print("README.md")
        print("backend/c.py")
        """,
    )
    for tool in ("ruff", "black", "mypy"):
        _write_executable(
            bin_dir / tool,
            f"""
            #!/usr/bin/env python3
            import sys
            print("{tool}: " + " ".join(sys.argv[1:]))
            raise SystemExit(1)
            """,
        )
    _prepend_path(monkeypatch, bin_dir)

    result = await _dispatcher().execute(
        "tu-lint-changed",
        "Skill",
        {"skill": "lint_changed"},
    )

    assert result.is_error is False
    payload = json.loads(result.content)
    assert set(payload) == {"ruff", "black", "mypy"}
    assert payload["ruff"] == ["ruff: check backend/a.py backend/c.py"]
    assert payload["black"] == ["black: --check backend/a.py backend/c.py"]
    assert payload["mypy"] == ["mypy: backend/a.py backend/c.py"]


@pytest.mark.asyncio
async def test_fmt_skill_invokable_via_skill_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "ruff",
        """
        #!/usr/bin/env python3
        from pathlib import Path
        import sys
        for arg in sys.argv[2:]:
            Path(arg).write_text(Path(arg).read_text() + "# formatted\\n")
        """,
    )
    _prepend_path(monkeypatch, bin_dir)
    target = tmp_path / "sample.py"
    target.write_text("x=1\n", encoding="utf-8")

    result = await _dispatcher().execute(
        "tu-fmt",
        "Skill",
        {"skill": "fmt", "args": {"path_glob": str(tmp_path / "*.py")}},
    )

    assert result.is_error is False
    assert json.loads(result.content) == {"files_modified": 1}
    assert target.read_text(encoding="utf-8").endswith("# formatted\n")
