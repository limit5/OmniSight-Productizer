"""RPG.W2.1 -- Guild registry imported from BP.B + agent-class mapping.

ADR-0008 makes RPG a consumer of two existing sources of truth:

* ``backend.sandbox_tier.Guild`` owns the Guild enum values.
* ``config/agent_class_schema.yaml`` owns the ``agent_class`` labels.

This module keeps the RPG-side registry deliberately small: immutable
metadata for every Guild plus a conservative ``agent_class`` -> Guild
eligibility table that later routing, character-card, and party code can
read without copying BP.B values.

Public surface
--------------
* :class:`GuildDefinition`, :data:`GUILD_DEFINITIONS`, :data:`GUILDS`,
  :func:`get_guild_definition`, :func:`list_guild_definitions` --
  immutable per-Guild RPG metadata keyed off the BP.B enum.
* :class:`GuildModelPreference`, :func:`model_preference_for_guild`,
  :func:`list_guild_model_preferences` -- RPG-facing view of BP.F's
  per-Guild model mapping, validated against the MP routing provider
  matrix in :mod:`backend.agents.routing_policy`.
* :data:`AGENT_CLASS_GUILD_MATRIX`,
  :func:`eligible_guilds_for_agent_class`,
  :func:`agent_class_supports_guild`,
  :func:`list_agent_class_guild_mappings` -- the conservative
  ``agent_class`` -> Guild eligibility table.
* :data:`SECONDARY_GUILD_UNLOCK_LEVEL`,
  :class:`SecondaryGuildChoice`,
  :func:`secondary_guilds_for_agent_class`,
  :func:`choose_secondary_guild` -- W18 secondary-Guild (multi-class)
  selection gated at Lv 50.

All registries are exposed as ``MappingProxyType`` views so callers
cannot mutate them, and the module asserts its registry-complete and
matrix-shape invariants at import time.
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
    """RPG-facing metadata for one canonical Guild.

    Attributes:
        guild: Canonical Guild enum member owned by
            :mod:`backend.sandbox_tier` (BP.B).
        display_name: Short title-case label suitable for UI/API output.
        summary: One-sentence description of the Guild's domain of work.
    """

    guild: Guild
    display_name: str
    summary: str


@dataclass(frozen=True)
class SecondaryGuildChoice:
    """Validated Lv 50 secondary-Guild selection for one agent class.

    Returned by :func:`choose_secondary_guild` once the ``agent_class``,
    ``primary_guild``, ``secondary_guild`` and ``level`` inputs have all
    been cross-checked against :data:`AGENT_CLASS_GUILD_MATRIX` and the
    :data:`SECONDARY_GUILD_UNLOCK_LEVEL` gate.

    Attributes:
        agent_class: Whitespace-stripped agent class slug.
        primary_guild: Eligible primary Guild for that agent class.
        secondary_guild: Newly chosen secondary Guild; distinct from
            ``primary_guild`` and present in the agent class eligibility
            set.
        level: Caller-supplied agent level at the time of the choice
            (always ``>= SECONDARY_GUILD_UNLOCK_LEVEL``).
    """

    agent_class: str
    primary_guild: Guild
    secondary_guild: Guild
    level: int


@dataclass(frozen=True)
class GuildModelPreference:
    """RPG-facing import of BP.F's model mapping for one Guild.

    Produced by :func:`model_preference_for_guild`. Carries the
    ``model_spec`` verbatim plus the ADR-0007 vendor label so callers
    can index into MP routing tables without re-parsing the raw spec.

    Attributes:
        guild: Guild this preference applies to.
        model_spec: Verbatim ``provider:model`` string from BP.F's
            ``configs/model_mapping.yaml``.
        provider_family: ADR-0007 vendor label (e.g. ``anthropic``,
            ``openai``) derived from ``model_spec`` and confirmed to be
            in
            :data:`backend.agents.routing_policy.ROUTING_POLICY_CONSUMED_PROVIDER_LABELS`.
    """

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
    """Return RPG-facing metadata for ``guild``.

    Raises:
        KeyError: ``guild`` is missing from :data:`GUILD_DEFINITIONS`.
            The registry-complete invariant asserted at import time
            means this only fires when a new ``Guild`` enum member is
            added without a matching :class:`GuildDefinition`.
    """

    return GUILD_DEFINITIONS[guild]


def list_guild_definitions() -> tuple[GuildDefinition, ...]:
    """Return all Guild definitions in canonical enum (BP.B) order.

    Order matches ``Guild`` enum iteration so UI/API consumers can rely
    on a stable presentation order without sorting client-side.
    """

    return tuple(GUILD_DEFINITIONS[guild] for guild in Guild)


def eligible_guilds_for_agent_class(agent_class: str) -> FrozenSet[Guild]:
    """Return the Guilds an ``agent_class`` may specialize into.

    Lookup is whitespace-tolerant: leading/trailing whitespace on
    ``agent_class`` is stripped before matching the registry.

    Raises:
        UnknownAgentClassError: stripped ``agent_class`` is not a key in
            :data:`AGENT_CLASS_GUILD_MATRIX`.
    """

    key = agent_class.strip()
    try:
        return AGENT_CLASS_GUILD_MATRIX[key]
    except KeyError as exc:
        raise UnknownAgentClassError(
            f"No RPG Guild mapping for agent_class {key!r}"
        ) from exc


def agent_class_supports_guild(agent_class: str, guild: Guild) -> bool:
    """Whether ``agent_class`` may specialize into ``guild``.

    Raises:
        UnknownAgentClassError: ``agent_class`` is not a key in
            :data:`AGENT_CLASS_GUILD_MATRIX` (propagated from
            :func:`eligible_guilds_for_agent_class`).
    """

    return guild in eligible_guilds_for_agent_class(agent_class)


def model_preference_for_guild(guild: Guild) -> GuildModelPreference:
    """Return BP.F's model preference for ``guild`` using MP routing labels.

    Reads the per-Guild ``model_spec`` from BP.F's routing matrix and
    resolves its provider into the ADR-0007 vendor label, so the result
    is directly usable as a key into MP routing tables.

    Raises:
        UnknownGuildModelMappingError: BP.F has no entry for ``guild``.
        UnroutableGuildModelMappingError: BP.F's mapping for ``guild``
            resolves to a provider that MP routing does not consume
            (i.e. not in
            :data:`backend.agents.routing_policy.ROUTING_POLICY_CONSUMED_PROVIDER_LABELS`).
    """

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
    """Return BP.F model preferences for every Guild in canonical enum order.

    Fail-fast over the entire BP.B Guild enum: surfaces any drift where
    BP.F is missing a mapping or names an unroutable provider.

    Raises:
        UnknownGuildModelMappingError: BP.F is missing a mapping for any
            Guild in the BP.B enum.
        UnroutableGuildModelMappingError: BP.F names a provider for some
            Guild that MP routing does not consume.
    """

    return tuple(model_preference_for_guild(guild) for guild in Guild)


def secondary_guilds_for_agent_class(
    agent_class: str,
    primary_guild: Guild | str,
    level: int,
) -> FrozenSet[Guild]:
    """Return Lv 50 secondary-Guild choices for an agent class.

    The primary Guild is excluded so W18 multi-classing always adds a second
    specialization rather than re-selecting the current one. Returns an
    empty set when ``level`` is below :data:`SECONDARY_GUILD_UNLOCK_LEVEL`
    so callers can call unconditionally and treat "not unlocked yet" the
    same as "unlocked but no other Guilds available".

    Args:
        agent_class: Agent class slug; whitespace is stripped.
        primary_guild: Current primary Guild for the agent. Accepts
            either a :class:`~backend.sandbox_tier.Guild` member or its
            slug string.
        level: Agent's current level. Must be a non-bool ``int >= 1``.

    Raises:
        TypeError: ``level`` is not an ``int`` (booleans are rejected),
            or ``primary_guild`` is neither a ``Guild`` nor a string.
        ValueError: ``level`` is ``< 1``, or ``primary_guild`` is an
            empty/unknown slug.
        UnknownAgentClassError: ``agent_class`` is not a key in
            :data:`AGENT_CLASS_GUILD_MATRIX`.
        SecondaryGuildChoiceError: ``primary_guild`` is not in the
            eligible set for ``agent_class``.
    """

    _validate_level(level)
    if level < SECONDARY_GUILD_UNLOCK_LEVEL:
        return frozenset()

    primary = _coerce_guild(primary_guild, field="primary_guild")
    eligible = eligible_guilds_for_agent_class(agent_class)
    _assert_primary_guild_eligible(agent_class, primary, eligible)
    return frozenset(
        guild
        for guild in eligible
        if guild != primary
    )


def choose_secondary_guild(
    agent_class: str,
    primary_guild: Guild | str,
    secondary_guild: Guild | str,
    level: int,
) -> SecondaryGuildChoice:
    """Validate and return an agent's Lv 50 secondary-Guild choice.

    Cross-checks the level gate, both Guild coercions, and the
    ``agent_class`` eligibility matrix before returning an immutable
    :class:`SecondaryGuildChoice` record.

    Args:
        agent_class: Agent class slug; whitespace is stripped in the
            returned record.
        primary_guild: Current primary Guild. ``Guild`` member or slug.
        secondary_guild: Newly chosen secondary Guild. ``Guild`` member
            or slug.
        level: Agent's current level. Must be a non-bool ``int >= 1``.

    Raises:
        TypeError: ``level`` is not an ``int``, or either Guild argument
            is neither a ``Guild`` nor a string.
        ValueError: ``level`` is ``< 1``, or a Guild argument is empty
            or an unknown slug.
        SecondaryGuildLockedError: ``level`` is below
            :data:`SECONDARY_GUILD_UNLOCK_LEVEL`.
        UnknownAgentClassError: ``agent_class`` is not a key in
            :data:`AGENT_CLASS_GUILD_MATRIX`.
        SecondaryGuildChoiceError: ``primary_guild`` is not eligible for
            ``agent_class``, ``secondary_guild`` equals ``primary_guild``,
            or ``secondary_guild`` is not in the eligible set.
    """

    _validate_level(level)
    if level < SECONDARY_GUILD_UNLOCK_LEVEL:
        raise SecondaryGuildLockedError(
            "secondary Guild unlock requires level "
            f"{SECONDARY_GUILD_UNLOCK_LEVEL}; got level {level}"
        )

    primary = _coerce_guild(primary_guild, field="primary_guild")
    _assert_primary_guild_eligible(
        agent_class,
        primary,
        eligible_guilds_for_agent_class(agent_class),
    )
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
    """Return stable ``agent_class`` -> Guild rows for UI/API consumers.

    Each row is ``(agent_class, tuple[Guild, ...])``. Both the outer
    rows (by agent_class slug) and the inner Guilds (by Guild slug) are
    sorted so the result is deterministic and independent of dict
    insertion order.
    """

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


def _assert_primary_guild_eligible(
    agent_class: str,
    primary_guild: Guild,
    eligible_guilds: FrozenSet[Guild],
) -> None:
    if primary_guild in eligible_guilds:
        return
    allowed = ", ".join(sorted(guild.value for guild in eligible_guilds)) or "none"
    raise SecondaryGuildChoiceError(
        f"primary Guild {primary_guild.value!r} is not eligible for "
        f"agent_class {agent_class.strip()!r}; allowed: {allowed}"
    )


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
    "GuildModelPreference",
    "SecondaryGuildChoice",
    "SecondaryGuildChoiceError",
    "SecondaryGuildLockedError",
    "UnknownAgentClassError",
    "UnknownGuildModelMappingError",
    "UnroutableGuildModelMappingError",
    "agent_class_supports_guild",
    "choose_secondary_guild",
    "eligible_guilds_for_agent_class",
    "get_guild_definition",
    "list_agent_class_guild_mappings",
    "list_guild_definitions",
    "list_guild_model_preferences",
    "model_preference_for_guild",
    "secondary_guilds_for_agent_class",
]
