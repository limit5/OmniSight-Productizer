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
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Friendly env-var alias → catalog entry name. Used by
# :func:`build_registry_from_env` so operators don't have to spell out the
# ``claude_ai_`` prefix in their .env. Both runner (auto-runner-sdk.py)
# and backend specialist agents read tokens through this map, so adding a
# new MCP server requires a single line here.
ENV_TOKEN_VAR_BY_NAME: dict[str, str] = {
    "claude_ai_Figma":            "OMNISIGHT_MCP_FIGMA_TOKEN",
    "claude_ai_Gmail":            "OMNISIGHT_MCP_GMAIL_TOKEN",
    "claude_ai_Google_Calendar":  "OMNISIGHT_MCP_GOOGLE_CALENDAR_TOKEN",
    "claude_ai_Google_Drive":     "OMNISIGHT_MCP_GOOGLE_DRIVE_TOKEN",
    # OP-813 (A5): JIRA MCP gives the runner a tool surface for asking the
    # ticket graph — "what's the parent META?", "which siblings are in the
    # same Wave?", "what AC did the previous attempt verify?". Read-only;
    # writes (transitions, comments) still go through ``jira_dispatch`` so
    # the audit / governance layer is preserved.
    "mcp_jira":                   "OMNISIGHT_MCP_JIRA_TOKEN",
    # OP-853 (C4): Graphiti MCP gives the runner read-only temporal context
    # over ticket/status/bot/review timelines. Ingestion happens in the
    # Graphiti service; runner calls are query-only.
    "mcp_graphiti":               "OMNISIGHT_MCP_GRAPHITI_TOKEN",
}


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
    # OP-813 (A5): mcp-atlassian-style JIRA bridge. Default URL points at the
    # community ``mcp-atlassian`` reference server; operators may override to
    # a self-hosted instance via ``OMNISIGHT_MCP_JIRA_URL``. The catalog
    # advertises only **read** primitives — ``getTicket``, ``searchTickets``,
    # ``getComments`` — because the runner-side governance layer (transitions,
    # comments, AC verification) MUST stay in ``jira_dispatch``. AC#4 of
    # OP-813 pins this read-only contract explicitly.
    _CatalogEntry(
        name="mcp_jira",
        default_url="https://mcp-atlassian.local/jira",
        description=(
            "JIRA read-only MCP. Surface tools for the runner to query its "
            "own ticket + siblings: getTicket(key) returns ticket fields "
            "(summary, description, status, labels, parent, fixVersions), "
            "searchTickets(jql) runs an arbitrary JQL and returns issue keys "
            "+ summaries, getComments(key) returns the comment thread. "
            "Writes (transitions, add_comment) NEVER go through MCP — they "
            "stay in backend.agents.jira_dispatch so the governance / audit "
            "trail is single-sourced (per OP-813 AC#4)."
        ),
        sample_tools=("getTicket", "searchTickets", "getComments"),
    ),
    # OP-853 (C4): Graphiti temporal-memory MCP. Default URL points at the
    # service name used by local/self-hosted deployments; operators may
    # override it via ``OMNISIGHT_MCP_GRAPHITI_URL``. Only read/query-shaped
    # tools are advertised here; write/ingest endpoints remain service-side
    # event consumers and are refused by runner governance.
    _CatalogEntry(
        name="mcp_graphiti",
        default_url="https://mcp-graphiti.local",
        description=(
            "Graphiti read-only temporal MCP. Surface ticket-time queries for "
            "the runner: getTicketTimeline(key) returns status transition, bot "
            "pickup, and Gerrit review events; "
            "findSimilarPriorTicketsByTimeline(features) finds similar recent "
            "tickets; getBotSuccessRateByPattern(bot, feature) summarizes bot "
            "success rates by ticket pattern. Writes are refused before MCP "
            "dispatch per OP-853."
        ),
        sample_tools=(
            "getTicketTimeline",
            "findSimilarPriorTicketsByTimeline",
            "getBotSuccessRateByPattern",
        ),
    ),
)


# OP-813: read-only allowlist for JIRA MCP tool methods. The runner refuses
# any ``mcp__mcp_jira__<method>`` call where ``<method>`` is not in this set
# — the goal is structural enforcement of AC#4 ("no mutation via MCP") so a
# misbehaving server (or compromised token) cannot, e.g., call
# ``transitionTicket`` and bypass the audit layer.
MCP_JIRA_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "getTicket",
    "searchTickets",
    "getComments",
})


# OP-853: Graphiti query tools are read-only by method prefix. This mirrors
# the ticket's contract: allow ``get*``, ``find*``, ``query*``, ``list*``;
# refuse write-shaped names such as ``create*``, ``update*``, ``delete*``.
MCP_GRAPHITI_READ_ONLY_PREFIXES: tuple[str, ...] = (
    "get",
    "find",
    "query",
    "list",
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


def build_registry_from_env(
    env: dict[str, str] | None = None,
    *,
    catalog: tuple[_CatalogEntry, ...] = DEFAULT_REMOTE_MCP_CATALOG,
    env_var_by_name: dict[str, str] | None = None,
) -> RemoteMCPRegistry:
    """Build a registry from environment-supplied OAuth tokens.

    For each catalog entry whose ENV_TOKEN_VAR_BY_NAME alias is set in
    ``env`` (defaults to ``os.environ``), an enabled MCPServerConfig is
    added to the returned registry. Catalog entries with **no token** in
    env are silently skipped — Anthropic rejects MCP servers without auth
    on most managed integrations, and an empty-token request would fail
    the whole turn for an LLM call that didn't even need that MCP.

    Operators set ``OMNISIGHT_MCP_DISABLE_ALL=1`` to opt out entirely
    (e.g., for a deterministic CI run that mustn't hit Figma).

    Returns:
      A registry containing 0..N enabled servers. Caller passes
      ``registry.to_anthropic_mcp_servers()`` into
      ``AnthropicClient.run_with_tools(mcp_servers=...)``.
    """
    src = env if env is not None else os.environ
    if src.get("OMNISIGHT_MCP_DISABLE_ALL", "").strip().lower() in {
        "1", "true", "yes", "on"
    }:
        logger.info("MCP integration disabled via OMNISIGHT_MCP_DISABLE_ALL")
        return RemoteMCPRegistry()

    aliases = env_var_by_name or ENV_TOKEN_VAR_BY_NAME
    configs: list[MCPServerConfig] = []
    for entry in catalog:
        env_var = aliases.get(entry.name)
        if env_var is None:
            continue  # catalog entry without a known env alias
        token = src.get(env_var, "").strip()
        if not token:
            continue  # operator hasn't completed OAuth for this server
        # OP-813 / OP-853: self-hosted MCP siblings allow operator override of
        # the default URL. Other entries don't expose URL override yet — they
        # all live behind the Anthropic-managed gateway.
        url = entry.default_url
        if entry.name == "mcp_jira":
            override = src.get("OMNISIGHT_MCP_JIRA_URL", "").strip()
            if override:
                url = override
        elif entry.name == "mcp_graphiti":
            override = src.get("OMNISIGHT_MCP_GRAPHITI_URL", "").strip()
            if override:
                url = override
        configs.append(
            MCPServerConfig(
                name=entry.name,
                url=url,
                authorization_token=token,
                description=entry.description,
                enabled=True,
            )
        )
    if configs:
        logger.info(
            "MCP registry: %d server(s) configured via env (%s)",
            len(configs),
            ", ".join(c.name for c in configs),
        )
    return RemoteMCPRegistry(configs)


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


def is_jira_mcp_read_only_tool(tool_name: str) -> bool:
    """OP-813 (A5): structural read-only check for the JIRA MCP server.

    Returns True iff ``tool_name`` is of the form
    ``mcp__mcp_jira__<method>`` AND ``<method>`` is in the
    :data:`MCP_JIRA_READ_ONLY_TOOLS` allowlist (``getTicket``,
    ``searchTickets``, ``getComments``).

    The runner uses this to enforce AC#4 ("no mutation via MCP") by routing
    only the allowlisted methods to the MCP server. A misbehaving server
    advertising a write-shaped method (e.g., ``transitionTicket``) is
    refused before the dispatcher fires, so the audit layer in
    ``jira_dispatch`` remains the single source of truth for JIRA mutations.

    Returns False for non-MCP tools, MCP tools targeting other servers, and
    JIRA MCP tool names not in the allowlist.
    """
    parsed = parse_mcp_tool_name(tool_name)
    if parsed is None:
        return False
    server, method = parsed
    return server == "mcp_jira" and method in MCP_JIRA_READ_ONLY_TOOLS


def is_graphiti_mcp_read_only_tool(tool_name: str) -> bool:
    """OP-853 (C4): structural read-only check for the Graphiti MCP server.

    Returns True iff ``tool_name`` is of the form
    ``mcp__mcp_graphiti__<method>`` AND ``<method>`` starts with one of
    :data:`MCP_GRAPHITI_READ_ONLY_PREFIXES` (``get``, ``find``, ``query``,
    ``list``). Mutation-shaped names are refused before dispatch so Graphiti
    remains a temporal-query surface for the runner, not a write path.
    """
    parsed = parse_mcp_tool_name(tool_name)
    if parsed is None:
        return False
    server, method = parsed
    return server == "mcp_graphiti" and method.startswith(
        MCP_GRAPHITI_READ_ONLY_PREFIXES
    )


# ─── Local (in-process) MCP server registration — META OP-814 ─────
#
# Some MCP servers run *inside* the runner process rather than over SSE
# (e.g. the Gerrit wrapper at :mod:`backend.agents.mcp_gerrit`, which is
# a thin shim over ``gerrit-ssh-cli`` calls already used by
# ``jira_dispatch``). They share the ``mcp_<server>__<method>`` naming
# convention so the agent treats them uniformly, but they do not get
# forwarded to Anthropic via ``mcp_servers=[]``; the runner dispatches
# them locally.

_LOCAL_MCP_HANDLERS: dict[str, Any] = {}
_LOCAL_MCP_TOOL_SCHEMAS: dict[str, list[dict[str, Any]]] = {}


def register_local_mcp_server(
    server_name: str,
    *,
    tool_handlers: dict[str, Any],
    tool_schemas: list[dict[str, Any]],
) -> None:
    """Register an in-process MCP server's tool handlers + schemas.

    ``tool_handlers`` maps full tool name (``<server>__<method>``) →
    callable. ``tool_schemas`` is the JSON Schema list the agent sees.
    Re-registration replaces the prior entry so a hot-reload during
    tests is safe.
    """
    for name in tool_handlers:
        if not name.startswith(f"{server_name}__"):
            raise ValueError(
                f"Local MCP {server_name!r}: tool name {name!r} must "
                f"start with {server_name!r} prefix"
            )
    _LOCAL_MCP_HANDLERS.update(tool_handlers)
    _LOCAL_MCP_TOOL_SCHEMAS[server_name] = list(tool_schemas)
    logger.info(
        "Local MCP server registered: %s (%d tools)",
        server_name, len(tool_handlers),
    )


def is_local_mcp_tool(tool_name: str) -> bool:
    """Return True if ``tool_name`` matches a registered local MCP tool."""
    return tool_name in _LOCAL_MCP_HANDLERS


def dispatch_local_mcp_tool(tool_name: str, input: dict[str, Any]) -> Any:
    """Execute a registered local MCP tool call."""
    handler = _LOCAL_MCP_HANDLERS.get(tool_name)
    if handler is None:
        raise KeyError(
            f"Unknown local MCP tool {tool_name!r}; known: "
            f"{sorted(_LOCAL_MCP_HANDLERS)}"
        )
    return handler(input)


def local_mcp_tool_schemas(server_name: str | None = None) -> list[dict[str, Any]]:
    """Return JSON Schemas for all (or one) registered local MCP server."""
    if server_name is None:
        out: list[dict[str, Any]] = []
        for schemas in _LOCAL_MCP_TOOL_SCHEMAS.values():
            out.extend(schemas)
        return out
    return list(_LOCAL_MCP_TOOL_SCHEMAS.get(server_name, ()))


def registered_local_mcp_servers() -> list[str]:
    return sorted(_LOCAL_MCP_TOOL_SCHEMAS)


def _register_default_local_mcp_servers() -> None:
    """Register the in-process MCP servers shipped with the runner.

    Currently:
      * ``mcp_gerrit`` — META OP-814 read-only wrapper over gerrit-ssh-cli.
    """
    from backend.agents import mcp_gerrit

    register_local_mcp_server(
        mcp_gerrit.SERVER_NAME,
        tool_handlers=mcp_gerrit.TOOL_HANDLERS,
        tool_schemas=mcp_gerrit.MCP_GERRIT_TOOL_SCHEMAS,
    )


_register_default_local_mcp_servers()
