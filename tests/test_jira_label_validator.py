"""OP-1042 tests for filing-time JIRA label validation."""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))

from jira_label_validator import validate  # noqa: E402


def _rules(labels: list[str], description: str = "") -> set[str]:
    return {issue.rule for issue in validate(labels, description)}


def _load_file_jira_ticket():
    path = SCRIPTS / "file_jira_ticket.py"
    spec = importlib.util.spec_from_file_location("file_jira_ticket", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_script(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _args(**overrides):
    values = {
        "summary": "OP-1042 synthetic",
        "description_file": "",
        "priority": "High",
        "tier": "M",
        "cls": "subscription-codex",
        "type": "feature",
        "areas": ["tests", "tooling"],
        "scope": None,
        "check": False,
        "force": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_unknown_label_rejected() -> None:
    rules = _rules(["class:subscription-codex", "tier:M", "area:tests", "bogus"])

    assert "unknown-label" in rules


def test_retired_priority_track_rejected() -> None:
    rules = _rules(["class:subscription-codex", "tier:M", "area:tests", "priority:fx-track"])

    assert "unknown-label" in rules


def test_single_valued_namespace_repeated_rejected() -> None:
    rules = _rules(["class:subscription-codex", "class:api-openai", "tier:M", "area:tests"])

    assert "single-valued-namespace-repeated" in rules


def test_type_meta_with_implementation_path_rejected() -> None:
    rules = _rules(
        ["class:subscription-codex", "tier:M", "area:tests", "type:meta"],
        "## Acceptance criteria\n- [ ] Change scripts/file_jira_ticket.py",
    )

    assert "meta-type-with-implementation" in rules


def test_class_without_area_or_tier_warns() -> None:
    issues = validate(["class:subscription-codex"], "roll-up only")

    assert [(issue.severity, issue.rule) for issue in issues] == [
        ("warning", "class-requires-routing")
    ]


def test_file_jira_ticket_refuses_malformed_post_before_network(monkeypatch) -> None:
    mod = _load_file_jira_ticket()

    def fail_jira_config(*_args, **_kwargs):
        raise AssertionError("malformed ticket must abort before JIRA config/network")

    monkeypatch.setattr(mod, "_jira_config", fail_jira_config)

    with pytest.raises(SystemExit) as exc:
        mod.file_ticket(
            _args(type="meta", areas=["tests"]),
            "## Acceptance criteria\n- [ ] Change scripts/file_jira_ticket.py\n",
        )

    assert "label validation errors" in str(exc.value)


def test_file_audit_29_tickets_refuses_malformed_issue_before_post(monkeypatch) -> None:
    mod = _load_script("file_audit_29_tickets")
    spec = mod.TicketSpec(
        alias="bad",
        summary="Synthetic malformed roll-up",
        scope_text="Change scripts/file_jira_ticket.py",
        code_ac=["Change scripts/file_jira_ticket.py"],
        deploy_ac=[],
        integration_ac=[],
        exercised_ac=[],
        go_live="test",
        labels=["class:subscription-codex", "tier:M", "area:tests", "type:meta"],
    )

    def fail_request(*_args, **_kwargs):
        raise AssertionError("malformed issue must abort before POST")

    monkeypatch.setattr(mod, "_request", fail_request)

    with pytest.raises(SystemExit) as exc:
        mod.create_issue("https://jira.example.test", "OP", "Basic test-token", spec)

    assert "label validation errors" in str(exc.value)


def test_file_audit_29_decompose_labels_are_valid_for_dry_run() -> None:
    mod = _load_script("file_audit_29_decompose")

    mod._validate_all_children_or_exit(force=False)
