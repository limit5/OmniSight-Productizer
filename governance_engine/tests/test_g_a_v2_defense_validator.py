"""OP-1101: G.A-v2 defense_contract validator plugin."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.plugins.g_a_v2_defense_validator import (  # noqa: E402
    GAV2DefenseValidatorPlugin,
)
from governance_engine.plugins.registry import (  # noqa: E402
    DEFAULT_REGISTRY,
    DEFAULT_PLUGIN_MODULES,
    load_plugins_from_module_paths,
)
from governance_engine.schema.v1 import TicketContractV1  # noqa: E402


BASE_FIXTURE = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"
DEFENSE_FIXTURES = Path(__file__).parent / "fixtures" / "defense_contract"


def _fixture(name: str) -> dict[str, Any]:
    return yaml.safe_load((DEFENSE_FIXTURES / name).read_text(encoding="utf-8"))


def _base_payload() -> dict[str, Any]:
    payload = yaml.safe_load(BASE_FIXTURE.read_text(encoding="utf-8"))
    payload.update(
        {
            "schema_version": "v1",
            "ticket_key": "OP-1101",
            "labels": ["phase:G.A-v2", "class:subscription-codex"],
            "phase_plugin_version": "v1",
        }
    )
    return payload


def _payload(name: str) -> dict[str, Any]:
    payload = _base_payload()
    fixture = _fixture(name)
    payload.update(fixture.get("ticket_overrides", {}))
    if "defense_contract" in fixture:
        payload["defense_contract"] = fixture["defense_contract"]
    return payload


def test_defense_contract_fixture_set_has_12_plus_cases() -> None:
    fixtures = sorted(DEFENSE_FIXTURES.glob("*.yaml"))

    assert len(fixtures) >= 12


@pytest.mark.parametrize(
    "fixture_name",
    [
        "valid_structured_log.yaml",
        "valid_endpoint_status.yaml",
        "valid_exit_code.yaml",
        "valid_none_local.yaml",
    ],
)
def test_a3a_shape_validator_accepts_valid_dimension_sets(fixture_name: str) -> None:
    errors = GAV2DefenseValidatorPlugin().validate_payload(_payload(fixture_name))

    assert errors == []


@pytest.mark.parametrize(
    "fixture_name",
    [
        "shape_missing_error_detection.yaml",
        "shape_missing_exception_handling.yaml",
        "shape_missing_shutdown_contract.yaml",
        "shape_missing_recovery_path.yaml",
        "shape_extra_field.yaml",
    ],
)
def test_a3a_shape_validator_rejects_missing_or_extra_fields(fixture_name: str) -> None:
    errors = GAV2DefenseValidatorPlugin().validate_payload(_payload(fixture_name))

    assert [error.rule_id for error in errors] == ["defense-contract-shape"]
    assert errors[0].severity == "error"


@pytest.mark.parametrize(
    ("fixture_name", "rule_id"),
    [
        (
            "cross_field_user_facing_no_remediation.yaml",
            "D2-remediation-on-user-facing",
        ),
        (
            "cross_field_non_idempotent_no_rescue.yaml",
            "D5-required-when-recovery-not-idempotent",
        ),
    ],
)
def test_a3b_cross_field_validator_consumes_forbidden_rules(
    fixture_name: str,
    rule_id: str,
) -> None:
    payload = _payload(fixture_name)
    ticket = TicketContractV1.model_validate(payload)

    errors = GAV2DefenseValidatorPlugin().validate(ticket)

    assert [error.rule_id for error in errors] == [rule_id]
    assert errors[0].severity == "error"


def test_a3c_bootstrap_mode_warns_for_a1_to_a7_missing_contract() -> None:
    errors = GAV2DefenseValidatorPlugin(bootstrap_mode=True).validate_payload(
        _payload("bootstrap_missing_contract.yaml")
    )

    assert [error.rule_id for error in errors] == ["defense-contract-bootstrap-gap"]
    assert errors[0].severity == "warning"


def test_regular_mode_rejects_missing_defense_contract() -> None:
    payload = _base_payload()
    payload["defense_contract"] = None

    errors = GAV2DefenseValidatorPlugin().validate_payload(payload)

    assert [error.rule_id for error in errors] == ["defense-contract-required"]
    assert errors[0].severity == "error"


def test_default_registry_discovers_defense_validator() -> None:
    plugin = DEFAULT_REGISTRY.get("G.A-v2")

    assert isinstance(plugin, GAV2DefenseValidatorPlugin)
    assert plugin.ruleset_id == "g-a-v2-defense-contract-rules-v1"


def test_registry_loads_defense_validator_with_audit_mode_flag() -> None:
    registry = load_plugins_from_module_paths(DEFAULT_PLUGIN_MODULES, audit_mode=True)
    plugin = registry.get("G.A-v2")

    assert isinstance(plugin, GAV2DefenseValidatorPlugin)
    assert plugin.audit_mode is True


def test_audit_mode_warns_for_self_audit_bootstrap_ticket() -> None:
    registry = load_plugins_from_module_paths(DEFAULT_PLUGIN_MODULES, audit_mode=True)
    plugin = registry.get("G.A-v2")
    assert isinstance(plugin, GAV2DefenseValidatorPlugin)

    errors = plugin.validate_payload(_payload("audit_mode_missing_contract.yaml"))

    assert [error.rule_id for error in errors] == ["defense-contract-bootstrap-gap"]
    assert errors[0].severity == "warning"


def test_v2_a8_self_audit_dry_run_produces_evidence_json() -> None:
    plugin = GAV2DefenseValidatorPlugin(audit_mode=True)

    evidence = json.loads(
        plugin.dry_run_evidence_json(_payload("audit_mode_missing_contract.yaml"))
    )

    assert evidence["ruleset_id"] == "g-a-v2-defense-contract-rules-v1"
    assert evidence["audit_mode"] is True
    assert evidence["ticket_key"] == "v2-A7"
    assert evidence["errors"][0]["severity"] == "warning"
