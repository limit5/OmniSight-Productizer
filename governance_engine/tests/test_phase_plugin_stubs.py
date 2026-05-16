from __future__ import annotations

import importlib
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.plugins.registry import DEFAULT_REGISTRY
from governance_engine.schema.v1 import TicketContractV1


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"
PHASE_SUFFIXES = tuple("abcdefghijk")


def _ticket(**overrides: object) -> TicketContractV1:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload.update({
        "schema_version": "v1",
        "ticket_key": "OP-1085",
        "labels": ["phase:31.A", "class:subscription-codex"],
        "phase_plugin_version": "v1",
    })
    payload.update(overrides)
    return TicketContractV1.model_validate(payload)


def _plugin_class(suffix: str) -> type:
    module = importlib.import_module(f"governance_engine.plugins.phase_31{suffix}")
    return getattr(module, f"Phase31{suffix.upper()}Plugin")


def test_all_11_phases_registered() -> None:
    phase_plugins = [
        plugin for plugin in DEFAULT_REGISTRY.all() if plugin.phase_id.startswith("31.")
    ]

    assert len(phase_plugins) == 11


def test_each_phase_id_matches_module() -> None:
    for suffix in PHASE_SUFFIXES:
        plugin_class = _plugin_class(suffix)

        assert plugin_class.phase_id == f"31.{suffix.upper()}"


def test_each_stub_has_v1_class_attrs() -> None:
    for suffix in PHASE_SUFFIXES:
        plugin = _plugin_class(suffix)()

        assert plugin.plugin_version == "v1"
        assert plugin.schema_versions_supported == frozenset({"v0", "v1"})
        assert plugin.ruleset_id == f"phase-31{suffix}-rules-v1"


def test_stubs_have_no_validation_rules_yet() -> None:
    ticket = _ticket()

    for suffix in PHASE_SUFFIXES:
        plugin = _plugin_class(suffix)()

        assert plugin.validate(ticket) == []
