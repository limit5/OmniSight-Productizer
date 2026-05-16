from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.preflight.dependency_artifacts import (
    PreflightError,
    check_dependency_artifacts,
)
from governance_engine.schema.v1 import TicketContractV1


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"


def _ticket(ticket_key: str, **overrides: object) -> TicketContractV1:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload.update(
        {
            "schema_version": "v1",
            "ticket_key": ticket_key,
            "labels": ["phase:31.A", "class:subscription-claude"],
            "phase_plugin_version": "v1",
        }
    )
    payload.update(overrides)
    return TicketContractV1.model_validate(payload)


def _producer_blocker(artifact: str) -> dict[str, str]:
    return {
        "blocker_phase": "31.E",
        "blocker_artifact": artifact,
        "reason": f"produces {artifact}",
    }


def test_satisfied_dependency_passes() -> None:
    consumer = _ticket("OP-1001", dependency_artifacts=["image-publish"])
    producer = _ticket(
        "OP-1002", cross_phase_blockers=[_producer_blocker("image-publish")]
    )

    assert check_dependency_artifacts([consumer, producer]) == []


def test_orphan_dependency_fails() -> None:
    consumer = _ticket("OP-1001", dependency_artifacts=["nonexistent"])
    bystander = _ticket("OP-1002")

    errors = check_dependency_artifacts([consumer, bystander])

    assert len(errors) == 1
    assert errors[0].ticket_key == "OP-1001"
    assert errors[0].missing_artifact == "nonexistent"
    assert errors[0].rule_id == "dependency-orphan"
    assert "nonexistent" in errors[0].detail


def test_self_satisfaction_does_not_count() -> None:
    self_referential = _ticket(
        "OP-1001",
        dependency_artifacts=["image-publish"],
        cross_phase_blockers=[_producer_blocker("image-publish")],
    )

    errors = check_dependency_artifacts([self_referential])

    assert len(errors) == 1
    assert errors[0] == PreflightError(
        ticket_key="OP-1001",
        missing_artifact="image-publish",
        detail=errors[0].detail,
    )


def test_multiple_orphans_listed() -> None:
    consumer = _ticket(
        "OP-1001", dependency_artifacts=["alpha", "beta", "gamma"]
    )
    bystander = _ticket("OP-1002")

    errors = check_dependency_artifacts([consumer, bystander])

    assert len(errors) == 3
    assert {err.missing_artifact for err in errors} == {"alpha", "beta", "gamma"}
    assert all(err.ticket_key == "OP-1001" for err in errors)
    assert all(err.rule_id == "dependency-orphan" for err in errors)
