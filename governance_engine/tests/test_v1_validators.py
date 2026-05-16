from __future__ import annotations

from pathlib import Path
import sys

import pytest
import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.schema.v1 import TicketContractV1
from governance_engine.schema.v1_validators import (
    validate_external_artifact_name,
    validate_l1_exclusive_reason_value,
    validate_required_path_shape,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"
KNOWN_L1_EXCLUSIVE_REASONS = [
    "key-material",
    "meta-closure",
    "release-tag-governance",
    "override",
    "roster-mutation",
    "governance-engine-foundation",
]


def _load_v1_payload() -> dict[str, object]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload.update(
        {
            "schema_version": "v1",
            "ticket_key": "OP-1080",
            "labels": ["phase:31.G", "class:subscription-codex"],
            "phase_plugin_version": "v1",
        }
    )
    return payload


def test_required_path_rejects_double_dot() -> None:
    assert validate_required_path_shape("backend/../foo.py") is False
    payload = _load_v1_payload()
    payload["required_paths"] = ["backend/../foo.py"]

    with pytest.raises(ValidationError) as excinfo:
        TicketContractV1.model_validate(payload)

    assert "required_paths" in str(excinfo.value)
    assert "backend/../foo.py" in str(excinfo.value)
    assert "no '..', leading '/', or '\\\\'" in str(excinfo.value)


def test_required_path_rejects_absolute() -> None:
    assert validate_required_path_shape("/backend/agents/foo.py") is False
    payload = _load_v1_payload()
    payload["required_paths"] = ["/backend/agents/foo.py"]

    with pytest.raises(ValidationError) as excinfo:
        TicketContractV1.model_validate(payload)

    assert "required_paths" in str(excinfo.value)
    assert "/backend/agents/foo.py" in str(excinfo.value)


def test_required_path_rejects_backslash() -> None:
    assert validate_required_path_shape(r"backend\agents\foo.py") is False
    payload = _load_v1_payload()
    payload["required_paths"] = [r"backend\agents\foo.py"]

    with pytest.raises(ValidationError) as excinfo:
        TicketContractV1.model_validate(payload)

    assert "required_paths" in str(excinfo.value)
    assert r"backend\\agents\\foo.py" in str(excinfo.value)


@pytest.mark.parametrize(
    "path",
    [
        "backend/agents/foo.py",
        "docs/adr/ADR-0035.md",
    ],
)
def test_required_path_accepts_typical(path: str) -> None:
    assert validate_required_path_shape(path) is True
    payload = _load_v1_payload()
    payload["required_paths"] = [path]

    assert TicketContractV1.model_validate(payload).required_paths == [path]


@pytest.mark.parametrize(
    "name",
    [
        "image-publish",
        "deploy-yaml",
    ],
)
def test_external_artifact_name_accepts_typical(name: str) -> None:
    assert validate_external_artifact_name(name) is True
    payload = _load_v1_payload()
    payload["cross_phase_blockers"] = [
        {
            "blocker_phase": "31.G",
            "blocker_artifact": name,
            "reason": "producer artifact must land first",
        }
    ]

    contract = TicketContractV1.model_validate(payload)

    assert contract.cross_phase_blockers[0].blocker_artifact == name


def test_external_artifact_name_rejects_uppercase() -> None:
    assert validate_external_artifact_name("Image-Publish") is False
    payload = _load_v1_payload()
    payload["cross_phase_blockers"] = [
        {
            "blocker_phase": "31.G",
            "blocker_artifact": "Image-Publish",
            "reason": "producer artifact must land first",
        }
    ]

    with pytest.raises(ValidationError) as excinfo:
        TicketContractV1.model_validate(payload)

    assert "cross_phase_blockers" in str(excinfo.value)
    assert "Image-Publish" in str(excinfo.value)
    assert "^[a-z][a-z0-9-]{0,62}$" in str(excinfo.value)


@pytest.mark.parametrize("reason", KNOWN_L1_EXCLUSIVE_REASONS)
def test_l1_exclusive_reason_accepts_5_known_enum_values(reason: str) -> None:
    assert validate_l1_exclusive_reason_value(reason) is True
    payload = _load_v1_payload()
    payload["l1_exclusive_reason"] = reason

    assert TicketContractV1.model_validate(payload).l1_exclusive_reason == reason


def test_l1_exclusive_reason_accepts_freeform_custom() -> None:
    reason = "because I said so"
    assert validate_l1_exclusive_reason_value(reason) is True
    payload = _load_v1_payload()
    payload["l1_exclusive_reason"] = reason

    assert TicketContractV1.model_validate(payload).l1_exclusive_reason == reason
