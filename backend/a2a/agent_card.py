"""BP.A2A.1 — OmniSight inbound A2A AgentCard schema.

This module is intentionally declarative. BP.A2A.2 owns the FastAPI
``/.well-known/agent.json`` and ``/a2a/invoke/{agent_name}`` routes;
BP.A2A.1 only defines the JSON-serialisable contract those routes will
publish.

The shape mirrors the project-local Pydantic template modules: frozen
value objects, strict extra-field rejection, and helper functions that
derive public URLs from an operator/request supplied base URL. The
specialist capability list is generated from the existing
``backend.sandbox_tier.Guild`` taxonomy, with OmniSight-only runtime
specialists such as ``orchestrator`` and ``hd`` appended as explicit
domain descriptors.

Module-global state audit (SOP Step 1): all module-level values are
immutable tuples, frozensets, or ``MappingProxyType`` views. No cache,
singleton, env read, DB read, or request state lives here; every uvicorn
worker derives the same AgentCard from the same code plus the request's
public base URL.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.sandbox_tier import Guild, SandboxTier, admitted_tiers


SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
A2A_PROTOCOL_VERSION: Literal["0.3.0"] = "0.3.0"
DEFAULT_DISCOVERY_PATH = "/.well-known/agent.json"
DEFAULT_INVOKE_PATH_TEMPLATE = "/a2a/invoke/{agent_name}"
DEFAULT_STREAM_PATH_TEMPLATE = "/a2a/invoke/{agent_name}?stream=true"
DEFAULT_INPUT_MODES: tuple[str, ...] = ("text/plain", "application/json")
DEFAULT_OUTPUT_MODES: tuple[str, ...] = (
    "text/plain",
    "application/json",
    "text/event-stream",
)
DEFAULT_A2A_SCOPES: tuple[str, ...] = ("a2a:discover:*", "a2a:invoke:*")
DEFAULT_STREAM_EVENTS: tuple[str, ...] = (
    "task_submitted",
    "task_working",
    "artifact_delta",
    "task_completed",
    "task_failed",
)


_GUILD_DISPLAY_NAMES = MappingProxyType({
    Guild.architect: "Architect",
    Guild.sa_sd: "SA-SD",
    Guild.ux: "UX",
    Guild.pm: "PM",
    Guild.gateway: "Gateway",
    Guild.bsp: "BSP",
    Guild.hal: "HAL",
    Guild.algo_cv: "Algo-CV",
    Guild.optical: "Optical",
    Guild.isp: "ISP",
    Guild.audio: "Audio",
    Guild.frontend: "Frontend",
    Guild.backend: "Backend",
    Guild.sre: "SRE",
    Guild.qa: "QA",
    Guild.auditor: "Auditor",
    Guild.red_team: "RedTeam",
    Guild.forensics: "Forensics",
    Guild.intel: "Intel",
    Guild.reporter: "Reporter",
    Guild.custom: "Custom",
})

_GUILD_DESCRIPTIONS = MappingProxyType({
    Guild.architect: "System design, ADR, blueprint, and cross-guild architecture review.",
    Guild.sa_sd: "Software architecture and detailed design hand-off specialist.",
    Guild.ux: "UX research, interaction design, and workflow ergonomics specialist.",
    Guild.pm: "Requirements grooming, scope slicing, and sprint-planning specialist.",
    Guild.gateway: "Gateway, A2A/MCP edge, orchestration ingress, and traffic-shaping specialist.",
    Guild.bsp: "Board support package work for kernel, U-Boot, device tree, and Yocto layers.",
    Guild.hal: "Hardware abstraction layer work for vendor SDK glue, drivers, and peripheral stubs.",
    Guild.algo_cv: "Computer-vision algorithm implementation, simulation, and benchmarking.",
    Guild.optical: "Optics, lens, IR-cut, and imaging hardware advisory work.",
    Guild.isp: "Image signal processor tuning, 3A pipeline, and sensor bring-up support.",
    Guild.audio: "Audio DSP, acoustic echo cancellation, noise reduction, and capture pipeline work.",
    Guild.frontend: "Frontend UI implementation for web application surfaces.",
    Guild.backend: "Backend Python, FastAPI, Alembic, Postgres, and service integration work.",
    Guild.sre: "Deployment, observability, incident response, and operational readiness work.",
    Guild.qa: "Test planning, contract tests, E2E validation, and regression coverage.",
    Guild.auditor: "Read-only audit-chain and compliance evidence observer.",
    Guild.red_team: "Adversarial testing, security probes, and prompt-injection exercises.",
    Guild.forensics: "Post-incident root cause, log archaeology, and evidence preservation.",
    Guild.intel: "SecOps threat intelligence, CVE feed triage, and external signal monitoring.",
    Guild.reporter: "Human-facing reports, changelogs, release notes, and summary artifacts.",
    Guild.custom: "Operator-defined specialist slot with conservative default policy.",
})

_EXTRA_SPECIALIST_DESCRIPTORS = (
    {
        "agent_name": "orchestrator",
        "display_name": "Orchestrator",
        "description": "OmniSight control-plane router for task intake, specialist selection, and result synthesis.",
        "source": "runtime_specialist",
        "admitted_tiers": (SandboxTier.T0.value,),
        "tags": ("routing", "langgraph", "control-plane"),
    },
    {
        "agent_name": "hd",
        "display_name": "HD",
        "description": "High-density PCB, SI/PI, bring-up, and board-level hardware design specialist.",
        "source": "domain_specialist",
        "admitted_tiers": (SandboxTier.T1.value, SandboxTier.T3.value),
        "tags": ("pcb", "si-pi", "hardware"),
    },
)


class AgentCardEndpoints(BaseModel):
    """Public A2A endpoint URLs for discovery and invocation."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    discovery_url: str = Field(..., min_length=1)
    invoke_url_template: str = Field(..., min_length=1)
    stream_url_template: str = Field(..., min_length=1)


class AgentCardAuth(BaseModel):
    """Authentication contract for OmniSight's inbound A2A surface."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    scheme: Literal["oauth2", "bearer"] = "oauth2"
    description: str = "PEP gateway OAuth bearer token required."
    scopes: tuple[str, ...] = DEFAULT_A2A_SCOPES


class AgentCardStreaming(BaseModel):
    """Streaming transport contract for A2A invocation."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    supported: bool = True
    transport: Literal["sse"] = "sse"
    content_type: Literal["text/event-stream"] = "text/event-stream"
    events: tuple[str, ...] = DEFAULT_STREAM_EVENTS


class AgentCardProtocolCapabilities(BaseModel):
    """Protocol-level capabilities advertised by the A2A server."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    streaming: bool = True
    push_notifications: bool = False
    state_transition_history: bool = True


class CapabilityDescriptor(BaseModel):
    """One callable OmniSight specialist capability."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    agent_name: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    source: Literal["guild", "runtime_specialist", "domain_specialist"]
    endpoint_url: str = Field(..., min_length=1)
    stream_endpoint_url: str = Field(..., min_length=1)
    admitted_tiers: tuple[str, ...] = Field(default_factory=tuple)
    input_modes: tuple[str, ...] = DEFAULT_INPUT_MODES
    output_modes: tuple[str, ...] = DEFAULT_OUTPUT_MODES
    tags: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("agent_name")
    @classmethod
    def _agent_name_is_slug(cls, value: str) -> str:
        if value != value.lower() or not value.replace("_", "").replace("-", "").isalnum():
            raise ValueError("agent_name must be a lowercase slug")
        return value


class AgentCard(BaseModel):
    """OmniSight A2A AgentCard published to external systems."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    protocol: Literal["a2a"] = "a2a"
    protocol_version: Literal["0.3.0"] = A2A_PROTOCOL_VERSION
    name: str = "OmniSight Productizer"
    description: str = (
        "OmniSight multi-agent productization service exposing specialist "
        "guilds through the Agent-to-Agent protocol."
    )
    version: str = "1.0.0"
    url: str = Field(..., min_length=1)
    provider: str = "OmniSight"
    endpoints: AgentCardEndpoints
    auth: AgentCardAuth = Field(default_factory=AgentCardAuth)
    streaming: AgentCardStreaming = Field(default_factory=AgentCardStreaming)
    protocol_capabilities: AgentCardProtocolCapabilities = Field(
        default_factory=AgentCardProtocolCapabilities
    )
    capabilities: tuple[CapabilityDescriptor, ...] = Field(min_length=1)
    default_input_modes: tuple[str, ...] = DEFAULT_INPUT_MODES
    default_output_modes: tuple[str, ...] = DEFAULT_OUTPUT_MODES


def _normalise_public_base_url(public_base_url: str) -> str:
    base = (public_base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("public_base_url is required")
    if not (base.startswith("https://") or base.startswith("http://")):
        raise ValueError("public_base_url must start with http:// or https://")
    return base


def _endpoint_url(public_base_url: str, path_template: str, agent_name: str) -> str:
    agent_slug = quote(agent_name, safe="")
    return public_base_url + path_template.format(agent_name=agent_slug)


def _guild_capability(public_base_url: str, guild: Guild) -> CapabilityDescriptor:
    tiers = tuple(sorted(t.value for t in admitted_tiers(guild)))
    name = guild.value
    return CapabilityDescriptor(
        agent_name=name,
        display_name=_GUILD_DISPLAY_NAMES[guild],
        description=_GUILD_DESCRIPTIONS[guild],
        source="guild",
        endpoint_url=_endpoint_url(public_base_url, DEFAULT_INVOKE_PATH_TEMPLATE, name),
        stream_endpoint_url=_endpoint_url(public_base_url, DEFAULT_STREAM_PATH_TEMPLATE, name),
        admitted_tiers=tiers,
        tags=("guild", *tiers),
    )


def build_capability_descriptors(public_base_url: str) -> tuple[CapabilityDescriptor, ...]:
    """Generate OmniSight specialist descriptors with public endpoint URLs."""

    base = _normalise_public_base_url(public_base_url)
    guild_descriptors = tuple(_guild_capability(base, guild) for guild in Guild)
    extra_descriptors = tuple(
        CapabilityDescriptor(
            agent_name=str(seed["agent_name"]),
            display_name=str(seed["display_name"]),
            description=str(seed["description"]),
            source=seed["source"],  # type: ignore[arg-type]
            endpoint_url=_endpoint_url(
                base,
                DEFAULT_INVOKE_PATH_TEMPLATE,
                str(seed["agent_name"]),
            ),
            stream_endpoint_url=_endpoint_url(
                base,
                DEFAULT_STREAM_PATH_TEMPLATE,
                str(seed["agent_name"]),
            ),
            admitted_tiers=tuple(seed["admitted_tiers"]),
            tags=tuple(seed["tags"]),
        )
        for seed in _EXTRA_SPECIALIST_DESCRIPTORS
    )
    return guild_descriptors + extra_descriptors


def build_agent_card(public_base_url: str) -> AgentCard:
    """Build the public OmniSight AgentCard for BP.A2A.2 discovery."""

    base = _normalise_public_base_url(public_base_url)
    return AgentCard(
        url=base + DEFAULT_DISCOVERY_PATH,
        endpoints=AgentCardEndpoints(
            discovery_url=base + DEFAULT_DISCOVERY_PATH,
            invoke_url_template=base + DEFAULT_INVOKE_PATH_TEMPLATE,
            stream_url_template=base + DEFAULT_STREAM_PATH_TEMPLATE,
        ),
        capabilities=build_capability_descriptors(base),
    )


__all__ = [
    "A2A_PROTOCOL_VERSION",
    "DEFAULT_A2A_SCOPES",
    "DEFAULT_DISCOVERY_PATH",
    "DEFAULT_INVOKE_PATH_TEMPLATE",
    "DEFAULT_STREAM_PATH_TEMPLATE",
    "SCHEMA_VERSION",
    "AgentCard",
    "AgentCardAuth",
    "AgentCardEndpoints",
    "AgentCardProtocolCapabilities",
    "AgentCardStreaming",
    "CapabilityDescriptor",
    "build_agent_card",
    "build_capability_descriptors",
]
