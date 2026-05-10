"""OP-810 — ToolError redirect hints for dedicated tool retries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from backend.agents import runner_handlers
from backend.agents.runner_handlers import make_runner_dispatcher


@pytest.fixture
def base_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(runner_handlers, "BASE_DIR", tmp_path.resolve())
    return tmp_path.resolve()


def _run_tool(tool_name: str, tool_input: dict[str, Any]):
    dispatcher = make_runner_dispatcher()
    return asyncio.run(dispatcher.execute("tu_op_810", tool_name, tool_input))


def _error_payload(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    result = _run_tool(tool_name, tool_input)
    assert result.is_error is True
    payload = json.loads(result.content)
    assert {"suggested_tool", "suggested_args"} <= set(payload)
    return payload


def test_bash_cat_redirects_to_read(base_dir: Path) -> None:
    target = base_dir / "sample.txt"
    target.write_text("alpha\n", encoding="utf-8")

    payload = _error_payload("Bash", {"command": "cat sample.txt"})

    assert payload["suggested_tool"] == "Read"
    assert payload["suggested_args"] == {"file_path": "sample.txt"}


def test_bash_grep_redirects_to_grep(base_dir: Path) -> None:
    (base_dir / "sample.py").write_text("alpha\n", encoding="utf-8")

    payload = _error_payload("Bash", {"command": "grep -rn alpha ."})

    assert payload["suggested_tool"] == "Grep"
    assert payload["suggested_args"] == {
        "output_mode": "files_with_matches",
        "-n": True,
        "pattern": "alpha",
        "path": ".",
    }


def test_bash_find_redirects_to_glob(base_dir: Path) -> None:
    (base_dir / "sample.py").write_text("alpha\n", encoding="utf-8")

    payload = _error_payload("Bash", {"command": "find . -name '*.py'"})

    assert payload["suggested_tool"] == "Glob"
    assert payload["suggested_args"] == {"path": ".", "pattern": "*.py"}


def test_read_wildcard_redirects_to_glob(base_dir: Path) -> None:
    (base_dir / "sample.py").write_text("alpha\n", encoding="utf-8")

    payload = _error_payload("Read", {"file_path": "*.py"})

    assert payload["suggested_tool"] == "Glob"
    assert payload["suggested_args"] == {"pattern": "*.py"}


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "expected_content"),
    [
        ("Bash", {"command": "cat sample.txt"}, "1\talpha"),
        ("Bash", {"command": "grep -r alpha ."}, "sample.txt"),
        ("Bash", {"command": "find . -name '*.py'"}, "sample.py"),
        ("Read", {"file_path": "*.py"}, "sample.py"),
    ],
)
def test_model_retry_uses_suggested_tool_on_first_retry(
    base_dir: Path,
    tool_name: str,
    tool_input: dict[str, Any],
    expected_content: str,
) -> None:
    (base_dir / "sample.txt").write_text("alpha\n", encoding="utf-8")
    (base_dir / "sample.py").write_text("alpha\n", encoding="utf-8")
    first_payload = _error_payload(tool_name, tool_input)

    retry = _run_tool(
        first_payload["suggested_tool"], first_payload["suggested_args"]
    )

    assert retry.is_error is False
    assert expected_content in retry.content
