from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.plugins.base import BasePhasePlugin
from governance_engine.preflight.plugin_version import (
    PluginRegistry,
    PreflightError,
    check_plugin_version_compatibility,
)
from governance_engine.schema.v1 import TicketContractV1

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"


def _ticket(**overrides: object) -> TicketContractV1:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload.update({
        "schema_version": "v1",
        "ticket_key": "OP-1001",
        "labels": ["phase:31.A", "class:subscription-claude"],
        "phase_plugin_version": "v1",
    })
    payload.update(overrides)
    return TicketContractV1.model_validate(payload)


class _Phase31AV1(BasePhasePlugin):
    phase_id = "31.A"


class _Phase31AV2(BasePhasePlugin):
    phase_id = "31.A"
    plugin_version = "v2"  # type: ignore[assignment]


def test_matching_version_passes() -> None:
    registry = PluginRegistry(plugins={"31.A": _Phase31AV1()})
    assert check_plugin_version_compatibility([_ticket()], registry) == []


def test_mismatched_version_fails() -> None:
    registry = PluginRegistry(plugins={"31.A": _Phase31AV2()})
    errors = check_plugin_version_compatibility([_ticket()], registry)
    assert len(errors) == 1
    err = errors[0]
    assert err.rule_id == "plugin-version-mismatch"
    assert err.ticket_key == "OP-1001"
    assert err.expected_plugin_version == "v1"
    assert err.actual_plugin_version == "v2"
    assert "v2" in err.detail


def test_unknown_phase_fails() -> None:
    registry = PluginRegistry(plugins={"31.A": _Phase31AV1()})
    ticket = _ticket(labels=["phase:31.K", "class:subscription-claude"])
    errors = check_plugin_version_compatibility([ticket], registry)
    assert errors == [PreflightError(
        ticket_key="OP-1001",
        expected_plugin_version="v1",
        actual_plugin_version=None,
        detail=errors[0].detail,
    )]
    assert "31.K" in errors[0].detail
