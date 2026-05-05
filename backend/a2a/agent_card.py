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

Module-global state audit (SOP Step 1): static descriptor seeds are
immutable tuples, frozensets, or ``MappingProxyType`` views. The only
mutable module-global is the BP.A2A.10 model-mapping mtime cache; every
uvicorn worker derives the same AgentCard from the same shared
``configs/model_mapping.yaml`` file and reloads when that file's mtime
changes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator
import yaml

from backend.sandbox_tier import Guild, SandboxTier, admitted_tiers

logger = logging.getLogger(__name__)


SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
A2A_PROTOCOL_VERSION: Literal["0.3.0"] = "0.3.0"
DEFAULT_DISCOVERY_PATH = "/.well-known/agent.json"
DEFAULT_INVOKE_PATH_TEMPLATE = "/a2a/invoke/{agent_name}"
DEFAULT_STREAM_PATH_TEMPLATE = "/a2a/invoke/{agent_name}?stream=true"
PROVIDER_DISCOVERY_PATH_TEMPLATE = "/.well-known/a2a/providers/{provider_id}/agent.json"
PROVIDER_INVOKE_PATH_TEMPLATE = "/a2a/providers/{provider_id}/invoke/{agent_name}"
PROVIDER_STREAM_PATH_TEMPLATE = "/a2a/providers/{provider_id}/invoke/{agent_name}?stream=true"
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
A2A_PROVIDER_IDS: tuple[str, ...] = (
    "anthropic",
    "openai",
    "google",
    "xai",
    "groq",
    "deepseek",
    "together",
    "openrouter",
    "ollama",
)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MODEL_MAPPING_PATH = _PROJECT_ROOT / "configs" / "model_mapping.yaml"
_MODEL_MAPPING_CACHE: tuple[float | None, dict[str, str], dict[str, str]] | None = None


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

_GRAPH_SPECIALIST_DESCRIPTORS = (
    {
        "agent_name": "firmware",
        "display_name": "Firmware",
        "description": "Firmware implementation, driver bring-up, cross-compilation, and embedded integration specialist.",
        "admitted_tiers": (SandboxTier.T1.value, SandboxTier.T3.value),
        "tags": ("langgraph", "firmware", "specialist"),
    },
    {
        "agent_name": "software",
        "display_name": "Software",
        "description": "Application code, backend/frontend implementation, algorithm, and refactor specialist.",
        "admitted_tiers": (SandboxTier.T1.value, SandboxTier.T2.value),
        "tags": ("langgraph", "software", "specialist"),
    },
    {
        "agent_name": "validator",
        "display_name": "Validator",
        "description": "Validation, regression test, benchmark, lint, and verification specialist.",
        "admitted_tiers": (SandboxTier.T0.value, SandboxTier.T1.value),
        "tags": ("langgraph", "validator", "specialist"),
    },
    {
        "agent_name": "reviewer",
        "display_name": "Reviewer",
        "description": "Code review, patchset analysis, Gerrit review, and implementation risk specialist.",
        "admitted_tiers": (SandboxTier.T0.value, SandboxTier.T1.value),
        "tags": ("langgraph", "reviewer", "specialist"),
    },
    {
        "agent_name": "general",
        "display_name": "General",
        "description": "Fallback specialist for tasks that do not map cleanly to a narrower domain.",
        "admitted_tiers": (SandboxTier.T0.value,),
        "tags": ("langgraph", "general", "specialist"),
    },
)

_PROVIDER_DISPLAY_NAMES = MappingProxyType({
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "xai": "xAI",
    "groq": "Groq",
    "deepseek": "DeepSeek",
    "together": "Together",
    "openrouter": "OpenRouter",
    "ollama": "Ollama",
})

_FALLBACK_PROVIDER_DEFAULT_MODELS = MappingProxyType({
    "anthropic": "claude-sonnet-4-20250514",
    "openai": "gpt-4o",
    "google": "gemini-1.5-pro",
    "xai": "grok-3-mini",
    "groq": "llama-3.3-70b-versatile",
    "deepseek": "deepseek-chat",
    "together": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
    "openrouter": "anthropic/claude-sonnet-4",
    "ollama": "llama3.1",
})

_FALLBACK_SPECIALIST_MODEL_SPECS = MappingProxyType({
    Guild.architect.value: "anthropic:claude-opus-4-20250514",
    Guild.sa_sd.value: "anthropic:claude-sonnet-4-20250514",
    Guild.ux.value: "google:gemini-1.5-pro",
    Guild.pm.value: "anthropic:claude-sonnet-4-20250514",
    Guild.gateway.value: "anthropic:claude-haiku-4-20250506",
    Guild.bsp.value: "anthropic:claude-sonnet-4-20250514",
    Guild.hal.value: "anthropic:claude-sonnet-4-20250514",
    Guild.algo_cv.value: "anthropic:claude-opus-4-20250514",
    Guild.optical.value: "anthropic:claude-sonnet-4-20250514",
    Guild.isp.value: "anthropic:claude-sonnet-4-20250514",
    Guild.audio.value: "anthropic:claude-sonnet-4-20250514",
    Guild.frontend.value: "anthropic:claude-sonnet-4-20250514",
    Guild.backend.value: "anthropic:claude-sonnet-4-20250514",
    Guild.sre.value: "anthropic:claude-sonnet-4-20250514",
    Guild.qa.value: "anthropic:claude-sonnet-4-20250514",
    Guild.auditor.value: "anthropic:claude-opus-4-20250514",
    Guild.red_team.value: "xai:grok-3-mini",
    Guild.forensics.value: "google:gemini-1.5-pro",
    Guild.intel.value: "google:gemini-1.5-pro",
    Guild.reporter.value: "anthropic:claude-haiku-4-20250506",
    Guild.custom.value: "anthropic:claude-sonnet-4-20250514",
    "orchestrator": "anthropic:claude-sonnet-4-20250514",
    "hd": "anthropic:claude-sonnet-4-20250514",
    "firmware": "anthropic:claude-sonnet-4-20250514",
    "software": "anthropic:claude-sonnet-4-20250514",
    "validator": "openai:gpt-4o",
    "reviewer": "anthropic:claude-sonnet-4-20250514",
    "general": "anthropic:claude-sonnet-4-20250514",
})


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
    source: Literal["guild", "runtime_specialist", "domain_specialist", "provider_specialist"]
    endpoint_url: str = Field(..., min_length=1)
    stream_endpoint_url: str = Field(..., min_length=1)
    admitted_tiers: tuple[str, ...] = Field(default_factory=tuple)
    input_modes: tuple[str, ...] = DEFAULT_INPUT_MODES
    output_modes: tuple[str, ...] = DEFAULT_OUTPUT_MODES
    tags: tuple[str, ...] = Field(default_factory=tuple)
    provider_id: str | None = None
    model_spec: str | None = None

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


class SpecialistA2AEndpoint(BaseModel):
    """Provider-scoped specialist endpoint visible to orchestrator routing."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    provider_id: str = Field(..., min_length=1)
    agent_name: str = Field(..., min_length=1)
    endpoint_url: str = Field(..., min_length=1)
    stream_endpoint_url: str = Field(..., min_length=1)
    model_spec: str = Field(..., min_length=1)
    protocol: Literal["a2a"] = "a2a"


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


def _provider_endpoint_url(
    public_base_url: str,
    path_template: str,
    provider_id: str,
    agent_name: str = "",
) -> str:
    provider_slug = quote(provider_id, safe="")
    agent_slug = quote(agent_name, safe="")
    return public_base_url + path_template.format(
        provider_id=provider_slug,
        agent_name=agent_slug,
    )


def _require_provider_id(provider_id: str) -> str:
    provider = (provider_id or "").strip().lower()
    if provider not in A2A_PROVIDER_IDS:
        raise ValueError(f"unknown A2A provider {provider_id!r}")
    return provider


def _coerce_model_spec(raw: Any) -> str | None:
    if isinstance(raw, str):
        spec = raw.strip()
    elif isinstance(raw, dict):
        if isinstance(raw.get("model_spec"), str):
            spec = raw["model_spec"].strip()
        elif isinstance(raw.get("provider"), str) and isinstance(raw.get("model"), str):
            spec = f"{raw['provider'].strip()}:{raw['model'].strip()}"
        else:
            return None
    else:
        return None
    if ":" not in spec:
        return None
    provider, model = spec.split(":", 1)
    if provider.strip().lower() not in A2A_PROVIDER_IDS or not model.strip():
        return None
    return f"{provider.strip().lower()}:{model.strip()}"


def _parse_model_mapping(raw: Any) -> tuple[dict[str, str], dict[str, str]]:
    provider_defaults = dict(_FALLBACK_PROVIDER_DEFAULT_MODELS)
    specialist_specs = dict(_FALLBACK_SPECIALIST_MODEL_SPECS)
    if not isinstance(raw, dict):
        return provider_defaults, specialist_specs

    providers = raw.get("providers")
    if isinstance(providers, dict):
        for provider_id, provider_cfg in providers.items():
            provider = str(provider_id).strip().lower()
            if provider not in A2A_PROVIDER_IDS or not isinstance(provider_cfg, dict):
                continue
            model = provider_cfg.get("default_model")
            if isinstance(model, str) and model.strip():
                provider_defaults[provider] = model.strip()

    for section in ("guilds", "runtime_specialists", "graph_specialists"):
        entries = raw.get(section)
        if not isinstance(entries, dict):
            continue
        for agent_name, cfg in entries.items():
            agent = str(agent_name).strip().lower()
            if not agent:
                continue
            spec = _coerce_model_spec(cfg)
            if spec:
                specialist_specs[agent] = spec
    return provider_defaults, specialist_specs


def _load_model_mapping() -> tuple[dict[str, str], dict[str, str]]:
    """Load BP.F sample model mapping with file-mtime cache invalidation."""

    global _MODEL_MAPPING_CACHE
    try:
        mtime = _MODEL_MAPPING_PATH.stat().st_mtime
    except OSError:
        mtime = None
    if _MODEL_MAPPING_CACHE is not None and _MODEL_MAPPING_CACHE[0] == mtime:
        return _MODEL_MAPPING_CACHE[1], _MODEL_MAPPING_CACHE[2]
    try:
        if mtime is not None:
            parsed = yaml.safe_load(_MODEL_MAPPING_PATH.read_text(encoding="utf-8")) or {}
            provider_defaults, specialist_specs = _parse_model_mapping(parsed)
            _MODEL_MAPPING_CACHE = (mtime, provider_defaults, specialist_specs)
            return provider_defaults, specialist_specs
    except Exception as exc:  # noqa: BLE001 - AgentCard generation must stay available.
        logger.warning("model_mapping.yaml load failed: %s; using built-in A2A sample", exc)
    provider_defaults = dict(_FALLBACK_PROVIDER_DEFAULT_MODELS)
    specialist_specs = dict(_FALLBACK_SPECIALIST_MODEL_SPECS)
    _MODEL_MAPPING_CACHE = (mtime, provider_defaults, specialist_specs)
    return provider_defaults, specialist_specs


def reload_model_mapping_for_tests() -> None:
    """Clear this worker's AgentCard model-mapping cache."""

    global _MODEL_MAPPING_CACHE
    _MODEL_MAPPING_CACHE = None


def _specialist_model_spec(agent_name: str) -> str:
    _, specialist_specs = _load_model_mapping()
    agent = (agent_name or "").strip().lower()
    return specialist_specs.get(agent, specialist_specs["general"])


def _provider_model_spec(provider_id: str, agent_name: str) -> str:
    provider_defaults, specialist_specs = _load_model_mapping()
    provider = _require_provider_id(provider_id)
    configured = specialist_specs.get((agent_name or "").strip().lower(), "")
    prefix = f"{provider}:"
    if configured.startswith(prefix):
        return configured
    return f"{provider}:{provider_defaults[provider]}"


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
        model_spec=_specialist_model_spec(name),
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
            model_spec=_specialist_model_spec(str(seed["agent_name"])),
        )
        for seed in _EXTRA_SPECIALIST_DESCRIPTORS
    )
    return guild_descriptors + extra_descriptors


def build_provider_capability_descriptors(
    public_base_url: str,
    provider_id: str,
) -> tuple[CapabilityDescriptor, ...]:
    """Generate provider-scoped specialist descriptors for BP.A2A.9."""

    base = _normalise_public_base_url(public_base_url)
    provider = _require_provider_id(provider_id)
    base_descriptors = list(build_capability_descriptors(base))
    known = {descriptor.agent_name for descriptor in base_descriptors}
    base_descriptors.extend(
        CapabilityDescriptor(
            agent_name=str(seed["agent_name"]),
            display_name=str(seed["display_name"]),
            description=str(seed["description"]),
            source="runtime_specialist",
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
            model_spec=_specialist_model_spec(str(seed["agent_name"])),
        )
        for seed in _GRAPH_SPECIALIST_DESCRIPTORS
        if seed["agent_name"] not in known
    )
    return tuple(
        CapabilityDescriptor(
            agent_name=descriptor.agent_name,
            display_name=f"{_PROVIDER_DISPLAY_NAMES[provider]} {descriptor.display_name}",
            description=(
                f"{descriptor.description} Invoked through the "
                f"{_PROVIDER_DISPLAY_NAMES[provider]} A2A provider endpoint."
            ),
            source="provider_specialist",
            endpoint_url=_provider_endpoint_url(
                base,
                PROVIDER_INVOKE_PATH_TEMPLATE,
                provider,
                descriptor.agent_name,
            ),
            stream_endpoint_url=_provider_endpoint_url(
                base,
                PROVIDER_STREAM_PATH_TEMPLATE,
                provider,
                descriptor.agent_name,
            ),
            admitted_tiers=descriptor.admitted_tiers,
            input_modes=descriptor.input_modes,
            output_modes=descriptor.output_modes,
            tags=("provider", provider, *descriptor.tags),
            provider_id=provider,
            model_spec=_provider_model_spec(provider, descriptor.agent_name),
        )
        for descriptor in base_descriptors
    )


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


def build_provider_agent_card(public_base_url: str, provider_id: str) -> AgentCard:
    """Build one provider-scoped A2A AgentCard for specialist routing."""

    base = _normalise_public_base_url(public_base_url)
    provider = _require_provider_id(provider_id)
    discovery_path = PROVIDER_DISCOVERY_PATH_TEMPLATE.format(
        provider_id=quote(provider, safe=""),
    )
    display = _PROVIDER_DISPLAY_NAMES[provider]
    return AgentCard(
        name=f"OmniSight {display} Specialist Agents",
        description=(
            "Provider-scoped OmniSight specialist AgentCard. Internal "
            "orchestrators route through these A2A endpoints instead of "
            "binding to provider SDK classes."
        ),
        provider=display,
        url=base + discovery_path,
        endpoints=AgentCardEndpoints(
            discovery_url=base + discovery_path,
            invoke_url_template=base + PROVIDER_INVOKE_PATH_TEMPLATE.format(
                provider_id=quote(provider, safe=""),
                agent_name="{agent_name}",
            ),
            stream_url_template=base + PROVIDER_STREAM_PATH_TEMPLATE.format(
                provider_id=quote(provider, safe=""),
                agent_name="{agent_name}",
            ),
        ),
        capabilities=build_provider_capability_descriptors(base, provider),
    )


def build_provider_agent_cards(public_base_url: str) -> tuple[AgentCard, ...]:
    """Build AgentCards for all nine supported LLM providers."""

    return tuple(
        build_provider_agent_card(public_base_url, provider)
        for provider in A2A_PROVIDER_IDS
    )


def resolve_specialist_a2a_endpoint(
    public_base_url: str,
    *,
    provider_id: str,
    agent_name: str,
) -> SpecialistA2AEndpoint:
    """Return the only provider/specialist handle the orchestrator needs."""

    base = _normalise_public_base_url(public_base_url)
    provider = _require_provider_id(provider_id)
    agent = (agent_name or "").strip().lower()
    known = {
        descriptor.agent_name
        for descriptor in build_provider_capability_descriptors(base, provider)
    }
    if agent not in known:
        raise ValueError(f"unknown A2A specialist {agent_name!r}")
    return SpecialistA2AEndpoint(
        provider_id=provider,
        agent_name=agent,
        endpoint_url=_provider_endpoint_url(
            base,
            PROVIDER_INVOKE_PATH_TEMPLATE,
            provider,
            agent,
        ),
        stream_endpoint_url=_provider_endpoint_url(
            base,
            PROVIDER_STREAM_PATH_TEMPLATE,
            provider,
            agent,
        ),
        model_spec=_provider_model_spec(provider, agent),
    )


__all__ = [
    "A2A_PROTOCOL_VERSION",
    "A2A_PROVIDER_IDS",
    "DEFAULT_A2A_SCOPES",
    "DEFAULT_DISCOVERY_PATH",
    "DEFAULT_INVOKE_PATH_TEMPLATE",
    "DEFAULT_STREAM_PATH_TEMPLATE",
    "PROVIDER_DISCOVERY_PATH_TEMPLATE",
    "PROVIDER_INVOKE_PATH_TEMPLATE",
    "PROVIDER_STREAM_PATH_TEMPLATE",
    "SCHEMA_VERSION",
    "AgentCard",
    "AgentCardAuth",
    "AgentCardEndpoints",
    "AgentCardProtocolCapabilities",
    "AgentCardStreaming",
    "CapabilityDescriptor",
    "SpecialistA2AEndpoint",
    "build_agent_card",
    "build_capability_descriptors",
    "build_provider_agent_card",
    "build_provider_agent_cards",
    "build_provider_capability_descriptors",
    "reload_model_mapping_for_tests",
    "resolve_specialist_a2a_endpoint",
]
