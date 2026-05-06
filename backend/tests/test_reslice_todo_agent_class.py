"""MP.W0.2 - tests for ``scripts/reslice_todo_agent_class.py``.

The helper is read-only TODO hygiene tooling. Tests use temporary TODO,
schema, and ADR files so they never mutate the real ``TODO.md``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "reslice_todo_agent_class.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import reslice_todo_agent_class as reslicer  # noqa: E402


SCHEMA_TEXT = """\
schema_version: 1
label_key: agent_class
inline_label_prefix: class
unknown_value: unassigned
allowed_values:
  - subscription-codex
  - api-anthropic
  - unassigned
"""

ASSIGNMENT_TEXT = """\
## Capability assignment (v0.5.0 ship)

| Wave | agent_class | Why |
|---|---|---|
| W1-W7 backend core | api-anthropic | high-blast |
| W8-W10 frontend core | subscription-codex | bounded React |
| W11 tests / docs | subscription-codex | mature pattern |
"""


def _write_inputs(tmp_path: Path, todo_text: str) -> tuple[Path, Path, Path]:
    todo = tmp_path / "TODO.md"
    schema = tmp_path / "agent_class_schema.yaml"
    assignment = tmp_path / "adr.md"
    todo.write_text(todo_text, encoding="utf-8")
    schema.write_text(SCHEMA_TEXT, encoding="utf-8")
    assignment.write_text(ASSIGNMENT_TEXT, encoding="utf-8")
    return todo, schema, assignment


class TestSchemaAndAssignment:
    def test_loads_schema_allowed_values(self, tmp_path: Path) -> None:
        schema_path = tmp_path / "agent_class_schema.yaml"
        schema_path.write_text(SCHEMA_TEXT, encoding="utf-8")

        schema = reslicer.load_schema(schema_path)

        assert schema.inline_label_prefix == "class"
        assert schema.unknown_value == "unassigned"
        assert schema.allowed_values == ("subscription-codex", "api-anthropic", "unassigned")

    def test_schema_rejects_unknown_value_not_in_allowed_values(self, tmp_path: Path) -> None:
        schema_path = tmp_path / "bad.yaml"
        schema_path.write_text(
            SCHEMA_TEXT.replace("unknown_value: unassigned", "unknown_value: api-openai"),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="unknown_value"):
            reslicer.load_schema(schema_path)

    def test_parses_assignment_table_wave_ranges(self) -> None:
        schema = reslicer.AgentClassSchema(
            inline_label_prefix="class",
            unknown_value="unassigned",
            allowed_values=("subscription-codex", "api-anthropic", "unassigned"),
        )

        rules = reslicer.parse_assignment_rules(ASSIGNMENT_TEXT, schema)

        assert [(r.wave, r.agent_class, r.wave_ranges) for r in rules] == [
            ("W1-W7 backend core", "api-anthropic", ((1, 7),)),
            ("W8-W10 frontend core", "subscription-codex", ((8, 10),)),
            ("W11 tests / docs", "subscription-codex", ((11, 11),)),
        ]


class TestTodoScan:
    def test_iter_todo_items_covers_open_done_and_agent_markers(self) -> None:
        text = "\n".join([
            "- [ ] W8.1 Build UI",
            "- [x][G] W1.2 Backend shipped [class:api-anthropic]",
            "- [!][G] W9.1 Failure marker ignored",
            "  - [x] nested completed item [class:subscription-codex]",
        ])

        items = list(reslicer.iter_todo_items(text))

        assert [(i.line, i.state, i.agent_marker, i.task_id, i.class_label) for i in items] == [
            (1, " ", None, "W8.1", None),
            (2, "x", "[G]", "W1.2", "api-anthropic"),
            (4, "x", None, None, "subscription-codex"),
        ]

    def test_findings_include_missing_and_invalid_labels(self, tmp_path: Path) -> None:
        todo, schema_path, assignment = _write_inputs(
            tmp_path,
            "\n".join([
                "- [ ] W8.1 Build UI",
                "- [x] W1.2 Backend [class:api-anthropic]",
                "- [ ] MP.W0.2 Helper [class:typo]",
            ]),
        )

        items, findings, _schema, _rules = reslicer.scan_todo(
            todo_path=todo,
            schema_path=schema_path,
            assignment_path=assignment,
        )

        assert len(items) == 3
        assert [(f.line, f.kind, f.task_id, f.suggested_class, f.suggestion_source) for f in findings] == [
            (1, "missing", "W8.1", "subscription-codex", "ADR0008:W8-W10 frontend core"),
            (3, "invalid", "MP.W0.2", "unassigned", "fallback:unassigned"),
        ]

    def test_non_w_task_without_label_falls_back_to_unassigned(self, tmp_path: Path) -> None:
        todo, schema_path, assignment = _write_inputs(tmp_path, "- [ ] MP.W0.2 Helper\n")

        _items, findings, _schema, _rules = reslicer.scan_todo(
            todo_path=todo,
            schema_path=schema_path,
            assignment_path=assignment,
        )

        assert findings[0].suggested_class == "unassigned"
        assert findings[0].suggestion_source == "fallback:unassigned"


class TestRenderingAndCli:
    def test_text_report_prompts_operator_with_line_and_suggestion(self, tmp_path: Path) -> None:
        todo, schema_path, assignment = _write_inputs(tmp_path, "- [ ] W8.1 Build UI\n")
        items, findings, _schema, rules = reslicer.scan_todo(
            todo_path=todo,
            schema_path=schema_path,
            assignment_path=assignment,
        )

        report = reslicer.render_text(findings, total_items=len(items), rules_count=len(rules))

        assert "Operator actions needed: 1" in report
        assert "TODO.md:1: missing W8.1 -> [class:subscription-codex]" in report

    def test_cli_check_returns_one_when_findings_exist(self, tmp_path: Path) -> None:
        todo, schema_path, assignment = _write_inputs(tmp_path, "- [ ] W8.1 Build UI\n")

        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--todo",
                str(todo),
                "--schema",
                str(schema_path),
                "--assignment",
                str(assignment),
                "--check",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 1
        assert "Operator actions needed: 1" in proc.stdout

    def test_cli_json_report_is_machine_readable(self, tmp_path: Path) -> None:
        todo, schema_path, assignment = _write_inputs(tmp_path, "- [ ] W11.1 Guard\n")

        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--todo",
                str(todo),
                "--schema",
                str(schema_path),
                "--assignment",
                str(assignment),
                "--format",
                "json",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["finding_count"] == 1
        assert payload["findings"][0]["suggested_class"] == "subscription-codex"
        assert payload["findings"][0]["suggestion_source"] == "ADR0008:W11 tests / docs"
