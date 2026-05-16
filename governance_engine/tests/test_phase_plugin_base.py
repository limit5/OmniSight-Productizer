from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.plugins.base import (
    BasePhasePlugin,
    PhasePlugin,
    PluginError,
)
from governance_engine.schema.v1 import TicketContractV1


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"
)


def _v1_ticket(**overrides: object) -> TicketContractV1:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload.update(
        {
            "schema_version": "v1",
            "ticket_key": "OP-1099",
            "labels": ["phase:31.A", "class:subscription-claude"],
            "phase_plugin_version": "v1",
        }
    )
    payload.update(overrides)
    return TicketContractV1.model_validate(payload)


class _Phase31APlugin(BasePhasePlugin):
    phase_id = "31.A"


def test_base_plugin_validate_returns_empty_list() -> None:
    plugin = _Phase31APlugin()
    ticket = _v1_ticket()

    assert plugin.validate(ticket) == []


def test_base_plugin_applies_to_via_label() -> None:
    plugin = _Phase31APlugin()
    ticket = _v1_ticket()

    assert plugin.applies_to(ticket) is True

    other_phase = _v1_ticket(labels=["phase:31.B", "class:subscription-claude"])
    assert plugin.applies_to(other_phase) is False


def test_base_plugin_applies_to_respects_schema_version() -> None:
    plugin = _Phase31APlugin()
    base_ticket = _v1_ticket()
    synthesized_v2 = base_ticket.model_copy(update={"schema_version": "v2"})

    assert synthesized_v2.schema_version == "v2"
    assert plugin.applies_to(synthesized_v2) is False


def test_plugin_error_has_required_fields() -> None:
    err = PluginError(
        phase_id="31.A",
        rule_id="31a-example",
        severity="error",
        detail="example failure",
    )

    assert err.phase_id == "31.A"
    assert err.rule_id == "31a-example"
    assert err.severity == "error"
    assert err.detail == "example failure"


def test_protocol_runtime_checkable_isinstance() -> None:
    class HandRolled:
        phase_id = "31.A"
        plugin_version = "v1"
        schema_versions_supported = frozenset({"v0", "v1"})
        ruleset_id = "phase-31a-rules-v1"

        def validate(self, ticket: TicketContractV1) -> list[PluginError]:
            return []

        def applies_to(self, ticket: TicketContractV1) -> bool:
            return True

    assert isinstance(HandRolled(), PhasePlugin)

    class MissingMethod:
        phase_id = "31.A"
        plugin_version = "v1"
        schema_versions_supported = frozenset({"v0", "v1"})
        ruleset_id = "phase-31a-rules-v1"

    assert not isinstance(MissingMethod(), PhasePlugin)


def test_base_plugin_class_attrs_populated() -> None:
    plugin = _Phase31APlugin()

    assert plugin.plugin_version == "v1"
    assert plugin.schema_versions_supported == frozenset({"v0", "v1"})
    assert plugin.ruleset_id == "phase-31a-rules-v1"
    assert isinstance(plugin, PhasePlugin)
