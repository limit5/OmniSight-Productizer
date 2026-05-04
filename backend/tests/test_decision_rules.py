"""Phase 50B — Decision Rules unit tests."""

from __future__ import annotations

import pytest

from backend import decision_engine as de
from backend import decision_rules as dr


@pytest.fixture(autouse=True)
def _reset_state():
    dr.clear()
    de._current_mode = de.OperationMode.supervised  # type: ignore[attr-defined]
    yield
    dr.clear()


def test_list_rules_is_empty_by_default():
    assert dr.list_rules() == []


def test_replace_rules_normalises_and_generates_ids():
    out = dr.replace_rules([
        {"kind_pattern": "stuck/*", "severity": "risky", "auto_in_modes": ["turbo"]},
        {"kind_pattern": "budget/*", "priority": 5, "enabled": False},
    ])
    assert len(out) == 2
    # priority-ordered: budget/* (5) comes first
    assert out[0]["kind_pattern"] == "budget/*"
    for r in out:
        assert r["id"].startswith("rule-")
        assert isinstance(r["enabled"], bool)


def test_replace_rules_rejects_bad_severity():
    with pytest.raises(dr.DecisionRuleValidationError, match="severity"):
        dr.replace_rules([{"kind_pattern": "x", "severity": "ultra"}])


def test_replace_rules_rejects_unknown_mode():
    with pytest.raises(dr.DecisionRuleValidationError, match="mode"):
        dr.replace_rules([{"kind_pattern": "x", "auto_in_modes": ["bogus"]}])


def test_replace_rules_rejects_duplicate_id():
    with pytest.raises(dr.DecisionRuleValidationError, match="duplicate"):
        dr.replace_rules([
            {"id": "r1", "kind_pattern": "a"},
            {"id": "r1", "kind_pattern": "b"},
        ])


def test_decision_rule_validation_error_preserves_value_error_contract():
    assert issubclass(dr.DecisionRuleValidationError, ValueError)


def test_match_first_priority_hit_wins():
    dr.replace_rules([
        {"id": "specific", "kind_pattern": "stuck/timeout", "severity": "risky", "priority": 1},
        {"id": "catchall", "kind_pattern": "stuck/*", "severity": "routine", "priority": 50},
    ])
    m = dr.match("stuck/timeout", de.OperationMode.supervised)
    assert m is not None and m["id"] == "specific"


def test_match_skips_disabled_rules():
    dr.replace_rules([
        {"id": "r", "kind_pattern": "x", "enabled": False},
    ])
    assert dr.match("x", de.OperationMode.supervised) is None


def test_propose_uses_rule_severity_and_default():
    dr.replace_rules([
        {"kind_pattern": "ambiguity/*", "severity": "info",
         "default_option_id": "proceed"},
    ])
    dec = de.propose(
        kind="ambiguity/spec",
        title="spec unclear",
        options=[{"id": "ask", "label": "Ask"}, {"id": "proceed", "label": "Proceed"}],
        severity=de.DecisionSeverity.risky,  # rule should override to info
    )
    # info in supervised mode → auto_executed
    assert dec.status == de.DecisionStatus.auto_executed
    assert dec.chosen_option_id == "proceed"
    assert dec.source.get("rule_id")


def test_propose_rule_forces_auto_in_listed_mode():
    dr.replace_rules([
        {"kind_pattern": "dangerous/*", "severity": "destructive",
         "auto_in_modes": ["supervised"],  # force auto even though destructive
         "default_option_id": "abort"},
    ])
    dec = de.propose(
        kind="dangerous/delete",
        title="nuke artifact",
        options=[{"id": "abort", "label": "Abort"}, {"id": "go", "label": "Go"}],
    )
    # In supervised mode, destructive wouldn't auto-execute normally —
    # the rule overrides and forces auto to "abort".
    assert dec.status == de.DecisionStatus.auto_executed
    assert dec.chosen_option_id == "abort"


def test_test_against_reports_hits():
    dr.replace_rules([
        {"id": "stk", "kind_pattern": "stuck/*", "severity": "risky", "auto_in_modes": ["turbo"]},
    ])
    res = dr.test_against(["stuck/loop", "other"], de.OperationMode.turbo)
    assert res == [
        {"kind": "stuck/loop", "rule_id": "stk", "severity": "risky", "auto": True},
        {"kind": "other", "rule_id": None, "severity": None, "auto": False},
    ]
