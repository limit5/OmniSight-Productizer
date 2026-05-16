"""PhasePlugin contract — base class + Protocol for per-phase governance rules.

G.B ships 11 concrete per-phase plugins (one per S12 phase 31.A-K) that
enforce phase-specific filing-time rules. G.A-v1-5 ships only the contract:
the runtime-checkable :class:`PhasePlugin` Protocol, the :class:`PluginError`
dataclass, and a no-op :class:`BasePhasePlugin` that G.B subclasses extend.

A plugin declares four class attributes — ``phase_id`` (``^31\\.[A-K]$``),
``plugin_version`` (locks the plugin to its schema generation; v1 plugins
pin to ``"v1"``), ``schema_versions_supported`` (which
``TicketContract.schema_version`` values the plugin accepts; v1 plugins
default to ``frozenset({"v0", "v1"})``), and ``ruleset_id`` (stable audit
identifier matching ``^phase-31[a-k]-rules-v[0-9]+$``) — plus two methods:
``validate(ticket) -> list[PluginError]`` (empty list iff OK) and
``applies_to(ticket) -> bool`` (dispatches on labels + schema version).

Example subclass::

    class Phase31EPlugin(BasePhasePlugin):
        phase_id = "31.E"

        def validate(self, ticket: TicketContractV1) -> list[PluginError]:
            if ticket.phase_plugin_version != "v1":
                return [PluginError(
                    phase_id=self.phase_id,
                    rule_id="31e-plugin-version-required",
                    severity="error",
                    detail="31.E tickets must declare phase_plugin_version='v1'",
                )]
            return []
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal, Protocol, runtime_checkable

from governance_engine.schema.v1 import TicketContractV1


@dataclass(frozen=True)
class PluginError:
    phase_id: str
    rule_id: str
    severity: Literal["error", "warning"]
    detail: str


@runtime_checkable
class PhasePlugin(Protocol):
    phase_id: str
    plugin_version: Literal["v1"]
    schema_versions_supported: frozenset[str]
    ruleset_id: str

    def validate(self, ticket: TicketContractV1) -> list[PluginError]: ...

    def applies_to(self, ticket: TicketContractV1) -> bool: ...


class BasePhasePlugin:
    """No-op concrete base for G.B subclasses.

    Subclasses MUST set :attr:`phase_id` (matching ``^31\\.[A-K]$``) and
    override :meth:`validate`. :attr:`ruleset_id` is a read-only property
    rather than a class attribute because its derivation references ``self``;
    a subclass that bumps its ruleset to v2+ may override the property.
    """

    phase_id: ClassVar[str] = ""
    plugin_version: ClassVar[Literal["v1"]] = "v1"
    schema_versions_supported: ClassVar[frozenset[str]] = frozenset({"v0", "v1"})

    @property
    def ruleset_id(self) -> str:
        return f"phase-{self.phase_id.lower().replace('.', '')}-rules-v1"

    def validate(self, ticket: TicketContractV1) -> list[PluginError]:
        return []

    def applies_to(self, ticket: TicketContractV1) -> bool:
        return (
            f"phase:{self.phase_id}" in ticket.labels
            and ticket.schema_version in self.schema_versions_supported
        )


__all__ = ["PhasePlugin", "PluginError", "BasePhasePlugin"]
