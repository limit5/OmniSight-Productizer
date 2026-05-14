from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.preflight.meta_blockedby import (
    PreflightError,
    check_meta_blocked_by_completeness,
)
from governance_engine.schema.v1 import TicketContractV1


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"


def _ticket(ticket_key: str, **overrides: object) -> TicketContractV1:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload.update(
        {
            "schema_version": "v1",
            "ticket_key": ticket_key,
            "labels": ["phase:G.A-v1", "class:subscription-codex"],
            "phase_plugin_version": "v1",
        }
    )
    payload.update(overrides)
    return TicketContractV1.model_validate(payload)


def test_exact_count_passes() -> None:
    meta = _ticket("OP-1000", tag_type="meta")
    child_a = _ticket("OP-1001", blocked_by=["OP-1000"])
    child_b = _ticket("OP-1002", blocked_by=["OP-1000"])

    assert check_meta_blocked_by_completeness(
        [meta, child_a, child_b],
        {"OP-1000": 2},
    ) == []


def test_one_extra_child_fails() -> None:
    meta = _ticket("OP-1000", tag_type="meta")
    child_a = _ticket("OP-1001", blocked_by=["OP-1000"])
    child_b = _ticket("OP-1002", blocked_by=["OP-1000"])

    assert check_meta_blocked_by_completeness(
        [meta, child_a, child_b],
        {"OP-1000": 1},
    ) == [
        PreflightError(
            meta_key="OP-1000",
            claimed=1,
            actual=2,
        )
    ]


def test_one_missing_child_fails() -> None:
    meta = _ticket("OP-1000", tag_type="meta")
    child = _ticket("OP-1001", blocked_by=["OP-1000"])

    errors = check_meta_blocked_by_completeness(
        [meta, child],
        {"OP-1000": 2},
    )

    assert len(errors) == 1
    assert errors[0].meta_key == "OP-1000"
    assert errors[0].claimed == 2
    assert errors[0].actual == 1
    assert errors[0].rule_id == "meta-blockedby-count-mismatch"


def test_no_meta_passes_vacuously() -> None:
    story = _ticket("OP-1001", blocked_by=["OP-1000"])

    assert check_meta_blocked_by_completeness(
        [story],
        {"OP-1000": 1},
    ) == []
