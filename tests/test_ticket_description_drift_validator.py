"""OP-1073 tests for scripts/ticket-description-drift-validator.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ticket-description-drift-validator.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "sprint-sp-b-x-drift-validator-fixtures"

def _load_script():
    spec = importlib.util.spec_from_file_location("ticket_description_drift_validator", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def _fixture(name: str) -> str:
    return yaml.safe_load((FIXTURE_DIR / "specs.yaml").read_text(encoding="utf-8"))[name]

def _child(categories: int = 9, files_touched: int = 4) -> str:
    return (
        _fixture("parent")
        .replace("Parent says 9 categories must be preserved in child AC.", f"Child AC says {categories} categories.")
        .replace("files_touched_max: 4", f"files_touched_max: {files_touched}")
    )

def test_extract_boundaries_from_child_section_yaml() -> None:
    mod = _load_script()
    boundaries = mod.extract_boundaries(_fixture("parent"), child_id="SP-B-X-015")
    assert boundaries["loc_delta_max"] == 400
    assert boundaries["required_paths"] == [
        "scripts/ticket-description-drift-validator.py",
        "tests/test_ticket_description_drift_validator.py",
        "tests/fixtures/sprint-sp-b-x-drift-validator-fixtures/",
        "docs/sop/ticket-description-drift-validator.md",
    ]
    assert boundaries["external_systems"] == ["jira"]

def test_field_comparison_reports_boundary_drift() -> None:
    mod = _load_script()
    parent = mod.extract_boundaries(_fixture("parent"), child_id="SP-B-X-015")
    child = mod.extract_boundaries(_child(files_touched=5))
    drift = mod.compare_boundary_fields(parent, child)
    assert ("files_touched_max", 4, 5) in drift

def test_forbidden_combinations_integration_reports_rule_error() -> None:
    mod = _load_script()
    boundaries = mod.extract_boundaries(_child())
    boundaries["files_touched_max"] = 1
    messages = mod.forbidden_combination_messages(boundaries)
    assert messages == ["rule 10 required_paths_fit_files_touched_max: required_paths count cannot exceed files_touched_max"]

def test_count_mismatch_detection_reproduces_8_vs_9() -> None:
    mod = _load_script()
    parent = mod.parent_section_for_child(_fixture("parent"), "SP-B-X-015")
    drift = mod.compare_count_claims(parent, _child(categories=8))
    assert drift == [("category", 9, 8)]

def test_integration_conformant_child_passes_and_drifted_child_fails(tmp_path) -> None:
    mod = _load_script()
    parent = _fixture("parent")
    issues = {
        "OP-1": mod.JiraIssue("OP-1", "SP-B-X-015 conformant", _child()),
        "OP-2": mod.JiraIssue("OP-2", "SP-B-X-015 drifted", _child(categories=8, files_touched=5)),
    }
    clean = mod.validate_tickets(parent, ["OP-1"], issues.__getitem__)
    dirty = mod.validate_tickets(parent, ["OP-1", "OP-2"], issues.__getitem__)
    report_path = mod.write_report(mod.render_report(dirty), tmp_path)

    assert clean[0].findings == []
    assert any(finding.code == "boundary-field-drift" for finding in dirty[1].findings)
    assert any(finding.code == "count-claim-drift" for finding in dirty[1].findings)
    assert report_path.name.startswith("sprint-sp-b-x-drift-validator-")
    assert "OP-2" in report_path.read_text(encoding="utf-8")
