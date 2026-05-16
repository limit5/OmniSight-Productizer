"""Cross-phase preflight: phase_plugin_version compatibility.

For each v1 ticket, the registry must hold a plugin for the ticket's
``phase:<id>`` label whose ``plugin_version`` matches and whose
``schema_versions_supported`` admits the ticket's ``schema_version``.
``BasePhasePlugin`` attributes are V1-5-owned; only consumed here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from governance_engine.plugins.base import PhasePlugin
from governance_engine.schema.v1 import TicketContractV1

_PHASE_LABEL_PREFIX = "phase:"


@dataclass(frozen=True)
class PreflightError:
    ticket_key: str
    expected_plugin_version: str
    actual_plugin_version: str | None
    detail: str
    rule_id: str = "plugin-version-mismatch"


@dataclass(frozen=True)
class PluginRegistry:
    plugins: Mapping[str, PhasePlugin] = field(default_factory=dict)

    def get(self, phase_id: str) -> PhasePlugin | None:
        return self.plugins.get(phase_id)


def _phase_id_from_labels(labels: list[str]) -> str | None:
    return next(
        (lbl[len(_PHASE_LABEL_PREFIX):] for lbl in labels
         if lbl.startswith(_PHASE_LABEL_PREFIX)),
        None,
    )


def _mk(ticket_key: str, actual: str | None, detail: str) -> PreflightError:
    return PreflightError(ticket_key, "v1", actual, detail)


def check_plugin_version_compatibility(
    roster: list[TicketContractV1],
    registry: PluginRegistry,
) -> list[PreflightError]:
    errors: list[PreflightError] = []
    for ticket in roster:
        if ticket.phase_plugin_version != "v1":
            continue
        phase_id = _phase_id_from_labels(ticket.labels)
        if phase_id is None:
            errors.append(_mk(ticket.ticket_key, None,
                "ticket has no 'phase:<id>' label; cannot dispatch"))
            continue
        plugin = registry.get(phase_id)
        if plugin is None:
            errors.append(_mk(ticket.ticket_key, None,
                f"no plugin registered for phase '{phase_id}'"))
            continue
        if plugin.plugin_version != "v1":
            errors.append(_mk(ticket.ticket_key, plugin.plugin_version,
                f"phase '{phase_id}' plugin is at version "
                f"'{plugin.plugin_version}', ticket requires 'v1'"))
            continue
        if ticket.schema_version not in plugin.schema_versions_supported:
            errors.append(_mk(ticket.ticket_key, plugin.plugin_version,
                f"phase '{phase_id}' plugin (v1) does not accept "
                f"schema_version '{ticket.schema_version}'; supported: "
                f"{sorted(plugin.schema_versions_supported)}"))
    return errors


__all__ = ["check_plugin_version_compatibility", "PluginRegistry", "PreflightError"]
