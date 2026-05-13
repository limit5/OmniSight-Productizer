from __future__ import annotations

from pathlib import Path
import sys

import pytest
import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.schema.v0 import TicketContract
from governance_engine.schema.v1 import CrossPhaseBlocker, TicketContractV1


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"

EVIDENCE_CLASSES = [
    "unit",
    "integration",
    "operator",
    "audit-log",
    "structural",
    "behavioral-smoke",
    "semantic",
    "longitudinal",
]
EXTERNAL_PAYLOAD_CLASSES = [
    "operational",
    "sensitive",
    "public",
    "credential-material",
]


def _load_v0_payload() -> dict[str, object]:
    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


def _load_v1_payload() -> dict[str, object]:
    payload = _load_v0_payload()
    payload.update(
        {
            "schema_version": "v1",
            "ticket_key": "OP-1099",
            "labels": ["phase:31.X", "class:subscription-codex"],
            "phase_plugin_version": "v1",
        }
    )
    return payload


def test_v1_extends_v0_fields() -> None:
    payload = _load_v1_payload()
    contract = TicketContractV1.model_validate(payload)
    dumped = contract.model_dump()

    assert set(TicketContract.model_fields).issubset(TicketContractV1.model_fields)
    for field_name in TicketContract.model_fields:
        assert field_name in dumped


def test_ticket_key_required() -> None:
    payload = _load_v1_payload()
    payload.pop("ticket_key")

    with pytest.raises(ValidationError):
        TicketContractV1.model_validate(payload)


@pytest.mark.parametrize(
    ("ticket_key", "valid"),
    [
        ("OP-1099", True),
        ("DEV-100", False),
        ("op-1099", False),
    ],
)
def test_ticket_key_format(ticket_key: str, valid: bool) -> None:
    payload = _load_v1_payload()
    payload["ticket_key"] = ticket_key

    if valid:
        assert TicketContractV1.model_validate(payload).ticket_key == ticket_key
    else:
        with pytest.raises(ValidationError):
            TicketContractV1.model_validate(payload)


def test_labels_default_to_empty_list_NOT_allowed() -> None:
    payload = _load_v1_payload()
    payload.pop("labels")

    with pytest.raises(ValidationError):
        TicketContractV1.model_validate(payload)


def test_blocked_by_default_to_empty_list_allowed() -> None:
    payload = _load_v1_payload()
    payload.pop("blocked_by", None)

    assert TicketContractV1.model_validate(payload).blocked_by == []


def test_cross_phase_blockers_accepts_structured_form() -> None:
    payload = _load_v1_payload()
    payload["cross_phase_blockers"] = [
        {
            "blocker_phase": "31.E",
            "blocker_artifact": "image-publish",
            "reason": "image SHA must land first",
        }
    ]

    contract = TicketContractV1.model_validate(payload)

    assert contract.cross_phase_blockers == [
        CrossPhaseBlocker(
            blocker_phase="31.E",
            blocker_artifact="image-publish",
            reason="image SHA must land first",
        )
    ]


def test_cross_phase_blockers_rejects_prose_only_strings() -> None:
    payload = _load_v1_payload()
    payload["cross_phase_blockers"] = "external image-publish must land first"

    with pytest.raises(ValidationError):
        TicketContractV1.model_validate(payload)


def test_evidence_class_closed_enum() -> None:
    for evidence_class in EVIDENCE_CLASSES:
        payload = _load_v1_payload()
        payload["evidence_class"] = evidence_class
        assert TicketContractV1.model_validate(payload).evidence_class == evidence_class

    payload = _load_v1_payload()
    payload["evidence_class"] = "contract"
    with pytest.raises(ValidationError):
        TicketContractV1.model_validate(payload)


def test_external_payload_class_closed_enum() -> None:
    for external_payload_class in EXTERNAL_PAYLOAD_CLASSES:
        payload = _load_v1_payload()
        payload["external_payload_class"] = external_payload_class
        assert (
            TicketContractV1.model_validate(payload).external_payload_class
            == external_payload_class
        )

    payload = _load_v1_payload()
    payload["external_payload_class"] = "secret"
    with pytest.raises(ValidationError):
        TicketContractV1.model_validate(payload)


def test_credential_material_payload_parses() -> None:
    payload = _load_v1_payload()
    payload["external_payload_class"] = "credential-material"
    payload["authority_required"] = "L3"

    contract = TicketContractV1.model_validate(payload)

    assert contract.external_payload_class == "credential-material"
    assert contract.authority_required == "L3"


@pytest.mark.parametrize("schema_version", ["v0", "v1"])
def test_schema_version_accepts_v0_or_v1(schema_version: str) -> None:
    payload = _load_v1_payload()
    payload["schema_version"] = schema_version

    assert TicketContractV1.model_validate(payload).schema_version == schema_version


def test_schema_version_rejects_v2() -> None:
    payload = _load_v1_payload()
    payload["schema_version"] = "v2"

    with pytest.raises(ValidationError):
        TicketContractV1.model_validate(payload)
