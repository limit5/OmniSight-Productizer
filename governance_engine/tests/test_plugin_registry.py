from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.plugins.base import BasePhasePlugin, PluginError
from governance_engine.plugins.registry import (
    PluginRegistry,
    load_plugins_from_module_paths,
)
from governance_engine.schema.v1 import TicketContractV1

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "31a_1a_apt_base_tools.yaml"


def _ticket(**overrides: object) -> TicketContractV1:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload.update({
        "schema_version": "v1",
        "ticket_key": "OP-1084",
        "labels": ["phase:31.A", "class:subscription-codex"],
        "phase_plugin_version": "v1",
    })
    payload.update(overrides)
    return TicketContractV1.model_validate(payload)


class _Phase31APlugin(BasePhasePlugin):
    phase_id = "31.A"


class _Phase31AReplacementPlugin(BasePhasePlugin):
    phase_id = "31.A"


def _write_plugin_module(tmp_path: Path, name: str, source: str) -> Path:
    module_path = tmp_path / f"{name}.py"
    module_path.write_text(source, encoding="utf-8")
    return module_path


def test_registry_register_get_roundtrip() -> None:
    registry = PluginRegistry()
    plugin = _Phase31APlugin()

    registry.register(plugin)

    assert registry.get("31.A") is plugin
    assert registry.get("31.B") is None
    assert registry.all() == [plugin]


def test_registry_validate_all_dispatches_to_matching_phase(tmp_path: Path) -> None:
    _write_plugin_module(
        tmp_path,
        "synthetic_phase_a",
        """
from governance_engine.plugins.base import BasePhasePlugin, PluginError

class SyntheticPhaseAPlugin(BasePhasePlugin):
    phase_id = "31.A"
    def validate(self, ticket):
        return [PluginError(
            phase_id=self.phase_id,
            rule_id="synthetic-31a",
            severity="error",
            detail=ticket.ticket_key,
        )]
""",
    )

    registry = load_plugins_from_module_paths([str(tmp_path)])
    errors = registry.validate_all(_ticket())

    assert errors == [
        PluginError(
            phase_id="31.A",
            rule_id="synthetic-31a",
            severity="error",
            detail="OP-1084",
        )
    ]


def test_registry_validate_all_ignores_non_matching_phase(tmp_path: Path) -> None:
    _write_plugin_module(
        tmp_path,
        "synthetic_phase_b",
        """
from governance_engine.plugins.base import BasePhasePlugin, PluginError

class SyntheticPhaseBPlugin(BasePhasePlugin):
    phase_id = "31.B"
    def validate(self, ticket):
        return [PluginError(
            phase_id=self.phase_id,
            rule_id="synthetic-31b",
            severity="error",
            detail="should not run",
        )]
""",
    )

    registry = load_plugins_from_module_paths([str(tmp_path)])

    assert registry.validate_all(_ticket()) == []


def test_loader_handles_missing_module_gracefully(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_plugin_module(
        tmp_path,
        "synthetic_phase_a",
        """
from governance_engine.plugins.base import BasePhasePlugin

class SyntheticPhaseAPlugin(BasePhasePlugin):
    phase_id = "31.A"
""",
    )

    caplog.set_level(logging.WARNING)
    registry = load_plugins_from_module_paths([
        str(tmp_path),
        "governance_engine.plugins.does_not_exist",
    ])

    assert registry.get("31.A") is not None
    assert len(registry.all()) == 1
    assert "Could not import plugin module" in caplog.text
    assert "governance_engine.plugins.does_not_exist" in caplog.text


def test_re_register_replaces_not_duplicates() -> None:
    registry = PluginRegistry()
    original = _Phase31APlugin()
    replacement = _Phase31AReplacementPlugin()

    registry.register(original)
    registry.register(replacement)

    assert registry.get("31.A") is replacement
    assert registry.all() == [replacement]
