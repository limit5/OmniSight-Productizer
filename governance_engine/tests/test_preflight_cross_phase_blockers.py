from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.preflight.cross_phase_blockers import (
    PreflightError,
    check_cross_phase_blocker_shape,
)
from governance_engine.schema.v1 import TicketContractV1


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"


def _ticket(**blocker_overrides: object) -> TicketContractV1:
    blocker = {
        "blocker_phase": "31.E",
        "blocker_artifact": "image-publish",
        "reason": "Image publish job must exist before consumers can pull.",
    }
    blocker.update(blocker_overrides)
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload.update({
        "schema_version": "v1",
        "ticket_key": "OP-1001",
        "labels": ["phase:31.A", "class:subscription-claude"],
        "phase_plugin_version": "v1",
        "cross_phase_blockers": [blocker],
    })
    return TicketContractV1.model_validate(payload)


def test_valid_blocker_passes() -> None:
    assert check_cross_phase_blocker_shape([_ticket()]) == []


def test_governance_phase_blocker_passes() -> None:
    assert check_cross_phase_blocker_shape([_ticket(blocker_phase="G.A-v1")]) == []


def test_bad_phase_format_fails() -> None:
    errors = check_cross_phase_blocker_shape([_ticket(blocker_phase="phase-31E")])
    assert len(errors) == 1
    assert errors[0].ticket_key == "OP-1001"
    assert errors[0].field == "blocker_phase"
    assert errors[0].value == "phase-31E"
    assert errors[0].rule_id == "cross-phase-blocker-shape"


def test_bad_artifact_format_fails() -> None:
    errors = check_cross_phase_blocker_shape([_ticket(blocker_artifact="Image_Publish")])
    assert len(errors) == 1
    assert errors[0].field == "blocker_artifact"
    assert errors[0].value == "Image_Publish"
    assert errors[0].rule_id == "cross-phase-blocker-shape"


def test_empty_reason_fails() -> None:
    assert check_cross_phase_blocker_shape([_ticket(reason="")]) == [
        PreflightError(
            ticket_key="OP-1001", field="reason", value="",
            detail="reason must be non-empty",
        )
    ]


def test_oversized_reason_fails() -> None:
    oversized = "x" * 501
    errors = check_cross_phase_blocker_shape([_ticket(reason=oversized)])
    assert len(errors) == 1
    assert errors[0].field == "reason"
    assert errors[0].value == oversized
    assert "501" in errors[0].detail
