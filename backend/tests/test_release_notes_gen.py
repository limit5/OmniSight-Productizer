"""OP-888 contract tests for scripts/release_notes_from_milestone.py."""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "release_notes_from_milestone.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("release_notes_from_milestone", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeJira:
    def __init__(self, tickets, *, exists: bool = True, suggestions=None) -> None:
        self.tickets = tickets
        self.exists = exists
        self.suggestions = suggestions or ["v0.99.0", "v0.99-rc"]
        self.requested_versions: list[str] = []

    def fix_version_exists(self, version: str) -> bool:
        self.requested_versions.append(version)
        return self.exists

    def suggest_versions(self, version: str) -> list[str]:
        return self.suggestions

    def tickets_for_fix_version(self, version: str):
        self.requested_versions.append(version)
        return self.tickets


def _ticket(mod, key: str, *, labels, priority: str = "Medium", description: str | None = None):
    return mod.MilestoneTicket(
        key=key,
        summary=f"{key} summary",
        priority=priority,
        labels=tuple(labels),
        description=description
        or (
            "## Acceptance criteria\n"
            "- First AC\n"
            "- Second AC\n"
            "\n"
            "## Reference\n"
            "L-OP-745-example\n"
        ),
    )


def test_milestone_clean_happy_generates_ops_notes(tmp_path) -> None:
    mod = _load_script()
    lesson_dir = tmp_path / "docs" / "sop" / "lessons"
    lesson_dir.mkdir(parents=True)
    (lesson_dir / "L-OP-745-example.md").write_text("# Lesson\n", encoding="utf-8")

    ticket = _ticket(mod, "OP-100", labels=["sprint:A"], priority="High")
    result = mod.generate_release_notes(
        version="v0.99-rc",
        repo=tmp_path,
        jira=FakeJira([ticket]),
        today=date(2026, 5, 11),
    )

    assert result.output_path == tmp_path / "release-notes" / "v0.99-rc.md"
    assert result.output_path.exists()
    assert "## Sprint A / High" in result.rendered
    assert "OP-100 - OP-100 summary" in result.rendered
    assert "docs/sop/lessons/L-OP-745-example.md" in result.rendered
    assert "Generated: 2026-05-11" in result.rendered


def test_ticket_malformed_skip_is_flagged_in_output(tmp_path) -> None:
    mod = _load_script()
    bad = _ticket(mod, "OP-101", labels=["sprint:B"], description="## Notes\nNo AC here\n")
    result = mod.generate_release_notes(
        version="v0.99-rc",
        repo=tmp_path,
        jira=FakeJira([bad]),
        today=date(2026, 5, 11),
    )

    assert result.included == ()
    assert len(result.skipped) == 1
    assert "TicketDescriptionMalformed" in result.rendered
    assert "OP-101" in result.rendered


def test_lesson_broken_uses_placeholder(tmp_path) -> None:
    mod = _load_script()
    ticket = _ticket(mod, "OP-102", labels=["sprint:C"])
    result = mod.generate_release_notes(
        version="v0.99-rc",
        repo=tmp_path,
        jira=FakeJira([ticket]),
        today=date(2026, 5, 11),
    )

    assert mod.LESSON_PLACEHOLDER in result.rendered
    assert "Broken lesson references:" in result.rendered
    assert "L-OP-745-example.md" in result.rendered


def test_no_fix_version_refuses_with_suggestions(tmp_path) -> None:
    mod = _load_script()
    with pytest.raises(mod.JIRAFixVersionNotFound) as exc:
        mod.generate_release_notes(
            version="v9.9.9",
            repo=tmp_path,
            jira=FakeJira([], exists=False, suggestions=["v0.99-rc"]),
            today=date(2026, 5, 11),
        )

    assert "v9.9.9 was not found" in str(exc.value)
    assert "v0.99-rc" in str(exc.value)


def test_multi_sprint_grouping_sorts_by_sprint_then_priority(tmp_path) -> None:
    mod = _load_script()
    tickets = [
        _ticket(mod, "OP-201", labels=["sprint:D"], priority="Low"),
        _ticket(mod, "OP-202", labels=["sprint:A"], priority="Medium"),
        _ticket(mod, "OP-203", labels=["sprint:A"], priority="Highest"),
        _ticket(mod, "OP-204", labels=["sprint:E"], priority="High"),
    ]
    result = mod.generate_release_notes(
        version="v0.99-rc",
        repo=tmp_path,
        jira=FakeJira(tickets),
        today=date(2026, 5, 11),
    )

    assert result.rendered.index("## Sprint A / Highest") < result.rendered.index("## Sprint A / Medium")
    assert result.rendered.index("## Sprint A / Medium") < result.rendered.index("## Sprint D / Low")
    assert result.rendered.index("## Sprint D / Low") < result.rendered.index("## Sprint E / High")
