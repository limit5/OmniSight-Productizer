"""BP.B.2 -- canonical Guild definitions.

This module owns the declarative metadata for the 21-Guild taxonomy.
The enum slugs remain owned by :mod:`backend.sandbox_tier`; this package
adds the human-facing labels and concise domain summaries that downstream
agent registries, routing views, and UI payloads consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from backend.sandbox_tier import Guild


@dataclass(frozen=True)
class GuildDefinition:
    """Immutable metadata for one canonical Guild.

    Attributes:
        guild: Canonical Guild enum member owned by
            :mod:`backend.sandbox_tier`.
        display_name: Short title-case label suitable for UI/API output.
        summary: One-sentence description of the Guild's domain of work.
    """

    guild: Guild
    display_name: str
    summary: str


GUILD_DEFINITIONS: Mapping[Guild, GuildDefinition] = MappingProxyType(
    {
        Guild.architect: GuildDefinition(
            guild=Guild.architect,
            display_name="Architect",
            summary="System design, ADR, blueprint, and cross-guild architecture review.",
        ),
        Guild.sa_sd: GuildDefinition(
            guild=Guild.sa_sd,
            display_name="SA-SD",
            summary="Software architecture and detailed design hand-off specialist.",
        ),
        Guild.ux: GuildDefinition(
            guild=Guild.ux,
            display_name="UX",
            summary="UX research, wireframes, and interaction design.",
        ),
        Guild.pm: GuildDefinition(
            guild=Guild.pm,
            display_name="PM",
            summary="Requirements grooming, scope slicing, and sprint planning.",
        ),
        Guild.gateway: GuildDefinition(
            guild=Guild.gateway,
            display_name="Gateway",
            summary="Orchestrator gateway, A2A/MCP edge, and traffic shaping.",
        ),
        Guild.bsp: GuildDefinition(
            guild=Guild.bsp,
            display_name="BSP",
            summary="Board support package work for kernel, U-Boot, and device tree.",
        ),
        Guild.hal: GuildDefinition(
            guild=Guild.hal,
            display_name="HAL",
            summary="Hardware abstraction layer work for SDK glue and drivers.",
        ),
        Guild.algo_cv: GuildDefinition(
            guild=Guild.algo_cv,
            display_name="Algo-CV",
            summary="Computer-vision algorithm implementation and benchmarking.",
        ),
        Guild.optical: GuildDefinition(
            guild=Guild.optical,
            display_name="Optical",
            summary="Optics, lens, IR-cut, and imaging hardware advisory work.",
        ),
        Guild.isp: GuildDefinition(
            guild=Guild.isp,
            display_name="ISP",
            summary="Image signal processor tuning and sensor bring-up support.",
        ),
        Guild.audio: GuildDefinition(
            guild=Guild.audio,
            display_name="Audio",
            summary="Audio DSP, acoustic echo cancellation, and capture pipeline work.",
        ),
        Guild.frontend: GuildDefinition(
            guild=Guild.frontend,
            display_name="Frontend",
            summary="Frontend UI implementation for web application surfaces.",
        ),
        Guild.backend: GuildDefinition(
            guild=Guild.backend,
            display_name="Backend",
            summary="Backend Python, FastAPI, Alembic, Postgres, and service integration.",
        ),
        Guild.sre: GuildDefinition(
            guild=Guild.sre,
            display_name="SRE",
            summary="Deployment, observability, incident response, and operational readiness.",
        ),
        Guild.qa: GuildDefinition(
            guild=Guild.qa,
            display_name="QA",
            summary="Test planning, contract tests, E2E validation, and regression coverage.",
        ),
        Guild.auditor: GuildDefinition(
            guild=Guild.auditor,
            display_name="Auditor",
            summary="Read-only audit-chain and compliance evidence observer.",
        ),
        Guild.red_team: GuildDefinition(
            guild=Guild.red_team,
            display_name="RedTeam",
            summary="Adversarial testing, security probes, and prompt-injection exercises.",
        ),
        Guild.forensics: GuildDefinition(
            guild=Guild.forensics,
            display_name="Forensics",
            summary="Post-incident root cause, log archaeology, and evidence preservation.",
        ),
        Guild.intel: GuildDefinition(
            guild=Guild.intel,
            display_name="Intel",
            summary="SecOps threat intelligence, CVE feed triage, and external signals.",
        ),
        Guild.reporter: GuildDefinition(
            guild=Guild.reporter,
            display_name="Reporter",
            summary="Human-facing reports, changelogs, release notes, and summaries.",
        ),
        Guild.custom: GuildDefinition(
            guild=Guild.custom,
            display_name="Custom",
            summary="Operator-defined specialist slot with conservative defaults.",
        ),
    }
)


def get_guild_definition(guild: Guild) -> GuildDefinition:
    """Return metadata for ``guild``."""

    return GUILD_DEFINITIONS[guild]


def list_guild_definitions() -> tuple[GuildDefinition, ...]:
    """Return all Guild definitions in canonical enum order."""

    return tuple(GUILD_DEFINITIONS[guild] for guild in Guild)


def _assert_registry_complete() -> None:
    missing = [guild for guild in Guild if guild not in GUILD_DEFINITIONS]
    if missing:
        raise RuntimeError(
            "Guild definition registry is incomplete; missing Guild members: "
            f"{sorted(guild.value for guild in missing)}"
        )

    extra = frozenset(GUILD_DEFINITIONS) - frozenset(Guild)
    if extra:
        raise RuntimeError(
            "Guild definition registry declares unknown Guild members: "
            f"{sorted(str(guild) for guild in extra)}"
        )


_assert_registry_complete()


__all__ = [
    "GUILD_DEFINITIONS",
    "GuildDefinition",
    "get_guild_definition",
    "list_guild_definitions",
]
