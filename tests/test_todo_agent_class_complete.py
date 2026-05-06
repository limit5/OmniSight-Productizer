"""MP.W0.4 — drift guard for TODO ``[class:X]`` coverage.

MP.W0.1 defines the canonical ``agent_class`` schema and MP.W0.2 ships
the read-only TODO scanner. This test turns the scanner into a CI guard:
once TODO.md has been re-sliced, every open checkbox row (``- [ ]``)
must carry a valid inline ``[class:<agent_class>]`` label.

Tier B Codex worktrees must not edit TODO.md, so the live guard skips a
fully pre-slice snapshot that contains no valid class labels yet. That
keeps this implementation commit testable without taking ownership of
runner-managed TODO markers; after the operator applies the labels, the
same test becomes strict and fails on any unlabeled open row.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
import reslice_todo_agent_class as reslicer  # noqa: E402


def _live_open_findings() -> tuple[
    reslicer.AgentClassSchema,
    list[reslicer.TodoItem],
    list[reslicer.Finding],
]:
    schema = reslicer.load_schema()
    rules = reslicer.load_assignment_rules(schema)
    baselines = reslicer.load_completed_epic_baselines(schema)
    todo_text = reslicer.TODO_PATH.read_text(encoding="utf-8")
    items = list(reslicer.iter_todo_items(todo_text))
    open_items = [item for item in items if item.state == " "]
    findings = reslicer.find_label_gaps(open_items, schema, rules, baselines)
    return (schema, items, findings)


def test_open_todo_rows_missing_class_labels_are_reported() -> None:
    """Contract guard: an unlabeled ``- [ ]`` row is a finding."""
    schema = reslicer.AgentClassSchema(
        inline_label_prefix="class",
        unknown_value="unassigned",
        allowed_values=("subscription-codex", "unassigned"),
    )
    items = list(reslicer.iter_todo_items("- [ ] MP.W0.4 Drift guard\n"))

    findings = reslicer.find_label_gaps(items, schema, rules=())

    assert [(f.line, f.kind, f.suggested_class) for f in findings] == [
        (1, "missing", "unassigned")
    ]


def test_live_open_todo_rows_have_agent_class_labels() -> None:
    """Every open TODO row must carry a valid ``[class:X]`` label."""
    schema, items, findings = _live_open_findings()
    allowed = set(schema.allowed_values)
    valid_labeled_items = [item for item in items if item.class_label in allowed]
    if not valid_labeled_items:
        pytest.skip(
            "TODO.md is still a pre-MP.W0 re-slice snapshot in this Tier B "
            "worktree; runner owns TODO.md marker/label writes."
        )

    assert not findings, (
        "Open TODO rows missing or using invalid [class:X] labels:\n"
        + "\n".join(
            f"TODO.md:{finding.line}: {finding.kind} "
            f"{finding.task_id or '<no-task-id>'} -> "
            f"[class:{finding.suggested_class}]"
            for finding in findings[:40]
        )
    )
