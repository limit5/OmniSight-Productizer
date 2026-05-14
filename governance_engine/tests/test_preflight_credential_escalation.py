from pathlib import Path

import yaml

from governance_engine.preflight.credential_escalation import (
    check_credential_material_requires_l1_non_subscription,
)
from governance_engine.schema.v1 import TicketContractV1


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"
CHECK = check_credential_material_requires_l1_non_subscription


def _ticket(
    ticket_class: str,
    authority_required: str,
    external_payload_class: str = "credential-material",
    **overrides: object,
) -> TicketContractV1:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload.update({
        "schema_version": "v1",
        "ticket_key": "OP-1092",
        "labels": ["phase:G.A-v1", "class:subscription-codex"],
        "phase_plugin_version": "v1",
        "class": ticket_class,
        "authority_required": authority_required,
        "external_payload_class": external_payload_class,
    })
    payload.update(overrides)
    return TicketContractV1.model_validate(payload)


def test_credential_with_l1_operator_window_passes() -> None:
    ticket = _ticket(
        "operator-window-top", "L1", l1_exclusive_reason="credential-entry"
    )
    assert CHECK([ticket]) == []


def test_credential_with_l1_but_subscription_class_fails() -> None:
    errors = CHECK([_ticket("subscription-codex", "L1")])
    assert len(errors) == 1
    assert errors[0].ticket_key == "OP-1092"
    assert errors[0].rule_id == "credential-material-not-l1-non-subscription"
    assert errors[0].severity == "error"
    assert "class must not start with subscription-" in errors[0].detail


def test_credential_with_l2_fails() -> None:
    errors = CHECK([
        _ticket("operator-window-deputy", "L2", l2_reason="credential-entry")
    ])
    assert len(errors) == 1
    assert "authority_required must be L1" in errors[0].detail


def test_credential_with_l3_fails() -> None:
    errors = CHECK([_ticket("operator-rehearsal", "L3")])
    assert len(errors) == 1
    assert "authority_required must be L1" in errors[0].detail


def test_non_credential_skipped() -> None:
    assert CHECK([_ticket("subscription-codex", "L3", "operational")]) == []
