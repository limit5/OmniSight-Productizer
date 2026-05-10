"""OP-834 TDD applicability and phase guard tests."""
from __future__ import annotations

import pytest

from backend.agents.tdd_applicability import resolve_from_issue
from backend.agents.tdd_orchestrator import (
    ERROR_ORDERING,
    ERROR_PREMATURE_GREEN,
    ERROR_UNRUNNABLE,
    ERROR_UNWRITEABLE,
    TDDOrchestrator,
)
from backend.agents.tool_dispatcher import StructuredToolError
from scripts.run_s1_via_anthropic_sdk import _build_tdd_orchestrator


def _issue(labels: list[str] | None = None, **fields):
    data = {"labels": labels or []}
    data.update(fields)
    return {"fields": data}


def _red_output() -> str:
    return "FAILED backend/tests/test_x.py::test_bug - AssertionError\nEXIT_CODE: 1"


def _green_output() -> str:
    return "1 passed in 0.01s\nEXIT_CODE: 0"


def _syntax_output() -> str:
    return "ERROR collecting backend/tests/test_x.py\nSyntaxError: invalid syntax\nEXIT_CODE: 2"


def _write_test(orch: TDDOrchestrator) -> None:
    payload = {"command": "create", "path": "backend/tests/test_x.py"}
    orch.before_text_edit(payload)
    orch.after_text_edit(payload)


def _edit_source(orch: TDDOrchestrator) -> None:
    payload = {"command": "str_replace", "path": "backend/agents/x.py"}
    orch.before_text_edit(payload)
    orch.after_text_edit(payload)


def test_applicability_reads_configured_custom_field_before_label() -> None:
    resolved = resolve_from_issue(
        _issue(["tdd:no"], customfield_12345={"value": "Yes"}),
        field_key="customfield_12345",
    )
    assert resolved.value == "yes"
    assert resolved.source == "field:customfield_12345"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("tdd:no", "no"),
        ("tdd:yes", "yes"),
        ("tdd:conditional", "conditional"),
    ],
)
def test_applicability_label_fallback(label: str, expected: str) -> None:
    assert resolve_from_issue(_issue([label])).value == expected


def test_applicability_defaults_to_conditional() -> None:
    resolved = resolve_from_issue(_issue(["component:backend"]))
    assert resolved.value == "conditional"
    assert resolved.source == "default"


def test_applicable_no_bypasses_source_edit_gate() -> None:
    orch = TDDOrchestrator(ticket_key="OP-834", applicable="no")
    _edit_source(orch)
    assert orch.state.bypassed is True
    assert orch.state.source_edited is False


def test_applicable_yes_blocks_source_edit_until_red_test() -> None:
    orch = TDDOrchestrator(ticket_key="OP-834", applicable="yes")
    with pytest.raises(StructuredToolError) as exc:
        _edit_source(orch)
    assert exc.value.error_code == ERROR_ORDERING

    _write_test(orch)
    orch.after_bash({"command": "pytest backend/tests/test_x.py"}, _red_output())
    _edit_source(orch)
    assert orch.state.red_confirmed is True
    assert orch.state.source_edited is True


def test_red_then_green_completes_tdd_flow() -> None:
    orch = TDDOrchestrator(ticket_key="OP-834", applicable="yes")
    _write_test(orch)
    orch.after_bash({"command": "pytest backend/tests/test_x.py"}, _red_output())
    _edit_source(orch)
    orch.after_bash({"command": "pytest backend/tests/test_x.py"}, _green_output())
    assert orch.state.events == ["write_test", "test_runs_red", "write_code", "run_test_green"]
    assert orch.state.green_confirmed is True


def test_red_then_still_red_records_failed_green_phase() -> None:
    orch = TDDOrchestrator(ticket_key="OP-834", applicable="yes")
    _write_test(orch)
    orch.after_bash({"command": "pytest backend/tests/test_x.py"}, _red_output())
    _edit_source(orch)
    orch.after_bash({"command": "pytest backend/tests/test_x.py"}, _red_output())
    assert orch.state.events[-1] == "run_test_still_red"
    assert orch.state.green_confirmed is False


def test_premature_green_is_blocking_violation() -> None:
    orch = TDDOrchestrator(ticket_key="OP-834", applicable="yes")
    _write_test(orch)
    with pytest.raises(StructuredToolError) as exc:
        orch.after_bash({"command": "pytest backend/tests/test_x.py"}, _green_output())
    assert exc.value.error_code == ERROR_PREMATURE_GREEN


def test_unrunnable_test_is_blocking_violation() -> None:
    orch = TDDOrchestrator(ticket_key="OP-834", applicable="yes")
    _write_test(orch)
    with pytest.raises(StructuredToolError) as exc:
        orch.after_bash({"command": "pytest backend/tests/test_x.py"}, _syntax_output())
    assert exc.value.error_code == ERROR_UNRUNNABLE


def test_test_write_fails_three_attempts_marks_unwriteable() -> None:
    orch = TDDOrchestrator(ticket_key="OP-834", applicable="yes")
    for _ in range(3):
        orch.before_text_edit({"command": "create", "path": "backend/tests/test_x.py"})
    with pytest.raises(StructuredToolError) as exc:
        orch.before_text_edit({"command": "create", "path": "backend/tests/test_x.py"})
    assert exc.value.error_code == ERROR_UNWRITEABLE


def test_conditional_locator_bypasses_infra_scaffold_work() -> None:
    orch = _build_tdd_orchestrator(
        ticket_key="OP-834",
        ticket_description="infra/scaffold work, no testable behavior",
        applicability=resolve_from_issue(_issue(["tdd:conditional"])),
    )
    _edit_source(orch)
    assert orch.state.bypassed is True
    assert orch.state.bypass_reason == "locator:no-testable-behavior"


def test_conditional_locator_enforces_when_behavior_is_testable() -> None:
    orch = _build_tdd_orchestrator(
        ticket_key="OP-834",
        ticket_description="backend behavior bug needs regression coverage",
        applicability=resolve_from_issue(_issue(["tdd:conditional"])),
    )
    with pytest.raises(StructuredToolError) as exc:
        _edit_source(orch)
    assert exc.value.error_code == ERROR_ORDERING
