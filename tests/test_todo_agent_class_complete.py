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


ACTIVE_PREFIXES: tuple[str, ...] = ("MP.", "RPG.", "FX2.")
"""Priority prefixes whose open rows must carry [class:X] labels.

Adding `[class:X]` to every one of TODO.md's 1100+ open rows would mostly
mean spamming `[class:unassigned]` on speculative future-priority items
that have no real routing signal yet. This allowlist scopes the drift
guard to actively-staffed priorities — the ones a runner is about to pick
up — and lets the rest stay unlabeled until they enter active execution.

To activate enforcement on a new priority, add its task-id prefix here
(e.g. `BP.` when BP staffing kicks off) and re-run the helper to fill in
the `[class:X]` tags for that priority's open rows.
"""


def _is_active(task_id: str | None) -> bool:
    """Return True iff ``task_id`` falls under an ACTIVE_PREFIXES priority."""
    if not task_id:
        return False
    return any(task_id.startswith(prefix) for prefix in ACTIVE_PREFIXES)


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
    active_open_items = [item for item in open_items if _is_active(item.task_id)]
    findings = reslicer.find_label_gaps(active_open_items, schema, rules, baselines)
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
    """Every active-prefix open TODO row must carry a valid ``[class:X]`` label.

    Scope is ``ACTIVE_PREFIXES`` (currently ``MP. / RPG. / FX2.``). Items
    outside that set are out of scope until their priority is added to the
    allowlist. The pre-slice Tier B skip remains for the case where no
    active item carries any valid label yet.
    """
    schema, items, findings = _live_open_findings()
    allowed = set(schema.allowed_values)
    active_items = [item for item in items if _is_active(item.task_id)]
    valid_labeled_active = [item for item in active_items if item.class_label in allowed]
    if not valid_labeled_active:
        pytest.skip(
            "No active-prefix TODO rows carry a valid [class:X] label yet; "
            "runner-managed pre-slice snapshot."
        )

    assert not findings, (
        "Active-prefix open TODO rows missing or using invalid [class:X] labels:\n"
        + "\n".join(
            f"TODO.md:{finding.line}: {finding.kind} "
            f"{finding.task_id or '<no-task-id>'} -> "
            f"[class:{finding.suggested_class}]"
            for finding in findings[:40]
        )
    )
