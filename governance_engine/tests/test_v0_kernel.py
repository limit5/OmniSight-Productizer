from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

import pytest
import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.schema.forbidden_combinations import validate_forbidden_combinations
from governance_engine.schema.v0 import L2Reason, TicketClass, TicketContract


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"

V2_BOUNDARY_FIELDS = [
    "loc_delta_max",
    "files_touched_max",
    "required_paths",
    "forbidden_paths",
    "non_goals",
    "test_scope",
    "destructive_op_classes",
    "destructive_op_scope",
    "execution_mode",
    "evidence_class",
    "on_scope_creep",
]


def _load_fixture_payload() -> dict[str, object]:
    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


def _validated_fixture() -> TicketContract:
    return TicketContract.model_validate(_load_fixture_payload())


def _model_construct_payload() -> dict[str, object]:
    return _validated_fixture().model_dump()


def test_31a_fixture_valid() -> None:
    contract = TicketContract.model_validate(_load_fixture_payload())

    assert validate_forbidden_combinations(contract) == []


@pytest.mark.parametrize(
    ("rule_id", "payload", "construct"),
    [
        pytest.param(
            1,
            {"destructive_op_classes": ["filesystem-delete"]},
            False,
            id="rule_1_destructive_ops_need_l1_l2",
        ),
        pytest.param(
            2,
            {"external_side_effect": "irreversible"},
            False,
            id="rule_2_irreversible_side_effect_matches_reversibility",
        ),
        pytest.param(
            3,
            {"ticket_class": TicketClass.OPERATOR_WINDOW_TOP, "l1_exclusive_reason": None},
            True,
            id="test_forbidden_rule_3_via_model_construct",
        ),
        pytest.param(
            4,
            {"ticket_class": TicketClass.OPERATOR_WINDOW_DEPUTY, "l2_reason": None},
            True,
            id="test_forbidden_rule_4_via_model_construct",
        ),
        pytest.param(
            5,
            {"class": "subscription-codex", "environment_scope": "production"},
            False,
            id="rule_5_subscription_cannot_touch_production",
        ),
        pytest.param(
            6,
            {"class": "subscription-codex", "runtime_capability": "operator-witnessed"},
            False,
            id="rule_6_subscription_cannot_be_operator_witnessed",
        ),
        pytest.param(
            7,
            {"evidence_class": "operator"},
            False,
            id="rule_7_operator_evidence_requires_rehearsal",
        ),
        pytest.param(
            8,
            {"class": "subscription-codex", "external_payload_class": "sensitive"},
            False,
            id="rule_8_subscription_cannot_have_sensitive_payload",
        ),
        pytest.param(
            9,
            {"tag_type": "meta"},
            False,
            id="rule_9_meta_requires_operator_rehearsal",
        ),
        pytest.param(
            10,
            {"required_paths": ["one", "two"], "files_touched_max": 1},
            False,
            id="rule_10_required_paths_fit_files_touched_max",
        ),
    ],
)
def test_forbidden_combinations(rule_id: int, payload: dict[str, object], construct: bool) -> None:
    if construct:
        bad_payload = _model_construct_payload()
    else:
        bad_payload = deepcopy(_load_fixture_payload())
    bad_payload.update(payload)

    if construct:
        contract = TicketContract.model_construct(**bad_payload)
    else:
        contract = TicketContract.model_validate(bad_payload)

    errors = validate_forbidden_combinations(contract)

    assert [error.rule_id for error in errors] == [rule_id]


@pytest.mark.parametrize("field", V2_BOUNDARY_FIELDS)
def test_schema_required_fields_missing(field: str) -> None:
    payload = _load_fixture_payload()
    payload.pop(field)

    with pytest.raises(ValidationError):
        TicketContract.model_validate(payload)


def test_enum_authority_class_values() -> None:
    assert [value.value for value in TicketClass] == [
        "subscription-claude",
        "subscription-codex",
        "operator-window-top",
        "operator-window-deputy",
        "operator-rehearsal",
    ]


def test_l2_reason_categories() -> None:
    assert [value.value for value in L2Reason] == [
        "execution-only",
        "witness-only",
        "approval-only",
        "credential-entry",
        "production-touch",
        "release-governance",
        "roster-mutation",
        "override-action",
        "destructive-action",
    ]


def test_l1_exclusive_reason_required_when_top() -> None:
    bad_payload = _model_construct_payload()
    bad_payload.update(
        {
            "ticket_class": TicketClass.OPERATOR_WINDOW_TOP,
            "l1_exclusive_reason": None,
        }
    )
    contract = TicketContract.model_construct(**bad_payload)

    errors = validate_forbidden_combinations(contract)

    assert [error.rule_id for error in errors] == [3]


def test_schema_version_locked_to_v0() -> None:
    payload = _load_fixture_payload()
    payload["schema_version"] = "v1"

    with pytest.raises(ValidationError):
        TicketContract.model_validate(payload)
