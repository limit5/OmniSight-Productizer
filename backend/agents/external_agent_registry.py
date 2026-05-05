"""BP.A2A.6 -- external A2A agent endpoint registry.

Operator-registered remote agents are outbound A2A peers, not external
tools. The registry records the endpoint binding OmniSight should use
later when BP.A2A.7 adds graph nodes that invoke those peers.

Module-global state audit (SOP Step 1): this module defines types and
classes only. Mutable registry state lives in the injected store. The
included in-memory store is intentionally per worker for dev/tests; a
PG-backed store should be wired when a durable Alembic table is added.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from urllib.parse import urljoin, urlparse

from backend.a2a.client import A2AClient


ExternalAgentAuthMode = Literal["none", "bearer", "oauth2"]
ExternalAgentHealthStatus = Literal[
    "unknown",
    "healthy",
    "degraded",
    "unreachable",
]

DEFAULT_AGENT_CARD_PATH = "/.well-known/agent.json"


class ExternalAgentRegistryError(RuntimeError):
    """Base error for external A2A agent registry failures."""


class ExternalAgentNotFoundError(ExternalAgentRegistryError):
    """Raised when an operator references an unknown external agent."""


class ExternalAgentDisabledError(ExternalAgentRegistryError):
    """Raised when an operator-disabled external agent is requested."""


@dataclass(frozen=True)
class ExternalAgentEndpoint:
    """One operator-registered outbound A2A peer endpoint."""

    agent_id: str
    display_name: str
    base_url: str
    agent_name: str
    description: str = ""
    auth_mode: ExternalAgentAuthMode = "none"
    token_ref: str = ""
    enabled: bool = True
    tags: tuple[str, ...] = field(default_factory=tuple)
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    health_status: ExternalAgentHealthStatus = "unknown"
    last_health_check: datetime | None = None
    registered_at: datetime | None = None
    updated_at: datetime | None = None
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _slug("agent_id", self.agent_id))
        object.__setattr__(self, "agent_name", _slug("agent_name", self.agent_name))
        object.__setattr__(self, "display_name", _required("display_name", self.display_name))
        object.__setattr__(self, "base_url", _normalise_base_url(self.base_url))
        object.__setattr__(
            self,
            "tags",
            tuple(sorted({t.strip() for t in self.tags if t.strip()})),
        )
        object.__setattr__(
            self,
            "capabilities",
            tuple(sorted({c.strip() for c in self.capabilities if c.strip()})),
        )
        if self.auth_mode in ("bearer", "oauth2") and not self.token_ref.strip():
            raise ValueError("token_ref is required when auth_mode uses a bearer token")
        if self.auth_mode == "none" and self.token_ref.strip():
            raise ValueError("token_ref must be empty when auth_mode='none'")

    @property
    def agent_card_url(self) -> str:
        return urljoin(self.base_url + "/", DEFAULT_AGENT_CARD_PATH.lstrip("/"))


class ExternalAgentRegistryStore(Protocol):
    async def upsert_endpoint(self, endpoint: ExternalAgentEndpoint) -> None: ...
    async def get_endpoint(self, agent_id: str) -> ExternalAgentEndpoint | None: ...
    async def list_endpoints(
        self, *, enabled_only: bool = False
    ) -> list[ExternalAgentEndpoint]: ...
    async def set_health(
        self,
        agent_id: str,
        status: ExternalAgentHealthStatus,
        checked_at: datetime,
    ) -> None: ...


class InMemoryExternalAgentRegistryStore:
    """Dev/test store. Production durable storage should implement the Protocol."""

    def __init__(self) -> None:
        self._endpoints: dict[str, ExternalAgentEndpoint] = {}

    async def upsert_endpoint(self, endpoint: ExternalAgentEndpoint) -> None:
        self._endpoints[endpoint.agent_id] = endpoint

    async def get_endpoint(self, agent_id: str) -> ExternalAgentEndpoint | None:
        return self._endpoints.get(agent_id)

    async def list_endpoints(
        self, *, enabled_only: bool = False
    ) -> list[ExternalAgentEndpoint]:
        rows = list(self._endpoints.values())
        if enabled_only:
            rows = [row for row in rows if row.enabled]
        return sorted(rows, key=lambda row: row.agent_id)

    async def set_health(
        self,
        agent_id: str,
        status: ExternalAgentHealthStatus,
        checked_at: datetime,
    ) -> None:
        endpoint = self._endpoints.get(agent_id)
        if endpoint is None:
            return
        self._endpoints[agent_id] = replace(
            endpoint,
            health_status=status,
            last_health_check=checked_at,
            updated_at=checked_at,
        )


class ExternalAgentRegistry:
    """Operator-facing registry for remote A2A agent endpoint bindings."""

    def __init__(self, store: ExternalAgentRegistryStore | None = None) -> None:
        self.store = store or InMemoryExternalAgentRegistryStore()

    async def register_endpoint(
        self,
        endpoint: ExternalAgentEndpoint,
    ) -> ExternalAgentEndpoint:
        now = datetime.now(timezone.utc)
        existing = await self.store.get_endpoint(endpoint.agent_id)
        registered_at = existing.registered_at if existing else endpoint.registered_at
        if registered_at is None:
            registered_at = now
        endpoint = replace(endpoint, registered_at=registered_at, updated_at=now)
        await self.store.upsert_endpoint(endpoint)
        return endpoint

    async def get_endpoint(
        self,
        agent_id: str,
        *,
        require_enabled: bool = False,
    ) -> ExternalAgentEndpoint:
        endpoint = await self.store.get_endpoint(_slug("agent_id", agent_id))
        if endpoint is None:
            raise ExternalAgentNotFoundError(f"external agent not registered: {agent_id}")
        if require_enabled and not endpoint.enabled:
            raise ExternalAgentDisabledError(
                f"external agent {agent_id!r} is disabled (operator kill-switch)"
            )
        return endpoint

    async def list_endpoints(
        self, *, enabled_only: bool = False
    ) -> list[ExternalAgentEndpoint]:
        return await self.store.list_endpoints(enabled_only=enabled_only)

    async def set_enabled(
        self,
        agent_id: str,
        enabled: bool,
    ) -> ExternalAgentEndpoint:
        endpoint = await self.get_endpoint(agent_id)
        updated = replace(
            endpoint,
            enabled=enabled,
            updated_at=datetime.now(timezone.utc),
        )
        await self.store.upsert_endpoint(updated)
        return updated

    async def set_health(
        self,
        agent_id: str,
        status: ExternalAgentHealthStatus,
        checked_at: datetime | None = None,
    ) -> None:
        await self.store.set_health(
            _slug("agent_id", agent_id),
            status,
            checked_at or datetime.now(timezone.utc),
        )

    async def build_client(
        self,
        agent_id: str,
        *,
        tenant_id: str,
        bearer_token: str = "",
    ) -> A2AClient:
        endpoint = await self.get_endpoint(agent_id, require_enabled=True)
        return A2AClient(
            endpoint.base_url,
            tenant_id=tenant_id,
            bearer_token=bearer_token,
        )


def _required(field_name: str, value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    return cleaned


def _slug(field_name: str, value: str) -> str:
    cleaned = _required(field_name, value)
    allowed = cleaned.replace("-", "").replace("_", "")
    if cleaned != cleaned.lower() or not allowed.isalnum():
        raise ValueError(f"{field_name} must be a lowercase slug")
    return cleaned


def _normalise_base_url(value: str) -> str:
    cleaned = _required("base_url", value).rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("base_url must be an absolute http(s) URL")
    return cleaned
