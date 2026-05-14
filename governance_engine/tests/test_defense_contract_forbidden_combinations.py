"""OP-1100: G.A-v2 defense_contract forbidden-combination rules."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.schema.defense_contract import (  # noqa: E402
    DefenseContract,
    RecoveryPath,
    RescuePath,
)
from governance_engine.schema.forbidden_combinations import (  # noqa: E402
    validate_forbidden_combinations,
)
from governance_engine.schema.v1 import TicketContractV1  # noqa: E402


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"
_UNSET = object()


def _load_v1_payload() -> dict[str, Any]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload.update(
        {
            "schema_version": "v1",
            "ticket_key": "OP-1100",
            "labels": ["phase:G.A-v2", "class:subscription-codex"],
            "phase_plugin_version": "v1",
            "defense_contract": _defense_contract_payload(),
        }
    )
    return payload


def _defense_contract_payload() -> dict[str, Any]:
    return {
        "error_detection": {
            "signal": "structured_log",
            "channel": "log:governance-defense-contract",
            "detection_latency_p99": "30s",
            "observability_test": "governance_engine/tests/test_defense_contract_forbidden_combinations.py",
            "alert_rule_id": None,
        },
        "exception_handling": {
            "enumerated_states": ["validation_error", "operator_blocked"],
            "remediation_hint_contract": "return validator detail and remediation hint",
            "user_facing": True,
            "fail_loudly": True,
        },
        "shutdown_contract": {
            "drain_seconds_p99": None,
            "cleanup_sequence": [],
            "forced_termination_safe": True,
            "state_persistence": "none",
        },
        "recovery_path": {
            "trigger_condition": "forbidden-combination regression detected by pytest",
            "steps": ["revert validator change", "rerun governance tests"],
            "idempotent": True,
            "rto_seconds_p99": 300,
            "evidence_file": "governance_engine/tests/test_defense_contract_forbidden_combinations.py",
        },
        "rescue_path": {
            "trigger_condition": "pytest remains red after revert",
            "operator_actions": ["comment on OP-1100", "transition ticket back to TODO"],
            "authority_required": "L2",
            "audit_trail": "JIRA OP-1100 comments",
        },
    }


def _validated_contract(
    *,
    payload_overrides: dict[str, Any] | None = None,
    defense_overrides: dict[str, Any] | None = None,
) -> TicketContractV1:
    payload = _load_v1_payload()
    if defense_overrides:
        defense_payload = deepcopy(payload["defense_contract"])
        for section, values in defense_overrides.items():
            if values is None:
                defense_payload[section] = None
            else:
                defense_payload[section].update(values)
        payload["defense_contract"] = defense_payload
    if payload_overrides:
        payload.update(payload_overrides)
    return TicketContractV1.model_validate(payload)


def _constructed_contract(
    *,
    payload_overrides: dict[str, Any] | None = None,
    defense_contract: Any = _UNSET,
) -> TicketContractV1:
    payload = _validated_contract().model_dump()
    if defense_contract is not _UNSET:
        payload["defense_contract"] = defense_contract
    if payload_overrides:
        payload.update(payload_overrides)
    return TicketContractV1.model_construct(**payload)


def _defense_contract(**section_overrides: dict[str, Any]) -> DefenseContract:
    payload = _defense_contract_payload()
    for section, values in section_overrides.items():
        payload[section].update(values)
    return DefenseContract.model_validate(payload)


def _defense_contract_with_constructed_recovery(
    *,
    recovery_overrides: dict[str, Any] | None = None,
    rescue_overrides: dict[str, Any] | None = None,
) -> DefenseContract:
    defense = _defense_contract()
    if recovery_overrides:
        recovery = defense.recovery_path.model_dump()
        recovery.update(recovery_overrides)
        defense = defense.model_copy(
            update={"recovery_path": RecoveryPath.model_construct(**recovery)}
        )
    if rescue_overrides:
        rescue = defense.rescue_path.model_dump()
        rescue.update(rescue_overrides)
        defense = defense.model_copy(
            update={"rescue_path": RescuePath.model_construct(**rescue)}
        )
    return defense


@pytest.mark.parametrize(
    ("rule_id", "rule_name", "contract"),
    [
        pytest.param(
            11,
            "D1-required-for-runtime",
            _constructed_contract(
                payload_overrides={
                    "runtime_capability": "network-production-when-apply"
                },
                defense_contract=_defense_contract(error_detection={"signal": "none"}),
            ),
            id="d1_runtime_needs_error_detection",
        ),
        pytest.param(
            12,
            "D2-loud-or-none",
            _constructed_contract(
                payload_overrides={"external_side_effect": "network-production"},
                defense_contract=_defense_contract(
                    exception_handling={"fail_loudly": False}
                ),
            ),
            id="d2_prod_side_effect_must_fail_loudly",
        ),
        pytest.param(
            13,
            "D2-remediation-on-user-facing",
            _validated_contract(
                defense_overrides={
                    "exception_handling": {"remediation_hint_contract": "none"}
                }
            ),
            id="d2_user_facing_needs_remediation_hint",
        ),
        pytest.param(
            14,
            "D3-required-for-lifecycle",
            _constructed_contract(
                payload_overrides={"scope_components": ["container"]},
                defense_contract={
                    "error_detection": _defense_contract_payload()["error_detection"],
                    "exception_handling": _defense_contract_payload()["exception_handling"],
                    "recovery_path": _defense_contract_payload()["recovery_path"],
                    "rescue_path": _defense_contract_payload()["rescue_path"],
                },
            ),
            id="d3_lifecycle_needs_shutdown_contract",
        ),
        pytest.param(
            15,
            "D4-required-for-destructive",
            _constructed_contract(
                payload_overrides={
                    "authority_required": "L1",
                    "destructive_op_classes": ["filesystem-delete"],
                },
                defense_contract=_defense_contract_with_constructed_recovery(
                    recovery_overrides={"evidence_file": None}
                ),
            ),
            id="d4_destructive_needs_recovery_evidence",
        ),
        pytest.param(
            16,
            "D5-required-when-recovery-not-idempotent",
            _constructed_contract(
                defense_contract=_defense_contract_with_constructed_recovery(
                    recovery_overrides={"idempotent": False},
                    rescue_overrides={"trigger_condition": "none"},
                ),
            ),
            id="d5_non_idempotent_recovery_needs_rescue_trigger",
        ),
    ],
)
def test_defense_contract_forbidden_combinations_fire(
    rule_id: int,
    rule_name: str,
    contract: TicketContractV1,
) -> None:
    errors = validate_forbidden_combinations(contract)

    assert [error.rule_id for error in errors] == [rule_id]
    assert errors[0].rule_name == rule_name
    assert errors[0].detail
    assert errors[0].remediation_hint


@pytest.mark.parametrize(
    ("rule_name", "contract"),
    [
        pytest.param(
            "D1-required-for-runtime",
            _constructed_contract(
                payload_overrides={
                    "runtime_capability": "network-production-when-apply"
                },
                defense_contract=_defense_contract(
                    error_detection={"signal": "structured_log"}
                ),
            ),
            id="valid_d1_observable_runtime",
        ),
        pytest.param(
            "D2-loud-or-none",
            _constructed_contract(
                payload_overrides={"external_side_effect": "network-production"},
                defense_contract=_defense_contract(
                    exception_handling={"fail_loudly": True}
                ),
            ),
            id="valid_d2_prod_side_effect_fails_loudly",
        ),
        pytest.param(
            "D2-remediation-on-user-facing",
            _validated_contract(
                defense_overrides={
                    "exception_handling": {
                        "remediation_hint_contract": "tell the user to retry after rollback"
                    }
                }
            ),
            id="valid_d2_user_facing_has_remediation_hint",
        ),
        pytest.param(
            "D3-required-for-lifecycle",
            _validated_contract(payload_overrides={"scope_components": ["container"]}),
            id="valid_d3_lifecycle_has_shutdown_contract",
        ),
        pytest.param(
            "D4-required-for-destructive",
            _validated_contract(
                payload_overrides={
                    "authority_required": "L1",
                    "destructive_op_classes": ["filesystem-delete"],
                }
            ),
            id="valid_d4_destructive_has_recovery_evidence",
        ),
        pytest.param(
            "D5-required-when-recovery-not-idempotent",
            _validated_contract(
                defense_overrides={"recovery_path": {"idempotent": False}}
            ),
            id="valid_d5_non_idempotent_has_rescue_trigger",
        ),
    ],
)
def test_defense_contract_forbidden_combinations_accept_valid_pairs(
    rule_name: str,
    contract: TicketContractV1,
) -> None:
    errors = validate_forbidden_combinations(contract)

    assert rule_name not in {error.rule_name for error in errors}
