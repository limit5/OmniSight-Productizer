"""RPG.W2.1 -- Guild registry imported from BP.B + agent-class mapping.

ADR-0008 makes RPG a consumer of two existing sources of truth:

* ``backend.sandbox_tier.Guild`` owns the Guild enum values.
* ``config/agent_class_schema.yaml`` owns the ``agent_class`` labels.

This module keeps the RPG-side registry deliberately small: immutable
metadata for every Guild plus a conservative ``agent_class`` -> Guild
eligibility table that later routing, character-card, and party code can
read without copying BP.B values.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import FrozenSet, Mapping

from backend.agents import routing_policy
from backend.sandbox_tier import Guild

SECONDARY_GUILD_UNLOCK_LEVEL = 50


@dataclass(frozen=True)
class GuildDefinition:
    """RPG-facing metadata for one canonical Guild."""

    guild: Guild
    display_name: str
    summary: str


@dataclass(frozen=True)
class SecondaryGuildChoice:
    """Validated Lv 50 secondary-Guild selection for one agent class."""

    agent_class: str
    primary_guild: Guild
    secondary_guild: Guild
    level: int


@dataclass(frozen=True)
class GuildModelPreference:
    """RPG-facing import of BP.F's model mapping for one Guild."""

    guild: Guild
    model_spec: str
    provider_family: str


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


GUILDS: FrozenSet[str] = frozenset(guild.value for guild in GUILD_DEFINITIONS)


AGENT_CLASS_GUILD_MATRIX: Mapping[str, FrozenSet[Guild]] = MappingProxyType(
    {
        "subscription-codex": frozenset(
            {
                Guild.frontend,
                Guild.qa,
                Guild.reporter,
                Guild.custom,
            }
        ),
        "subscription-claude": frozenset(
            {
                Guild.architect,
                Guild.sa_sd,
                Guild.pm,
                Guild.gateway,
                Guild.backend,
                Guild.sre,
                Guild.auditor,
            }
        ),
        "api-anthropic": frozenset(
            {
                Guild.architect,
                Guild.sa_sd,
                Guild.pm,
                Guild.gateway,
                Guild.bsp,
                Guild.hal,
                Guild.algo_cv,
                Guild.optical,
                Guild.isp,
                Guild.audio,
                Guild.frontend,
                Guild.backend,
                Guild.sre,
                Guild.qa,
                Guild.auditor,
                Guild.reporter,
                Guild.custom,
            }
        ),
        "api-openai": frozenset(
            {
                Guild.frontend,
                Guild.backend,
                Guild.qa,
                Guild.reporter,
                Guild.custom,
            }
        ),
        "local-llm-qwen": frozenset(
            {
                Guild.qa,
                Guild.reporter,
                Guild.custom,
            }
        ),
        "unassigned": frozenset(),
    }
)


class UnknownAgentClassError(KeyError):
    """Raised when an agent_class is absent from the RPG Guild matrix."""


class UnknownGuildModelMappingError(KeyError):
    """Raised when BP.F has no model mapping for a Guild."""


class UnroutableGuildModelMappingError(ValueError):
    """Raised when BP.F names a provider family MP routing does not consume."""


class SecondaryGuildLockedError(ValueError):
    """Raised when an agent has not reached the secondary-Guild unlock."""


class SecondaryGuildChoiceError(ValueError):
    """Raised when a secondary Guild selection is invalid for the agent."""


def get_guild_definition(guild: Guild) -> GuildDefinition:
    """Return RPG-facing metadata for ``guild``."""

    return GUILD_DEFINITIONS[guild]


def list_guild_definitions() -> tuple[GuildDefinition, ...]:
    """Return all Guild definitions in canonical enum order."""

    return tuple(GUILD_DEFINITIONS[guild] for guild in Guild)


def eligible_guilds_for_agent_class(agent_class: str) -> FrozenSet[Guild]:
    """Return the Guilds an ``agent_class`` may specialize into."""

    key = agent_class.strip()
    try:
        return AGENT_CLASS_GUILD_MATRIX[key]
    except KeyError as exc:
        raise UnknownAgentClassError(
            f"No RPG Guild mapping for agent_class {key!r}"
        ) from exc


def agent_class_supports_guild(agent_class: str, guild: Guild) -> bool:
    """Whether ``agent_class`` may specialize into ``guild``."""

    return guild in eligible_guilds_for_agent_class(agent_class)


def model_preference_for_guild(guild: Guild) -> GuildModelPreference:
    """Return BP.F's model preference for ``guild`` using MP routing labels."""

    guild_specs, _ = routing_policy._load_model_routing_matrix()
    model_spec = guild_specs.get(guild.value)
    if model_spec is None:
        raise UnknownGuildModelMappingError(
            f"No BP.F model mapping for Guild {guild.value!r}"
        )

    provider = routing_policy._provider_from_model_spec(model_spec)
    provider_family = (
        routing_policy._adr_vendor_label(provider) if provider is not None else None
    )
    if (
        provider_family is None
        or provider_family not in routing_policy.ROUTING_POLICY_CONSUMED_PROVIDER_LABELS
    ):
        raise UnroutableGuildModelMappingError(
            "BP.F model mapping for Guild "
            f"{guild.value!r} uses provider {provider!r}, but MP routing "
            "does not consume that provider label"
        )

    return GuildModelPreference(
        guild=guild,
        model_spec=model_spec,
        provider_family=provider_family,
    )


def list_guild_model_preferences() -> tuple[GuildModelPreference, ...]:
    """Return BP.F model preferences for every Guild in canonical enum order."""

    return tuple(model_preference_for_guild(guild) for guild in Guild)


def secondary_guilds_for_agent_class(
    agent_class: str,
    primary_guild: Guild | str,
    level: int,
) -> FrozenSet[Guild]:
    """Return Lv 50 secondary-Guild choices for an agent class.

    The primary Guild is excluded so W18 multi-classing always adds a second
    specialization rather than re-selecting the current one.
    """

    _validate_level(level)
    if level < SECONDARY_GUILD_UNLOCK_LEVEL:
        return frozenset()

    primary = _coerce_guild(primary_guild, field="primary_guild")
    return frozenset(
        guild
        for guild in eligible_guilds_for_agent_class(agent_class)
        if guild != primary
    )


def choose_secondary_guild(
    agent_class: str,
    primary_guild: Guild | str,
    secondary_guild: Guild | str,
    level: int,
) -> SecondaryGuildChoice:
    """Validate and return an agent's Lv 50 secondary-Guild choice."""

    _validate_level(level)
    if level < SECONDARY_GUILD_UNLOCK_LEVEL:
        raise SecondaryGuildLockedError(
            "secondary Guild unlock requires level "
            f"{SECONDARY_GUILD_UNLOCK_LEVEL}; got level {level}"
        )

    primary = _coerce_guild(primary_guild, field="primary_guild")
    secondary = _coerce_guild(secondary_guild, field="secondary_guild")
    if secondary == primary:
        raise SecondaryGuildChoiceError(
            "secondary Guild must be different from primary Guild"
        )

    choices = secondary_guilds_for_agent_class(agent_class, primary, level)
    if secondary not in choices:
        allowed = ", ".join(sorted(guild.value for guild in choices)) or "none"
        raise SecondaryGuildChoiceError(
            f"secondary Guild {secondary.value!r} is not eligible for "
            f"agent_class {agent_class.strip()!r}; allowed: {allowed}"
        )

    return SecondaryGuildChoice(
        agent_class=agent_class.strip(),
        primary_guild=primary,
        secondary_guild=secondary,
        level=level,
    )


def list_agent_class_guild_mappings() -> tuple[tuple[str, tuple[Guild, ...]], ...]:
    """Return stable ``agent_class`` -> Guild rows for UI/API consumers."""

    rows: list[tuple[str, tuple[Guild, ...]]] = []
    for agent_class in sorted(AGENT_CLASS_GUILD_MATRIX):
        guilds = tuple(
            sorted(AGENT_CLASS_GUILD_MATRIX[agent_class], key=lambda g: g.value)
        )
        rows.append((agent_class, guilds))
    return tuple(rows)


def _assert_registry_complete() -> None:
    missing = [guild for guild in Guild if guild not in GUILD_DEFINITIONS]
    if missing:
        missing_names = sorted(guild.value for guild in missing)
        raise RuntimeError(
            "RPG Guild registry is incomplete; missing definitions for "
            f"Guild members: {missing_names}"
        )
    extra = GUILDS - frozenset(guild.value for guild in Guild)
    if extra:
        raise RuntimeError(
            "RPG Guild registry declares unknown Guild slugs: "
            f"{sorted(extra)}"
        )


def _assert_matrix_values_are_guilds() -> None:
    for agent_class, guilds in AGENT_CLASS_GUILD_MATRIX.items():
        for guild in guilds:
            if not isinstance(guild, Guild):
                raise RuntimeError(
                    f"RPG Guild matrix entry for {agent_class!r} contains "
                    f"non-Guild value: {guild!r}"
                )


def _coerce_guild(value: Guild | str, *, field: str) -> Guild:
    if isinstance(value, Guild):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a Guild or string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} is required")
    try:
        return Guild(clean)
    except ValueError as exc:
        raise ValueError(f"unknown {field}: {clean!r}") from exc


def _validate_level(level: int) -> None:
    if isinstance(level, bool) or not isinstance(level, int):
        raise TypeError("level must be an int")
    if level < 1:
        raise ValueError("level must be >= 1")


_assert_registry_complete()
_assert_matrix_values_are_guilds()


__all__ = [
    "AGENT_CLASS_GUILD_MATRIX",
    "GUILDS",
    "GUILD_DEFINITIONS",
    "SECONDARY_GUILD_UNLOCK_LEVEL",
    "GuildDefinition",
    "SecondaryGuildChoice",
    "SecondaryGuildChoiceError",
    "SecondaryGuildLockedError",
    "UnknownAgentClassError",
    "agent_class_supports_guild",
    "choose_secondary_guild",
    "eligible_guilds_for_agent_class",
    "get_guild_definition",
    "list_agent_class_guild_mappings",
    "list_guild_definitions",
    "secondary_guilds_for_agent_class",
]
