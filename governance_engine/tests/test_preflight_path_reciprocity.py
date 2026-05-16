from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.preflight.path_reciprocity import (
    PreflightError,
    check_path_reciprocity,
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


def test_no_overlap_passes() -> None:
    owner = _ticket(
        "OP-1001",
        required_paths=["scripts/owner_only.sh"],
        forbidden_paths=["scripts/excluder_only.sh"],
    )
    other = _ticket(
        "OP-1002",
        required_paths=["scripts/other_only.sh"],
        forbidden_paths=["scripts/some_unrelated.sh"],
    )

    assert check_path_reciprocity([owner, other]) == []


def test_owner_vs_excluder_fails() -> None:
    owner = _ticket(
        "OP-1001",
        required_paths=["scripts/contested.sh"],
        forbidden_paths=[],
    )
    excluder = _ticket(
        "OP-1002",
        required_paths=["scripts/excluder_own.sh"],
        forbidden_paths=["scripts/contested.sh"],
    )

    errors = check_path_reciprocity([owner, excluder])

    assert errors == [
        PreflightError(
            ticket_key_owner="OP-1001",
            ticket_key_excluder="OP-1002",
            path="scripts/contested.sh",
        )
    ]
    assert errors[0].rule_id == "path-collision"


def test_multiple_owners_same_path_skipped() -> None:
    # Two tickets both claim ``shared.sh`` in ``required_paths``. V1-10
    # (mutex_with) is responsible for catching this case; the path
    # reciprocity rule only flags required-vs-forbidden collisions.
    owner_a = _ticket(
        "OP-1001",
        required_paths=["scripts/shared.sh"],
        forbidden_paths=[],
    )
    owner_b = _ticket(
        "OP-1002",
        required_paths=["scripts/shared.sh"],
        forbidden_paths=[],
    )

    assert check_path_reciprocity([owner_a, owner_b]) == []
