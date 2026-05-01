"""AB.5.6 — Remote MCP server integration (Figma / Gmail / Calendar / Drive).

Anthropic Messages API exposes a top-level ``mcp_servers=[]`` parameter
that auto-injects MCP server tool definitions into the request. The
SDK handles tool discovery, dispatch, and result routing — OmniSight
just declares which servers the caller has access to.

Four claude.ai-managed MCPs ship as defaults:

  * ``claude_ai_Figma``           — design context, code connect, FigJam
  * ``claude_ai_Gmail``           — message read / send (auth via OAuth)
  * ``claude_ai_Google_Calendar`` — events / availability
  * ``claude_ai_Google_Drive``    — file ops, search

Auth tokens are per-operator: they're captured during the operator's
existing claude.ai OAuth flow and stored encrypted via AS Token Vault
(handled outside this module — caller passes the token already-decrypted).

Out of scope (defer to dedicated batch when first MCP-using customer
ships):

  * OAuth flow for Figma / Google services — uses existing
    ``backend/security/oauth_client.py`` AS.1.x infrastructure when
    wired up, just adds the MCP-specific scopes
  * Per-tenant token storage layer — current MCPRegistry is in-memory
    only; PG-backed impl arrives with the multi-tenant gate
  * Dynamic tool schema discovery — Anthropic SDK does this at request
    time, no need to mirror in our tool_schemas.py registry

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §5.6
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ─── Server config + registry ────────────────────────────────────


@dataclass(frozen=True)
class MCPServerConfig:
    """One remote MCP server the operator has access to.

    ``name`` matches the prefix that Anthropic uses in tool_use blocks.
    Tool names from this server arrive as ``mcp__<name>__<method>``.
    """

    name: str
    """Unique server identifier (matches ``mcp__<name>__*`` prefix)."""

    url: str
    """SSE / streaming endpoint URL."""

    authorization_token: str | None = None
    """Per-operator OAuth token. Stored encrypted via AS Token Vault;
    caller passes the decrypted value here. None for public MCPs."""

    description: str = ""

    enabled: bool = True

    def __repr__(self) -> str:
        # Never print the token.
        token_repr = "<redacted>" if self.authorization_token else None
        return (
            f"MCPServerConfig(name={self.name!r}, url={self.url!r}, "
            f"authorization_token={token_repr}, enabled={self.enabled})"
        )

    def to_anthropic_payload(self) -> dict[str, Any]:
        """Serialize for Anthropic Messages API ``mcp_servers=[]`` slot.

        Anthropic SDK shape:
            {
              "type": "url",
              "url": "...",
              "name": "...",
              "authorization_token": "..."  (optional)
            }
        """
        payload: dict[str, Any] = {
            "type": "url",
            "url": self.url,
            "name": self.name,
        }
        if self.authorization_token:
            payload["authorization_token"] = self.authorization_token
        return payload


# ─── Default catalog (the 4 claude.ai-managed MCPs) ──────────────


@dataclass(frozen=True)
class _CatalogEntry:
    """Static metadata for a known remote MCP server."""

    name: str
    default_url: str
    description: str
    sample_tools: tuple[str, ...]
    """Representative tool names this MCP server exposes — for
    documentation only. Actual tool list comes from the server at
    request time."""


DEFAULT_REMOTE_MCP_CATALOG: tuple[_CatalogEntry, ...] = (
    _CatalogEntry(
        name="claude_ai_Figma",
        default_url="https://mcp.anthropic.com/v1/integrations/figma",
        description=(
            "Figma official MCP. Read designs (get_design_context, "
            "get_screenshot, get_metadata, get_figjam), Code Connect mapping "
            "(add_code_connect_map, get_code_connect_suggestions), "
            "design system (search_design_system, create_design_system_rules), "
            "diagram creation in FigJam (generate_diagram). "
            "URL parsing: figma.com/design/<fileKey>/<...>?node-id=<nodeId>."
        ),
        sample_tools=(
            "get_design_context", "get_screenshot", "get_metadata",
            "generate_diagram", "search_design_system",
            "add_code_connect_map", "get_libraries", "whoami",
        ),
    ),
    _CatalogEntry(
        name="claude_ai_Gmail",
        default_url="https://mcp.anthropic.com/v1/integrations/gmail",
        description=(
            "Gmail integration. authenticate / complete_authentication "
            "primitives surface today; message read/send arrive when the "
            "operator completes Google OAuth."
        ),
        sample_tools=("authenticate", "complete_authentication"),
    ),
    _CatalogEntry(
        name="claude_ai_Google_Calendar",
        default_url="https://mcp.anthropic.com/v1/integrations/google_calendar",
        description=(
            "Google Calendar integration. authenticate / "
            "complete_authentication primitives; event read/write arrive "
            "post-OAuth."
        ),
        sample_tools=("authenticate", "complete_authentication"),
    ),
    _CatalogEntry(
        name="claude_ai_Google_Drive",
        default_url="https://mcp.anthropic.com/v1/integrations/google_drive",
        description=(
            "Google Drive integration. authenticate / "
            "complete_authentication primitives; file operations arrive "
            "post-OAuth."
        ),
        sample_tools=("authenticate", "complete_authentication"),
    ),
)


def default_catalog_by_name() -> dict[str, _CatalogEntry]:
    return {entry.name: entry for entry in DEFAULT_REMOTE_MCP_CATALOG}


def build_default_server_config(
    name: str,
    *,
    url_override: str | None = None,
    authorization_token: str | None = None,
    enabled: bool = True,
) -> MCPServerConfig:
    """Build an MCPServerConfig from the static catalog by name.

    Operator typically calls this after completing OAuth to wire the
    captured token into the runtime registry. ``url_override`` lets
    air-gapped customers point at their own MCP gateway.
    """
    catalog = default_catalog_by_name()
    if name not in catalog:
        raise KeyError(
            f"Unknown remote MCP server {name!r}. Known: {sorted(catalog)}"
        )
    entry = catalog[name]
    return MCPServerConfig(
        name=entry.name,
        url=url_override or entry.default_url,
        authorization_token=authorization_token,
        description=entry.description,
        enabled=enabled,
    )


# ─── Runtime registry ────────────────────────────────────────────


class RemoteMCPRegistry:
    """Per-operator / per-tenant runtime MCP server registry.

    In-memory v1; PG-backed impl arrives when multi-tenant ships
    (same pattern as AB.3-7 stores).
    """

    def __init__(self, configs: list[MCPServerConfig] | None = None) -> None:
        self._servers: dict[str, MCPServerConfig] = {}
        for cfg in configs or []:
            self._servers[cfg.name] = cfg

    def add(self, config: MCPServerConfig) -> None:
        """Register or replace a server. Replacement is idempotent so
        operator can re-run the OAuth flow to refresh tokens."""
        self._servers[config.name] = config

    def remove(self, name: str) -> bool:
        return self._servers.pop(name, None) is not None

    def get(self, name: str) -> MCPServerConfig | None:
        return self._servers.get(name)

    def list_all(self, *, enabled_only: bool = False) -> list[MCPServerConfig]:
        items = list(self._servers.values())
        if enabled_only:
            items = [s for s in items if s.enabled]
        return sorted(items, key=lambda s: s.name)

    def to_anthropic_mcp_servers(
        self, *, only_names: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Build the Anthropic ``mcp_servers=[]`` payload.

        Defaults to all enabled servers; pass ``only_names`` to scope a
        request to a subset (e.g., only Figma for a design-review task).
        Disabled servers are silently filtered.
        """
        out: list[dict[str, Any]] = []
        for cfg in self._servers.values():
            if not cfg.enabled:
                continue
            if only_names is not None and cfg.name not in only_names:
                continue
            out.append(cfg.to_anthropic_payload())
        # Stable order: deterministic for tests + log diff
        out.sort(key=lambda d: d.get("name", ""))
        return out

    def configured_names(self) -> list[str]:
        return sorted(self._servers)

    def __len__(self) -> int:
        return len(self._servers)


# ─── Tool name parsing (mcp__<server>__<method>) ─────────────────


def parse_mcp_tool_name(tool_name: str) -> tuple[str, str] | None:
    """Split a `mcp__<server>__<method>` tool name into (server, method).

    Returns None if the name doesn't match the MCP convention so callers
    can fall back to non-MCP dispatch.
    """
    if not tool_name.startswith("mcp__"):
        return None
    body = tool_name[len("mcp__"):]
    sep = body.find("__")
    if sep <= 0:
        return None
    return (body[:sep], body[sep + 2:])


def is_mcp_tool(tool_name: str) -> bool:
    return parse_mcp_tool_name(tool_name) is not None
