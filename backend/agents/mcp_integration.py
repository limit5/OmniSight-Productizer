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

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

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


@dataclass(frozen=True)
class MCPToolListProbeResult:
    """Result from a direct MCP ``tools/list`` reachability probe."""

    server_name: str
    url: str
    tool_names: tuple[str, ...]
    raw: dict[str, Any]


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


# OP-2593 (U6-0 T2a): read-only allowlist for the Figma MCP server. Figma is
# a MIXED-capability remote MCP whose write methods (create_new_file,
# upload_assets, use_figma, ...) execute PROVIDER-SIDE — before any local
# guard can run. Enforcement therefore lives in the beta ``tool_configuration.
# allowed_tools`` per-server field on the forwarded ``mcp_servers=[]`` entry
# (see :meth:`RemoteMCPRegistry.to_anthropic_mcp_servers`).
#
# The 11 methods below are the FULL live read-only surface of
# ``claude_ai_Figma`` at the time of this containment; the 7 known writes
# (create_new_file, add_code_connect_map, send_code_connect_mappings,
# upload_assets, generate_diagram, use_figma, download_assets) are EXCLUDED.
# FAIL-CLOSED: any Figma method not positively listed here is refused. If
# the live catalog adds a new method it defaults to excluded until
# classified. ``download_assets`` is technically a read (exports image
# bytes) but is deliberately excluded under fail-closed until the T2b
# kernel-governed local proxy lands.
FIGMA_MCP_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "get_design_context",
    "get_screenshot",
    "get_metadata",
    "get_variable_defs",
    "get_code_connect_map",
    "get_context_for_code_connect",
    "get_code_connect_suggestions",
    "search_design_system",
    "get_figjam",
    "get_libraries",
    "whoami",
})


# OP-2607 (U6-0 P-PROV-A): exact read-only allowlist for the Graphiti MCP
# server, replacing the OP-853 prefix predicate (``get*``/``find*``/``query*``
# /``list*``), which was too loose — a future write-shaped method named e.g.
# ``getAndUpdateNode`` would have slipped through. This is the FULL legitimate
# set, pinned at design time from the catalog + every real call site
# (``queryTimeline``/``listPatterns`` existed only in prefix-predicate tests,
# never in the catalog or a caller — they are now refused).
MCP_GRAPHITI_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "getTicketTimeline",
    "findSimilarPriorTicketsByTimeline",
    "getBotSuccessRateByPattern",
})


# ─── OP-2607 (U6-0 P-PROV-A): per-server MCP forwarding policy ────
#
# Remote MCP servers execute PROVIDER-SIDE (Anthropic runs them) — the
# in-process dispatcher guard cannot reach them. The only lever is what
# we FORWARD in ``mcp_servers=[]``. The policy map below is the single
# source of truth for that decision: default-DENY unknown servers,
# origin-pinned URLs, exact per-server method allowlists.
#
# RESIDUAL ASSUMPTION: a method allowlist cannot prove SERVER behavior —
# a malicious server could hide a side effect behind ``getTicket``.
# Endpoint/operator trust of the pinned origins below is an explicit
# assumption until the T2b kernel-governed local proxy lands. Widening
# (new origins, new methods, new servers) happens ONLY via a reviewed
# policy change here (H0 pattern), never via env/config at runtime — an
# attacker-controlled env must not be able to re-open the hole.


@dataclass(frozen=True)
class McpServerPolicy:
    """Forwarding policy for one known remote MCP server.

    Invariants (validated at import time over :data:`MCP_SERVER_POLICY`):
      * ``action == "forward"`` ⇒ non-empty ``allowed_tools`` AND
        non-empty ``allowed_url_prefixes``.
      * ``action == "deny"`` ⇒ both are ``None``.
    """

    action: Literal["forward", "deny"]

    allowed_tools: frozenset[str] | None
    """Exact method allowlist injected as ``tool_configuration.
    allowed_tools`` on the forwarded entry. Fail-closed: methods not
    positively listed are refused provider-side."""

    allowed_url_prefixes: tuple[str, ...] | None
    """Pinned origins. A ``forward`` policy applies ONLY when the config
    URL matches one of these prefixes (delimiter-safe, see
    :func:`_url_matches_pinned_prefix`); name-match + URL-mismatch is
    treated as an impersonation attempt and DENIED."""


MCP_SERVER_POLICY: dict[str, McpServerPolicy] = {
    "claude_ai_Figma": McpServerPolicy(
        action="forward",
        allowed_tools=FIGMA_MCP_READ_ONLY_TOOLS,
        # The Anthropic-managed Figma gateway (DEFAULT_REMOTE_MCP_CATALOG).
        allowed_url_prefixes=(
            "https://mcp.anthropic.com/v1/integrations/figma",
        ),
    ),
    "mcp_jira": McpServerPolicy(
        action="forward",
        allowed_tools=MCP_JIRA_READ_ONLY_TOOLS,
        # Catalog default only: no production JIRA MCP origin is documented
        # (checked docs/operations/ runbooks + deploy/caddy/ — only Graphiti
        # has a public ingress). Add the production origin here in a
        # reviewed change when one is deployed.
        allowed_url_prefixes=(
            "https://mcp-atlassian.local/jira",
        ),
    ),
    "mcp_graphiti": McpServerPolicy(
        action="forward",
        allowed_tools=MCP_GRAPHITI_READ_ONLY_TOOLS,
        # Catalog default + the documented production origin
        # (docs/operations/graphiti-mcp-runbook.md, deploy/caddy/
        # mcp-graphiti.caddy). Pinning only the catalog default would
        # silently drop the prod deployment as "impersonation".
        allowed_url_prefixes=(
            "https://mcp-graphiti.local",
            "https://mcp-graphiti.sora.services",
        ),
    ),
    # Inactive-unless-token today; the policy delta is that a token can no
    # longer re-enable them — forwarding requires a reviewed policy change.
    "claude_ai_Gmail": McpServerPolicy(
        action="deny", allowed_tools=None, allowed_url_prefixes=None,
    ),
    "claude_ai_Google_Calendar": McpServerPolicy(
        action="deny", allowed_tools=None, allowed_url_prefixes=None,
    ),
    "claude_ai_Google_Drive": McpServerPolicy(
        action="deny", allowed_tools=None, allowed_url_prefixes=None,
    ),
}


def _validate_mcp_server_policy_map() -> None:
    """Import-time invariant check over :data:`MCP_SERVER_POLICY`."""
    for name, policy in MCP_SERVER_POLICY.items():
        if policy.action == "forward":
            if not policy.allowed_tools or not policy.allowed_url_prefixes:
                raise ValueError(
                    f"backend.agents.mcp_integration.MCP_SERVER_POLICY: "
                    f"forward policy for {name!r} must carry a non-empty "
                    f"allowed_tools AND non-empty allowed_url_prefixes "
                    f"(got allowed_tools={policy.allowed_tools!r}, "
                    f"allowed_url_prefixes={policy.allowed_url_prefixes!r})"
                )
        elif policy.action == "deny":
            if (
                policy.allowed_tools is not None
                or policy.allowed_url_prefixes is not None
            ):
                raise ValueError(
                    f"backend.agents.mcp_integration.MCP_SERVER_POLICY: "
                    f"deny policy for {name!r} must carry allowed_tools=None "
                    f"and allowed_url_prefixes=None "
                    f"(got allowed_tools={policy.allowed_tools!r}, "
                    f"allowed_url_prefixes={policy.allowed_url_prefixes!r})"
                )
        else:  # pragma: no cover - Literal-typed, defensive
            raise ValueError(
                f"backend.agents.mcp_integration.MCP_SERVER_POLICY: "
                f"unknown action {policy.action!r} for {name!r}"
            )


_validate_mcp_server_policy_map()


def _url_matches_pinned_prefix(url: str, prefix: str) -> bool:
    """Delimiter-safe prefix match between a config URL and a pinned origin.

    Naive ``str.startswith`` is defeated by host-suffix impostors:
    ``"https://mcp-graphiti.localhost.evil.example/mcp"`` startswith
    ``"https://mcp-graphiti.local"`` is True. Normalizing BOTH sides to end
    with ``/`` forces the character after the pinned prefix to be a path
    delimiter, so a host-suffix impostor cannot match.
    """
    normalized_prefix = prefix if prefix.endswith("/") else prefix + "/"
    candidate = url if url.endswith("/") else url + "/"
    return candidate.startswith(normalized_prefix)


def enforce_mcp_policy(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter raw ``mcp_servers=[]`` payload entries through the policy map.

    Registry-independent by design: it validates raw name/url dicts, so the
    same mechanism serves two call sites — the registry payload producer
    (:meth:`RemoteMCPRegistry.to_anthropic_mcp_servers`) and, per P-PROV-B,
    the final client boundary where callers may hand in arbitrary dicts.

    Per entry:
      * unknown server name        → DROP + warning (default-DENY)
      * ``deny`` policy            → DROP
      * pinned-origin mismatch     → DROP + warning (impersonation attempt)
      * ``forward`` + origin match → KEEP, (re)injecting
        ``tool_configuration = {"allowed_tools": sorted(...)}`` — exactly
        that one-key shape.

    Input order is preserved for the deterministic-payload contract; input
    dicts are not mutated.
    """
    out: list[dict[str, Any]] = []
    for entry in servers:
        name = entry.get("name", "")
        policy = MCP_SERVER_POLICY.get(name)
        if policy is None:
            logger.warning(
                "backend.agents.mcp_integration.enforce_mcp_policy: "
                "dropping UNKNOWN MCP server %r (url=%r) — default-DENY; "
                "known policy names: %s",
                name, entry.get("url"), sorted(MCP_SERVER_POLICY),
            )
            continue
        if policy.action == "deny":
            logger.info(
                "backend.agents.mcp_integration.enforce_mcp_policy: "
                "dropping MCP server %r — explicit deny policy",
                name,
            )
            continue
        url = entry.get("url", "")
        assert policy.allowed_url_prefixes is not None  # forward invariant
        assert policy.allowed_tools is not None  # forward invariant
        if not any(
            _url_matches_pinned_prefix(url, prefix)
            for prefix in policy.allowed_url_prefixes
        ):
            logger.warning(
                "backend.agents.mcp_integration.enforce_mcp_policy: "
                "dropping MCP server %r — URL %r does not match any pinned "
                "origin %r (possible impersonation attempt)",
                name, url, policy.allowed_url_prefixes,
            )
            continue
        kept = dict(entry)
        kept["tool_configuration"] = {
            "allowed_tools": sorted(policy.allowed_tools),
        }
        out.append(kept)
    return out


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
            f"backend.agents.mcp_integration.build_default_server_config: "
            f"Unknown remote MCP server {name!r} "
            f"(url_override={url_override!r}, "
            f"authorization_token={'<set>' if authorization_token else None}). "
            f"Known DEFAULT_REMOTE_MCP_CATALOG entries: {sorted(catalog)}"
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

        OP-2607 (U6-0 P-PROV-A): every candidate entry is routed through
        :func:`enforce_mcp_policy` — unknown servers and explicit-deny
        servers are dropped, origins are pinned, and each forwarded entry
        carries its exact ``tool_configuration.allowed_tools`` allowlist
        (this replaced the OP-2593 Figma-only special case; one mechanism,
        two call sites — the P-PROV-B client boundary is the other).
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
        return enforce_mcp_policy(out)

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
    disable_raw = src.get("OMNISIGHT_MCP_DISABLE_ALL", "")
    if disable_raw.strip().lower() in {"1", "true", "yes", "on"}:
        logger.info(
            "backend.agents.mcp_integration.build_registry_from_env: "
            "MCP integration disabled via OMNISIGHT_MCP_DISABLE_ALL=%r "
            "(returning empty registry)",
            disable_raw,
        )
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
            "backend.agents.mcp_integration.build_registry_from_env: "
            "MCP registry built — %d of %d catalog entries configured "
            "via env (%s); skipped %d without an env token",
            len(configs),
            len(catalog),
            ", ".join(c.name for c in configs),
            len(catalog) - len(configs),
        )
    else:
        logger.info(
            "backend.agents.mcp_integration.build_registry_from_env: "
            "MCP registry built — 0 of %d catalog entries had a token in env "
            "(checked env vars: %s); returning empty registry",
            len(catalog),
            sorted(aliases.values()),
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


def query_mcp_tool_list(
    config: MCPServerConfig,
    *,
    opener: Any = urllib.request.urlopen,
    timeout: float = 10.0,
) -> MCPToolListProbeResult:
    """Probe a remote MCP server by issuing an authenticated ``tools/list``.

    This is a runner/deploy smoke primitive, not the normal production
    dispatch path. Production still passes ``mcp_servers=[]`` to Anthropic;
    this helper lets a pickup or deploy check prove that the configured URL
    and bearer token can reach the server before relying on SDK discovery.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": f"{config.name}-tools-list",
        "method": "tools/list",
        "params": {},
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if config.authorization_token:
        headers["Authorization"] = f"Bearer {config.authorization_token}"
    request = urllib.request.Request(
        config.url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers=headers,
    )
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read().decode()
    except urllib.error.HTTPError:
        raise
    except OSError as exc:
        raise ConnectionError(
            f"backend.agents.mcp_integration.query_mcp_tool_list: "
            f"MCP tools/list probe failed for server {config.name!r} "
            f"at {config.url!r} (timeout={timeout}s, "
            f"authorization_token={'<set>' if config.authorization_token else None}): "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    parsed = json.loads(body) if body else {}
    tools = parsed.get("result", {}).get("tools", [])
    tool_names = tuple(
        item["name"]
        for item in tools
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    )
    return MCPToolListProbeResult(
        server_name=config.name,
        url=config.url,
        tool_names=tool_names,
        raw=parsed,
    )


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
    """OP-853 (C4) / OP-2607 (P-PROV-A): structural read-only check for the
    Graphiti MCP server.

    Returns True iff ``tool_name`` is of the form
    ``mcp__mcp_graphiti__<method>`` AND ``<method>`` is in the exact
    :data:`MCP_GRAPHITI_READ_ONLY_TOOLS` allowlist. The OP-853 prefix
    predicate (``get*``/``find*``/``query*``/``list*``) is gone — any
    method not positively pinned is refused before dispatch, so Graphiti
    remains a temporal-query surface for the runner, not a write path.
    """
    parsed = parse_mcp_tool_name(tool_name)
    if parsed is None:
        return False
    server, method = parsed
    return server == "mcp_graphiti" and method in MCP_GRAPHITI_READ_ONLY_TOOLS


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
    required_prefix = f"{server_name}__"
    for name in tool_handlers:
        if not name.startswith(required_prefix):
            raise ValueError(
                f"backend.agents.mcp_integration.register_local_mcp_server: "
                f"local MCP server {server_name!r} tool name {name!r} must "
                f"start with required prefix {required_prefix!r} "
                f"(got tool_handlers keys: {sorted(tool_handlers)})"
            )
    _LOCAL_MCP_HANDLERS.update(tool_handlers)
    _LOCAL_MCP_TOOL_SCHEMAS[server_name] = list(tool_schemas)
    logger.info(
        "backend.agents.mcp_integration.register_local_mcp_server: "
        "local MCP server %r registered with %d tool(s): %s",
        server_name, len(tool_handlers), sorted(tool_handlers),
    )


def is_local_mcp_tool(tool_name: str) -> bool:
    """Return True if ``tool_name`` matches a registered local MCP tool."""
    return tool_name in _LOCAL_MCP_HANDLERS


def dispatch_local_mcp_tool(tool_name: str, input: dict[str, Any]) -> Any:
    """Execute a registered local MCP tool call."""
    handler = _LOCAL_MCP_HANDLERS.get(tool_name)
    if handler is None:
        if not _LOCAL_MCP_HANDLERS:
            raise KeyError(
                f"backend.agents.mcp_integration.dispatch_local_mcp_tool: "
                f"Unknown local MCP tool {tool_name!r}; no local MCP servers "
                f"are registered (call register_local_mcp_server first)"
            )
        raise KeyError(
            f"backend.agents.mcp_integration.dispatch_local_mcp_tool: "
            f"Unknown local MCP tool {tool_name!r}; "
            f"registered servers: {registered_local_mcp_servers()}; "
            f"known tool names: {sorted(_LOCAL_MCP_HANDLERS)}"
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
