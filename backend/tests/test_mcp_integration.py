"""AB.5.6 — Remote MCP server integration tests.

Locks:

  - MCPServerConfig redacts authorization_token in repr (no log leaks)
  - to_anthropic_payload builds the {type, url, name, [authorization_token]} shape
  - DEFAULT_REMOTE_MCP_CATALOG ships 4 known entries (Figma / Gmail / Calendar / Drive)
  - build_default_server_config wires catalog metadata + caller token
  - build_default_server_config rejects unknown name
  - RemoteMCPRegistry: add idempotent (replacement on same name OK),
    remove returns bool, get + list_all + enabled_only filter
  - to_anthropic_mcp_servers: enabled servers only, optional name subset,
    stable ordering, empty registry → empty list
  - OP-2607 (U6-0 P-PROV-A): per-server policy map — default-DENY unknown
    servers, origin pinning (delimiter-safe), exact allowlists injected as
    tool_configuration.allowed_tools on every forwarded entry
  - parse_mcp_tool_name: valid prefix → (server, method), invalid → None
  - is_mcp_tool boolean wrapper
  - OP-2605 (U6-0 P-PROV-B): AnthropicClient.simple_params and
    run_with_tools re-validate mcp_servers via enforce_mcp_policy at the
    FINAL client boundary (rogue/denied dropped, empty result → param
    omitted) and run_with_tools pins MCP_CONNECTOR_BETA per-request,
    merged with ctor beta_headers

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §5.6
"""

from __future__ import annotations

import json
import sys
import types
import urllib.error
from typing import Any

import pytest

from backend.agents.mcp_integration import (
    DEFAULT_REMOTE_MCP_CATALOG,
    ENV_TOKEN_VAR_BY_NAME,
    FIGMA_MCP_READ_ONLY_TOOLS,
    MCP_GRAPHITI_READ_ONLY_TOOLS,
    MCP_JIRA_READ_ONLY_TOOLS,
    MCP_SERVER_POLICY,
    MCPServerConfig,
    RemoteMCPRegistry,
    build_default_server_config,
    default_catalog_by_name,
    enforce_mcp_policy,
    is_mcp_tool,
    parse_mcp_tool_name,
    query_mcp_tool_list,
)


# ─── MCPServerConfig basics ──────────────────────────────────────


def test_config_redacts_token_in_repr():
    cfg = MCPServerConfig(
        name="claude_ai_Figma",
        url="https://example.com/sse",
        authorization_token="VERY_SECRET_TOKEN_DO_NOT_LOG",
    )
    rep = repr(cfg)
    assert "VERY_SECRET" not in rep
    assert "redacted" in rep
    assert "claude_ai_Figma" in rep


def test_config_repr_shows_no_token_when_none():
    cfg = MCPServerConfig(name="x", url="https://x")
    rep = repr(cfg)
    assert "redacted" not in rep
    assert "None" in rep


def test_config_to_anthropic_payload_with_token():
    cfg = MCPServerConfig(
        name="claude_ai_Gmail",
        url="https://mcp/gmail",
        authorization_token="tok_123",
    )
    payload = cfg.to_anthropic_payload()
    assert payload == {
        "type": "url",
        "url": "https://mcp/gmail",
        "name": "claude_ai_Gmail",
        "authorization_token": "tok_123",
    }


def test_config_to_anthropic_payload_without_token():
    cfg = MCPServerConfig(name="public_x", url="https://x")
    payload = cfg.to_anthropic_payload()
    assert "authorization_token" not in payload
    assert payload["type"] == "url"


# ─── DEFAULT_REMOTE_MCP_CATALOG ──────────────────────────────────


def test_catalog_has_known_servers():
    """Pin the full catalog so adding a new server is deliberate."""
    names = {entry.name for entry in DEFAULT_REMOTE_MCP_CATALOG}
    assert names == {
        "claude_ai_Figma",
        "claude_ai_Gmail",
        "claude_ai_Google_Calendar",
        "claude_ai_Google_Drive",
        "mcp_jira",  # OP-813 (A5)
        "mcp_graphiti",  # OP-853 (C4)
    }


def test_env_token_aliases_cover_default_catalog():
    """Each default managed MCP must have an env-token alias."""
    names = {entry.name for entry in DEFAULT_REMOTE_MCP_CATALOG}
    assert set(ENV_TOKEN_VAR_BY_NAME) == names


def test_catalog_entries_have_url_description_sample_tools():
    for entry in DEFAULT_REMOTE_MCP_CATALOG:
        assert entry.default_url.startswith("https://")
        assert entry.description
        assert isinstance(entry.sample_tools, tuple)
        assert len(entry.sample_tools) >= 1


def test_default_catalog_by_name_returns_dict():
    by_name = default_catalog_by_name()
    assert "claude_ai_Figma" in by_name
    assert by_name["claude_ai_Figma"].name == "claude_ai_Figma"


# ─── build_default_server_config ─────────────────────────────────


def test_build_default_uses_catalog_url():
    cfg = build_default_server_config("claude_ai_Figma")
    catalog = default_catalog_by_name()
    assert cfg.url == catalog["claude_ai_Figma"].default_url


def test_build_default_url_override():
    cfg = build_default_server_config(
        "claude_ai_Figma",
        url_override="https://my-private-mcp/figma",
    )
    assert cfg.url == "https://my-private-mcp/figma"
    # Description still inherited
    assert cfg.description.startswith("Figma official MCP")


def test_build_default_with_token():
    cfg = build_default_server_config(
        "claude_ai_Gmail",
        authorization_token="from_oauth_flow",
    )
    assert cfg.authorization_token == "from_oauth_flow"


def test_build_default_unknown_raises():
    with pytest.raises(KeyError, match="Unknown remote MCP server"):
        build_default_server_config("not_a_known_mcp")


# ─── RemoteMCPRegistry ───────────────────────────────────────────


def _figma_cfg(token: str = "tok_figma") -> MCPServerConfig:
    return build_default_server_config("claude_ai_Figma", authorization_token=token)


def _gmail_cfg(token: str = "tok_gmail") -> MCPServerConfig:
    return build_default_server_config("claude_ai_Gmail", authorization_token=token)


def test_registry_starts_empty():
    reg = RemoteMCPRegistry()
    assert len(reg) == 0
    assert reg.list_all() == []
    assert reg.to_anthropic_mcp_servers() == []


def test_registry_initial_configs():
    reg = RemoteMCPRegistry(configs=[_figma_cfg(), _gmail_cfg()])
    assert len(reg) == 2
    assert reg.configured_names() == ["claude_ai_Figma", "claude_ai_Gmail"]


def test_registry_add_and_get():
    reg = RemoteMCPRegistry()
    reg.add(_figma_cfg())
    fetched = reg.get("claude_ai_Figma")
    assert fetched is not None
    assert fetched.authorization_token == "tok_figma"


def test_registry_add_replaces_existing_name():
    """Re-adding with same name = OAuth refresh, idempotent."""
    reg = RemoteMCPRegistry()
    reg.add(_figma_cfg(token="old"))
    reg.add(_figma_cfg(token="new_after_refresh"))
    assert reg.get("claude_ai_Figma").authorization_token == "new_after_refresh"
    assert len(reg) == 1


def test_registry_remove_returns_bool():
    reg = RemoteMCPRegistry(configs=[_figma_cfg()])
    assert reg.remove("claude_ai_Figma") is True
    assert reg.remove("claude_ai_Figma") is False  # already gone


def test_registry_list_all_sorted():
    reg = RemoteMCPRegistry(configs=[_gmail_cfg(), _figma_cfg()])
    names = [s.name for s in reg.list_all()]
    assert names == ["claude_ai_Figma", "claude_ai_Gmail"]


def test_registry_list_enabled_only_filter():
    figma = _figma_cfg()
    gmail = MCPServerConfig(
        name="claude_ai_Gmail", url="https://x", enabled=False,
    )
    reg = RemoteMCPRegistry(configs=[figma, gmail])
    assert len(reg.list_all()) == 2
    assert len(reg.list_all(enabled_only=True)) == 1
    assert reg.list_all(enabled_only=True)[0].name == "claude_ai_Figma"


# ─── to_anthropic_mcp_servers ────────────────────────────────────


def test_anthropic_payload_filters_disabled():
    figma = _figma_cfg()
    disabled = MCPServerConfig(
        name="claude_ai_Gmail", url="https://x",
        authorization_token="tok", enabled=False,
    )
    reg = RemoteMCPRegistry(configs=[figma, disabled])
    payload = reg.to_anthropic_mcp_servers()
    assert len(payload) == 1
    assert payload[0]["name"] == "claude_ai_Figma"


def test_anthropic_payload_only_names_subset():
    """Caller scopes a request to a specific MCP subset."""
    reg = RemoteMCPRegistry(configs=[
        _figma_cfg(),
        _gmail_cfg(),
        build_default_server_config("claude_ai_Google_Drive", authorization_token="t"),
    ])
    payload = reg.to_anthropic_mcp_servers(only_names=["claude_ai_Figma"])
    assert len(payload) == 1
    assert payload[0]["name"] == "claude_ai_Figma"


def test_anthropic_payload_stable_ordering():
    """Same registry → same payload byte-equal across runs (deterministic).

    OP-2607: strengthened — Gmail and Calendar carry explicit-DENY policies,
    so of the three registered servers only Figma survives the policy filter.
    """
    reg = RemoteMCPRegistry(configs=[
        _gmail_cfg(),
        _figma_cfg(),
        build_default_server_config("claude_ai_Google_Calendar", authorization_token="t"),
    ])
    a = reg.to_anthropic_mcp_servers()
    b = reg.to_anthropic_mcp_servers()
    assert a == b
    names_in_order = [s["name"] for s in a]
    assert names_in_order == sorted(names_in_order)
    # Policy filter: only the forward-policy server remains.
    assert names_in_order == ["claude_ai_Figma"]


def test_anthropic_payload_empty_when_only_names_no_match():
    reg = RemoteMCPRegistry(configs=[_figma_cfg()])
    payload = reg.to_anthropic_mcp_servers(only_names=["nonexistent"])
    assert payload == []


# ─── OP-2593 (U6-0 T2a): Figma read-only allowlist on forward ────


# Authoritative mutating-method list from the ticket: these 7 Figma methods
# execute provider-side and MUST NOT appear in the forwarded allowed_tools.
_FIGMA_MUTATING_METHODS = (
    "create_new_file",
    "add_code_connect_map",
    "send_code_connect_mappings",
    "upload_assets",
    "generate_diagram",
    "use_figma",
    "download_assets",
)


def test_figma_read_only_constant_exposes_the_expected_11_reads():
    """The frozenset pins the read-only surface at 11 methods (T2a scope)."""
    assert FIGMA_MCP_READ_ONLY_TOOLS == frozenset({
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
    assert len(FIGMA_MCP_READ_ONLY_TOOLS) == 11


def test_anthropic_payload_injects_figma_tool_configuration_allowed_tools():
    """AC: the claude_ai_Figma entry carries tool_configuration.allowed_tools
    sorted; the 3 core reads are present; all 7 mutating names are absent."""
    reg = RemoteMCPRegistry(configs=[_figma_cfg()])
    payload = reg.to_anthropic_mcp_servers()
    assert len(payload) == 1
    figma_entry = payload[0]
    assert figma_entry["name"] == "claude_ai_Figma"
    assert "tool_configuration" in figma_entry
    tc = figma_entry["tool_configuration"]
    assert set(tc.keys()) == {"allowed_tools"}

    allowed = tc["allowed_tools"]
    # Deterministic-ordering contract: sorted.
    assert allowed == sorted(allowed)
    # Full 11-read surface forwarded.
    assert allowed == sorted(FIGMA_MCP_READ_ONLY_TOOLS)

    # (b) Core reads PRESENT.
    for core_read in ("get_design_context", "get_screenshot", "get_metadata"):
        assert core_read in allowed

    # (a) All 7 mutating names ABSENT.
    for mutating in _FIGMA_MUTATING_METHODS:
        assert mutating not in allowed


def test_anthropic_payload_every_forwarded_entry_carries_tool_configuration():
    """OP-2607 contract (rewrite of the pre-policy 'non-Figma entry has no
    tool_configuration' pin): every FORWARDED entry carries a
    ``tool_configuration`` with exactly the one-key ``{"allowed_tools"}``
    shape; denied servers (Gmail) are absent from the payload entirely —
    a token no longer re-enables them."""
    reg = RemoteMCPRegistry(configs=[
        _figma_cfg(),
        _gmail_cfg(),
        build_default_server_config("mcp_jira", authorization_token="tok-jira"),
    ])
    payload = reg.to_anthropic_mcp_servers()
    by_name = {e["name"]: e for e in payload}

    assert set(by_name) == {"claude_ai_Figma", "mcp_jira"}
    assert "claude_ai_Gmail" not in by_name  # explicit deny → dropped

    for entry in payload:
        assert set(entry["tool_configuration"].keys()) == {"allowed_tools"}
        allowed = entry["tool_configuration"]["allowed_tools"]
        assert allowed == sorted(allowed)


def test_anthropic_payload_figma_allowlist_offline_no_token_needed():
    """AC 'Exercised': run the check with no Figma token / live turn — inspect
    the produced dict directly and state the exact allowed_tools list."""
    reg = RemoteMCPRegistry(configs=[
        build_default_server_config("claude_ai_Figma", authorization_token=None),
    ])
    payload = reg.to_anthropic_mcp_servers()

    figma_entry = payload[0]
    # No token flowed through: authorization_token key omitted per
    # MCPServerConfig.to_anthropic_payload contract.
    assert "authorization_token" not in figma_entry
    # Exact forwarded allowed_tools list (as would go to the Anthropic beta):
    assert figma_entry["tool_configuration"]["allowed_tools"] == [
        "get_code_connect_map",
        "get_code_connect_suggestions",
        "get_context_for_code_connect",
        "get_design_context",
        "get_figjam",
        "get_libraries",
        "get_metadata",
        "get_screenshot",
        "get_variable_defs",
        "search_design_system",
        "whoami",
    ]
    # Fail-closed confirmation: NO mutating method present.
    for mutating in _FIGMA_MUTATING_METHODS:
        assert mutating not in figma_entry["tool_configuration"]["allowed_tools"]


# ─── OP-2607 (U6-0 P-PROV-A): per-server policy map ──────────────


def test_anthropic_payload_drops_unknown_server_with_warning(caplog):
    """Default-DENY: a server not in MCP_SERVER_POLICY never reaches the
    payload, and the drop is logged as a warning."""
    reg = RemoteMCPRegistry(configs=[
        _figma_cfg(),
        MCPServerConfig(
            name="rogue_server",
            url="https://rogue.example/mcp",
            authorization_token="tok-rogue",
        ),
    ])
    with caplog.at_level("WARNING", logger="backend.agents.mcp_integration"):
        payload = reg.to_anthropic_mcp_servers()

    assert [e["name"] for e in payload] == ["claude_ai_Figma"]
    assert any(
        "UNKNOWN MCP server 'rogue_server'" in rec.getMessage()
        for rec in caplog.records
    )


def test_anthropic_payload_drops_origin_impersonation_with_warning(caplog):
    """Name-impersonation: a config NAMED mcp_jira with a non-pinned URL
    must NOT inherit the trusted allowlist — it is dropped + warned."""
    reg = RemoteMCPRegistry(configs=[
        MCPServerConfig(
            name="mcp_jira",
            url="https://evil.example/mcp",
            authorization_token="tok",
        ),
    ])
    with caplog.at_level("WARNING", logger="backend.agents.mcp_integration"):
        payload = reg.to_anthropic_mcp_servers()

    assert payload == []
    assert any(
        "impersonation" in rec.getMessage() for rec in caplog.records
    )


def test_anthropic_payload_jira_and_graphiti_carry_exact_allowlists():
    """Forwarded self-hosted servers carry their exact sorted allowlists."""
    reg = RemoteMCPRegistry(configs=[
        build_default_server_config("mcp_jira", authorization_token="t1"),
        build_default_server_config("mcp_graphiti", authorization_token="t2"),
    ])
    payload = reg.to_anthropic_mcp_servers()
    by_name = {e["name"]: e for e in payload}

    assert set(by_name) == {"mcp_jira", "mcp_graphiti"}
    assert by_name["mcp_jira"]["tool_configuration"] == {
        "allowed_tools": ["getComments", "getTicket", "searchTickets"],
    }
    assert by_name["mcp_graphiti"]["tool_configuration"] == {
        "allowed_tools": [
            "findSimilarPriorTicketsByTimeline",
            "getBotSuccessRateByPattern",
            "getTicketTimeline",
        ],
    }
    assert by_name["mcp_jira"]["tool_configuration"]["allowed_tools"] == sorted(
        MCP_JIRA_READ_ONLY_TOOLS
    )
    assert by_name["mcp_graphiti"]["tool_configuration"][
        "allowed_tools"
    ] == sorted(MCP_GRAPHITI_READ_ONLY_TOOLS)


def test_anthropic_payload_forwards_documented_graphiti_production_origin():
    """The pinned origins include the documented production Graphiti origin
    (graphiti-mcp-runbook.md / deploy/caddy/mcp-graphiti.caddy) — a prod
    deployment using the OMNISIGHT_MCP_GRAPHITI_URL override must NOT be
    dropped as impersonation."""
    reg = RemoteMCPRegistry(configs=[
        build_default_server_config(
            "mcp_graphiti",
            url_override="https://mcp-graphiti.sora.services",
            authorization_token="tok",
        ),
    ])
    payload = reg.to_anthropic_mcp_servers()
    assert [e["name"] for e in payload] == ["mcp_graphiti"]
    assert payload[0]["url"] == "https://mcp-graphiti.sora.services"


def test_policy_map_import_invariants():
    """AC-4 evidence: re-assert the import-time invariants over the live
    map (a genuinely violated invariant makes the module unimportable, so
    this is a pin of the contract, not a can-fail-gracefully check)."""
    assert set(MCP_SERVER_POLICY) == {
        "claude_ai_Figma",
        "claude_ai_Gmail",
        "claude_ai_Google_Calendar",
        "claude_ai_Google_Drive",
        "mcp_jira",
        "mcp_graphiti",
    }
    for name, policy in MCP_SERVER_POLICY.items():
        if policy.action == "forward":
            assert policy.allowed_tools, name
            assert policy.allowed_url_prefixes, name
        else:
            assert policy.action == "deny", name
            assert policy.allowed_tools is None, name
            assert policy.allowed_url_prefixes is None, name

    # The three Google-suite MCPs are explicit-deny; the rest forward.
    denied = {n for n, p in MCP_SERVER_POLICY.items() if p.action == "deny"}
    assert denied == {
        "claude_ai_Gmail",
        "claude_ai_Google_Calendar",
        "claude_ai_Google_Drive",
    }


# ─── enforce_mcp_policy (direct unit tests over raw dicts) ───────


def _raw_entry(name: str, url: str, token: str = "tok") -> dict:
    return {"type": "url", "url": url, "name": name, "authorization_token": token}


def test_enforce_mcp_policy_drops_unknown_and_denied_keeps_forward():
    servers = [
        _raw_entry("claude_ai_Figma", "https://mcp.anthropic.com/v1/integrations/figma"),
        _raw_entry("claude_ai_Gmail", "https://mcp.anthropic.com/v1/integrations/gmail"),
        _raw_entry("totally_unknown", "https://whatever.example/mcp"),
    ]
    out = enforce_mcp_policy(servers)
    assert [e["name"] for e in out] == ["claude_ai_Figma"]
    assert out[0]["tool_configuration"] == {
        "allowed_tools": sorted(FIGMA_MCP_READ_ONLY_TOOLS),
    }


def test_enforce_mcp_policy_reinjects_allowlist_over_caller_supplied():
    """A caller-supplied tool_configuration is overwritten by the policy's
    exact allowlist — the payload cannot smuggle a wider surface."""
    entry = _raw_entry("mcp_jira", "https://mcp-atlassian.local/jira")
    entry["tool_configuration"] = {
        "allowed_tools": ["transitionTicket", "deleteTicket"],
    }
    out = enforce_mcp_policy([entry])
    assert len(out) == 1
    assert out[0]["tool_configuration"] == {
        "allowed_tools": ["getComments", "getTicket", "searchTickets"],
    }
    # Input dict not mutated.
    assert entry["tool_configuration"]["allowed_tools"] == [
        "transitionTicket", "deleteTicket",
    ]


def test_enforce_mcp_policy_origin_mismatch_dropped():
    out = enforce_mcp_policy([
        _raw_entry("mcp_graphiti", "https://evil.example/mcp"),
    ])
    assert out == []


def test_enforce_mcp_policy_host_suffix_impostor_dropped():
    """Delimiter-safe pinning: naive startswith would accept a host whose
    name merely EXTENDS the pinned host. It must be refused."""
    impostor = "https://mcp-graphiti.localhost.evil.example/mcp"
    assert impostor.startswith("https://mcp-graphiti.local")  # the trap
    out = enforce_mcp_policy([_raw_entry("mcp_graphiti", impostor)])
    assert out == []

    # The genuine pinned origins still pass, with and without a path slash.
    for genuine in (
        "https://mcp-graphiti.local",
        "https://mcp-graphiti.local/",
        "https://mcp-graphiti.sora.services",
    ):
        out = enforce_mcp_policy([_raw_entry("mcp_graphiti", genuine)])
        assert [e["name"] for e in out] == ["mcp_graphiti"]


def test_enforce_mcp_policy_preserves_input_order():
    servers = [
        _raw_entry("mcp_jira", "https://mcp-atlassian.local/jira"),
        _raw_entry("claude_ai_Figma", "https://mcp.anthropic.com/v1/integrations/figma"),
        _raw_entry("mcp_graphiti", "https://mcp-graphiti.local"),
    ]
    out = enforce_mcp_policy(servers)
    assert [e["name"] for e in out] == [
        "mcp_jira", "claude_ai_Figma", "mcp_graphiti",
    ]


# ─── parse_mcp_tool_name ─────────────────────────────────────────


def test_parse_valid_mcp_tool_name():
    assert parse_mcp_tool_name("mcp__claude_ai_Figma__get_design_context") == (
        "claude_ai_Figma", "get_design_context",
    )


def test_parse_handles_underscores_in_method_name():
    """Method names commonly have underscores; double-underscore is the
    server/method separator."""
    assert parse_mcp_tool_name("mcp__claude_ai_Gmail__complete_authentication") == (
        "claude_ai_Gmail", "complete_authentication",
    )


def test_parse_preserves_extra_separators_in_method_name():
    assert parse_mcp_tool_name("mcp__custom_server__tools__list") == (
        "custom_server", "tools__list",
    )


def test_parse_non_mcp_returns_none():
    assert parse_mcp_tool_name("Read") is None
    assert parse_mcp_tool_name("Bash") is None
    assert parse_mcp_tool_name("") is None


def test_parse_malformed_mcp_returns_none():
    """No double-underscore separator → invalid."""
    assert parse_mcp_tool_name("mcp__lonely") is None
    # Empty body after prefix
    assert parse_mcp_tool_name("mcp__") is None


def test_is_mcp_tool_predicate():
    assert is_mcp_tool("mcp__claude_ai_Figma__whoami")
    assert not is_mcp_tool("Read")
    assert not is_mcp_tool("mcp__")


# ─── Integration with AnthropicClient.simple_params ──────────────


class _StubAnthropic:
    def __init__(self, **kwargs):  # noqa: ARG002
        self.messages = None


def _install_stub_sdk(monkeypatch):
    fake = types.ModuleType("anthropic")

    class _Client(_StubAnthropic):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    fake.Anthropic = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub")


def test_simple_params_forwards_mcp_servers(monkeypatch):
    """U6-0 P-PROV-B: simple_params re-validates mcp_servers at the client
    boundary — a policied (Figma) entry survives WITH its tool_configuration
    allowlist; denied (Gmail) and unknown (rogue) entries are dropped."""
    _install_stub_sdk(monkeypatch)
    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    mcp_payload = [
        {
            "type": "url",
            "url": "https://mcp.anthropic.com/v1/integrations/figma",
            "name": "claude_ai_Figma",
            "authorization_token": "tok_figma",
        },
        {
            "type": "url",
            "url": "https://mcp.anthropic.com/v1/integrations/gmail",
            "name": "claude_ai_Gmail",
            "authorization_token": "tok_gmail",
        },
        {"type": "url", "url": "https://evil.example/mcp", "name": "rogue"},
    ]

    params = client.simple_params(
        prompt="render a flowchart",
        tools=["Read"],
        mcp_servers=mcp_payload,
    )
    assert [e["name"] for e in params["mcp_servers"]] == ["claude_ai_Figma"]
    figma = params["mcp_servers"][0]
    assert figma["tool_configuration"] == {
        "allowed_tools": sorted(FIGMA_MCP_READ_ONLY_TOOLS)
    }
    assert figma["authorization_token"] == "tok_figma"


def test_simple_params_drops_rogue_only_payload_entirely(monkeypatch):
    """U6-0 P-PROV-B: when NOTHING survives policy, the mcp_servers param is
    omitted from the batch params dict entirely."""
    _install_stub_sdk(monkeypatch)
    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    params = client.simple_params(
        prompt="hi",
        mcp_servers=[
            {
                "type": "url",
                "url": "https://evil.example",
                "name": "rogue",
                "authorization_token": "tok_rogue",
            }
        ],
    )
    assert "mcp_servers" not in params


def test_simple_params_omits_mcp_servers_when_none(monkeypatch):
    _install_stub_sdk(monkeypatch)
    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    params = client.simple_params(prompt="hi", tools=["Read"])
    assert "mcp_servers" not in params


def test_simple_params_omits_mcp_servers_when_empty_list(monkeypatch):
    """Empty list is treated same as None — don't add an empty key."""
    _install_stub_sdk(monkeypatch)
    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    params = client.simple_params(prompt="hi", mcp_servers=[])
    assert "mcp_servers" not in params


# ─── Integration with AnthropicClient.run_with_tools ─────────────


def _install_capturing_stub_sdk(monkeypatch) -> dict[str, Any]:
    """Stub `anthropic` module whose (shared) messages.create captures kwargs.

    The stub's `.beta.messages` IS the same object as `.messages`, so beta
    routing lands in the same captured dict (including `betas=`).
    """
    fake = types.ModuleType("anthropic")
    captured_kwargs: dict[str, Any] = {}

    class _StubResponse:
        content: list = []
        stop_reason: str = "end_turn"
        usage = None

    class _StubMessages:
        def create(self, **kwargs):
            captured_kwargs.update(kwargs)
            return _StubResponse()

    class _StubClient:
        def __init__(self, **kwargs):  # noqa: ARG002
            self.messages = _StubMessages()
            self.beta = types.SimpleNamespace(messages=self.messages)

    fake.Anthropic = _StubClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub")
    return captured_kwargs


@pytest.mark.asyncio
async def test_run_with_tools_forwards_mcp_servers(monkeypatch):
    """U6-0 P-PROV-B: run_with_tools re-validates hand-rolled RAW dicts at
    the client boundary — the denied Gmail entry is DROPPED and the Figma
    entry passes WITH its tool_configuration allowlist injected."""
    captured_kwargs = _install_capturing_stub_sdk(monkeypatch)

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    mcp_payload = [
        {
            "type": "url",
            "url": "https://mcp.anthropic.com/v1/integrations/figma",
            "name": "claude_ai_Figma",
            "authorization_token": "tok_figma",
        },
        {
            "type": "url",
            "url": "https://mcp.anthropic.com/v1/integrations/gmail",
            "name": "claude_ai_Gmail",
            "authorization_token": "tok_gmail",
        },
    ]

    await client.run_with_tools(
        prompt="design something",
        tools=None,
        mcp_servers=mcp_payload,
    )
    forwarded = captured_kwargs.get("mcp_servers")
    assert forwarded is not None
    assert [e["name"] for e in forwarded] == ["claude_ai_Figma"]
    assert forwarded[0]["tool_configuration"] == {
        "allowed_tools": sorted(FIGMA_MCP_READ_ONLY_TOOLS)
    }
    assert forwarded[0]["authorization_token"] == "tok_figma"


@pytest.mark.asyncio
async def test_run_with_tools_drops_rogue_only_payload_entirely(monkeypatch):
    """U6-0 P-PROV-B: a rogue direct payload is dropped in full — neither
    mcp_servers nor the MCP beta pin reaches the create call."""
    captured_kwargs = _install_capturing_stub_sdk(monkeypatch)

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    await client.run_with_tools(
        prompt="exfiltrate",
        tools=None,
        mcp_servers=[
            {
                "type": "url",
                "url": "https://evil.example",
                "name": "rogue",
                "authorization_token": "tok_rogue",
            }
        ],
    )
    assert "mcp_servers" not in captured_kwargs
    assert "betas" not in captured_kwargs


@pytest.mark.asyncio
async def test_run_with_tools_pins_mcp_connector_beta_merged_with_ctor(monkeypatch):
    """U6-0 P-PROV-B: surviving mcp_servers pin MCP_CONNECTOR_BETA via
    per-request `betas=`, and a ctor beta_headers value SURVIVES alongside
    it (per-request betas= REPLACES the client default header — the merge
    guards against silently dropping e.g. managed-agents)."""
    captured_kwargs = _install_capturing_stub_sdk(monkeypatch)

    from backend.agents.anthropic_native_client import (
        MCP_CONNECTOR_BETA,
        AnthropicClient,
    )

    client = AnthropicClient(beta_headers=["managed-agents-2026-04-01"])
    await client.run_with_tools(
        prompt="design something",
        tools=None,
        mcp_servers=[
            {
                "type": "url",
                "url": "https://mcp.anthropic.com/v1/integrations/figma",
                "name": "claude_ai_Figma",
                "authorization_token": "tok_figma",
            }
        ],
    )
    betas = captured_kwargs.get("betas")
    assert betas is not None
    assert MCP_CONNECTOR_BETA in betas
    assert "managed-agents-2026-04-01" in betas


@pytest.mark.asyncio
async def test_run_with_tools_no_mcp_no_beta_pin(monkeypatch):
    """U6-0 P-PROV-B: without mcp_servers, no `betas=` is added — an
    unconditional ctor-level pin would fail this."""
    captured_kwargs = _install_capturing_stub_sdk(monkeypatch)

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient(beta_headers=["managed-agents-2026-04-01"])
    await client.run_with_tools(prompt="plain call", tools=None)
    assert "betas" not in captured_kwargs
    assert "mcp_servers" not in captured_kwargs


# ─── build_registry_from_env (Phase 1: runner ↔ MCP wiring) ───────


def test_build_registry_from_env_empty_env_returns_empty_registry():
    from backend.agents.mcp_integration import build_registry_from_env

    reg = build_registry_from_env(env={})
    assert len(reg) == 0
    assert reg.to_anthropic_mcp_servers() == []


def test_build_registry_from_env_single_token_creates_one_server():
    from backend.agents.mcp_integration import build_registry_from_env

    reg = build_registry_from_env(
        env={"OMNISIGHT_MCP_FIGMA_TOKEN": "tok-figma-abc"}
    )
    assert len(reg) == 1
    cfg = reg.get("claude_ai_Figma")
    assert cfg is not None
    assert cfg.authorization_token == "tok-figma-abc"
    assert cfg.enabled is True
    payload = reg.to_anthropic_mcp_servers()
    assert payload[0]["name"] == "claude_ai_Figma"
    assert payload[0]["authorization_token"] == "tok-figma-abc"


def test_build_registry_from_env_strips_token_whitespace():
    from backend.agents.mcp_integration import build_registry_from_env

    reg = build_registry_from_env(
        env={"OMNISIGHT_MCP_FIGMA_TOKEN": "  tok-figma-abc  "}
    )
    cfg = reg.get("claude_ai_Figma")
    assert cfg is not None
    assert cfg.authorization_token == "tok-figma-abc"


def test_build_registry_from_env_multiple_tokens():
    from backend.agents.mcp_integration import build_registry_from_env

    reg = build_registry_from_env(
        env={
            "OMNISIGHT_MCP_FIGMA_TOKEN": "tok-figma",
            "OMNISIGHT_MCP_GMAIL_TOKEN": "tok-gmail",
            "OMNISIGHT_MCP_GOOGLE_DRIVE_TOKEN": "tok-drive",
        }
    )
    names = {c.name for c in reg.list_all()}
    assert names == {
        "claude_ai_Figma", "claude_ai_Gmail", "claude_ai_Google_Drive",
    }


def test_build_registry_from_env_skips_empty_tokens():
    from backend.agents.mcp_integration import build_registry_from_env

    reg = build_registry_from_env(
        env={
            "OMNISIGHT_MCP_FIGMA_TOKEN": "real-tok",
            "OMNISIGHT_MCP_GMAIL_TOKEN": "",
            "OMNISIGHT_MCP_GOOGLE_DRIVE_TOKEN": "   ",  # whitespace
        }
    )
    assert len(reg) == 1
    assert reg.get("claude_ai_Figma") is not None
    assert reg.get("claude_ai_Gmail") is None
    assert reg.get("claude_ai_Google_Drive") is None


@pytest.mark.parametrize("disable_value", ["1", "true", "yes", "on", "TRUE"])
def test_build_registry_from_env_master_disable(disable_value):
    from backend.agents.mcp_integration import build_registry_from_env

    reg = build_registry_from_env(
        env={
            "OMNISIGHT_MCP_FIGMA_TOKEN": "would-be-active",
            "OMNISIGHT_MCP_DISABLE_ALL": disable_value,
        }
    )
    assert len(reg) == 0


def test_build_registry_from_env_master_disable_strips_whitespace():
    from backend.agents.mcp_integration import build_registry_from_env

    reg = build_registry_from_env(
        env={
            "OMNISIGHT_MCP_FIGMA_TOKEN": "would-be-active",
            "OMNISIGHT_MCP_DISABLE_ALL": "  yes  ",
        }
    )
    assert len(reg) == 0


def test_build_registry_from_env_disable_off_value_keeps_servers():
    """``OMNISIGHT_MCP_DISABLE_ALL=0`` should NOT disable."""
    from backend.agents.mcp_integration import build_registry_from_env

    reg = build_registry_from_env(
        env={
            "OMNISIGHT_MCP_FIGMA_TOKEN": "tok",
            "OMNISIGHT_MCP_DISABLE_ALL": "0",
        }
    )
    assert len(reg) == 1


def test_build_registry_from_env_uses_os_environ_when_env_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.agents.mcp_integration import build_registry_from_env

    for v in (
        "OMNISIGHT_MCP_FIGMA_TOKEN",
        "OMNISIGHT_MCP_GMAIL_TOKEN",
        "OMNISIGHT_MCP_GOOGLE_CALENDAR_TOKEN",
        "OMNISIGHT_MCP_GOOGLE_DRIVE_TOKEN",
        "OMNISIGHT_MCP_GRAPHITI_TOKEN",
        "OMNISIGHT_MCP_DISABLE_ALL",
    ):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("OMNISIGHT_MCP_FIGMA_TOKEN", "from-env")

    reg = build_registry_from_env()  # no env arg → reads os.environ
    cfg = reg.get("claude_ai_Figma")
    assert cfg is not None
    assert cfg.authorization_token == "from-env"


def test_build_registry_from_env_custom_alias_map():
    from backend.agents.mcp_integration import (
        DEFAULT_REMOTE_MCP_CATALOG,
        build_registry_from_env,
    )

    custom_map = {"claude_ai_Figma": "MY_FIGMA_TOKEN"}
    reg = build_registry_from_env(
        env={"MY_FIGMA_TOKEN": "abc"},
        env_var_by_name=custom_map,
        catalog=DEFAULT_REMOTE_MCP_CATALOG,
    )
    assert len(reg) == 1
    cfg = reg.get("claude_ai_Figma")
    assert cfg is not None
    assert cfg.authorization_token == "abc"


# ─── query_mcp_tool_list ─────────────────────────────────────────


class _ProbeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode()

    def __enter__(self) -> "_ProbeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_query_mcp_tool_list_posts_tools_list_and_extracts_names():
    captured: dict[str, Any] = {}

    def opener(request, timeout):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = types.SimpleNamespace(**json.loads(request.data.decode()))
        return _ProbeResponse(
            """{
              "jsonrpc": "2.0",
              "result": {
                "tools": [
                  {"name": "getTicket"},
                  {"name": "searchTickets"},
                  {"name": 123},
                  "not-a-tool"
                ]
              }
            }"""
        )

    cfg = MCPServerConfig(
        name="mcp_jira",
        url="https://mcp.example/jira",
        authorization_token="tok-jira",
    )
    result = query_mcp_tool_list(cfg, opener=opener, timeout=2.5)

    assert captured["url"] == "https://mcp.example/jira"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 2.5
    assert captured["authorization"] == "Bearer tok-jira"
    assert captured["body"].method == "tools/list"
    assert result.server_name == "mcp_jira"
    assert result.url == "https://mcp.example/jira"
    assert result.tool_names == ("getTicket", "searchTickets")


def test_query_mcp_tool_list_empty_body_returns_empty_result():
    def opener(request, timeout):  # noqa: ANN001, ARG001
        assert request.get_header("Authorization") is None
        return _ProbeResponse("")

    cfg = MCPServerConfig(name="public_mcp", url="https://mcp.example/public")
    result = query_mcp_tool_list(cfg, opener=opener)

    assert result.tool_names == ()
    assert result.raw == {}


def test_query_mcp_tool_list_reraises_http_error():
    err = urllib.error.HTTPError(
        "https://mcp.example/jira",
        503,
        "unavailable",
        hdrs=None,
        fp=None,
    )

    def opener(request, timeout):  # noqa: ANN001, ARG001
        raise err

    cfg = MCPServerConfig(name="mcp_jira", url="https://mcp.example/jira")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        query_mcp_tool_list(cfg, opener=opener)

    assert exc_info.value is err


def test_query_mcp_tool_list_wraps_os_error_without_leaking_token():
    def opener(request, timeout):  # noqa: ANN001, ARG001
        raise TimeoutError("socket timed out")

    cfg = MCPServerConfig(
        name="mcp_jira",
        url="https://mcp.example/jira",
        authorization_token="secret-token",
    )
    with pytest.raises(ConnectionError) as exc_info:
        query_mcp_tool_list(cfg, opener=opener, timeout=1.0)

    message = str(exc_info.value)
    assert "mcp_jira" in message
    assert "timeout=1.0s" in message
    assert "authorization_token=<set>" in message
    assert "secret-token" not in message
    assert isinstance(exc_info.value.__cause__, TimeoutError)
