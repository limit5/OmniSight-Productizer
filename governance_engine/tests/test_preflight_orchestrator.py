from __future__ import annotations

from pathlib import Path

import yaml

from governance_engine.plugins.base import BasePhasePlugin
from governance_engine.preflight.orchestrator import run_all_preflight_checks
from governance_engine.preflight.plugin_version import PluginRegistry
from governance_engine.schema.v1 import TicketContractV1

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"
BROKEN_ROSTER_PATH = Path(__file__).parent / "fixtures" / "v1_preflight_broken_roster.yaml"

EXPECTED_RULE_IDS = {
    "plugin-version-mismatch",
    "credential-material-not-l1-non-subscription",
    "cross-phase-blocker-shape",
    "dependency-orphan",
    "meta-blockedby-count-mismatch",
    "mutex-asymmetric",
    "path-collision",
}


class _Phase31AV1(BasePhasePlugin):
    phase_id = "31.A"


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


def _broken_roster() -> list[TicketContractV1]:
    payloads = yaml.safe_load(BROKEN_ROSTER_PATH.read_text(encoding="utf-8"))
    return [TicketContractV1.model_validate(payload) for payload in payloads]


def test_clean_roster_returns_empty_errors() -> None:
    registry = PluginRegistry(plugins={"31.A": _Phase31AV1()})
    roster = [_ticket("OP-1201")]

    assert run_all_preflight_checks(roster, registry, {}) == []


def test_each_check_module_runs() -> None:
    errors = run_all_preflight_checks(_broken_roster(), PluginRegistry(), {"OP-1100": 2})

    assert EXPECTED_RULE_IDS <= {error.rule_id for error in errors}


def test_orchestrator_ordering_is_stable() -> None:
    roster = _broken_roster()
    registry = PluginRegistry()
    claimed_child_count = {"OP-1100": 2}

    first = run_all_preflight_checks(roster, registry, claimed_child_count)
    second = run_all_preflight_checks(roster, registry, claimed_child_count)

    assert first == second
