from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.preflight.mutex_consistency import (
    PreflightError,
    check_mutex_consistency,
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


def test_symmetric_mutex_passes() -> None:
    first = _ticket(
        "OP-1001",
        scope_components=["runner-pipeline"],
        mutex_with=["OP-1002"],
    )
    second = _ticket(
        "OP-1002",
        scope_components=["runner-pipeline"],
        mutex_with=["OP-1001"],
    )

    assert check_mutex_consistency([first, second]) == []


def test_asymmetric_mutex_fails() -> None:
    first = _ticket(
        "OP-1001",
        scope_components=["runner-pipeline"],
        mutex_with=["OP-1002"],
    )
    second = _ticket(
        "OP-1002",
        scope_components=["runner-pipeline"],
        mutex_with=[],
    )

    errors = check_mutex_consistency([first, second])

    assert errors == [
        PreflightError(
            ticket_key_a="OP-1001",
            ticket_key_b="OP-1002",
            shared_component="runner-pipeline",
        )
    ]
    assert errors[0].rule_id == "mutex-asymmetric"


def test_no_shared_component_skips() -> None:
    first = _ticket(
        "OP-1001",
        scope_components=["runner-pipeline"],
        mutex_with=[],
    )
    second = _ticket(
        "OP-1002",
        scope_components=["operator-console"],
        mutex_with=[],
    )

    assert check_mutex_consistency([first, second]) == []


def test_meta_tickets_excluded() -> None:
    meta = _ticket(
        "OP-1001",
        scope_components=["runner-pipeline"],
        mutex_with=[],
        tag_type="meta",
    )
    story = _ticket(
        "OP-1002",
        scope_components=["runner-pipeline"],
        mutex_with=[],
    )

    assert check_mutex_consistency([meta, story]) == []
