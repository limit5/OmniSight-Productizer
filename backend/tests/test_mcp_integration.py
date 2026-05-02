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
  - parse_mcp_tool_name: valid prefix → (server, method), invalid → None
  - is_mcp_tool boolean wrapper
  - AnthropicClient.simple_params accepts + forwards mcp_servers
  - AnthropicClient.run_with_tools accepts + forwards mcp_servers per call

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §5.6
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from backend.agents.mcp_integration import (
    DEFAULT_REMOTE_MCP_CATALOG,
    MCPServerConfig,
    RemoteMCPRegistry,
    build_default_server_config,
    default_catalog_by_name,
    is_mcp_tool,
    parse_mcp_tool_name,
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


def test_catalog_has_four_known_servers():
    names = {entry.name for entry in DEFAULT_REMOTE_MCP_CATALOG}
    assert names == {
        "claude_ai_Figma",
        "claude_ai_Gmail",
        "claude_ai_Google_Calendar",
        "claude_ai_Google_Drive",
    }


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
    """Same registry → same payload byte-equal across runs (deterministic)."""
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


def test_anthropic_payload_empty_when_only_names_no_match():
    reg = RemoteMCPRegistry(configs=[_figma_cfg()])
    payload = reg.to_anthropic_mcp_servers(only_names=["nonexistent"])
    assert payload == []


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
    _install_stub_sdk(monkeypatch)
    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    reg = RemoteMCPRegistry(configs=[_figma_cfg()])
    mcp_payload = reg.to_anthropic_mcp_servers()

    params = client.simple_params(
        prompt="render a flowchart",
        tools=["Read"],
        mcp_servers=mcp_payload,
    )
    assert params["mcp_servers"] == mcp_payload
    assert params["mcp_servers"][0]["name"] == "claude_ai_Figma"


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


@pytest.mark.asyncio
async def test_run_with_tools_forwards_mcp_servers(monkeypatch):
    """run_with_tools must pass mcp_servers through to messages.create."""
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

    fake.Anthropic = _StubClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub")

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    reg = RemoteMCPRegistry(configs=[_figma_cfg(), _gmail_cfg()])
    mcp_payload = reg.to_anthropic_mcp_servers()

    await client.run_with_tools(
        prompt="design something",
        tools=None,
        mcp_servers=mcp_payload,
    )
    assert captured_kwargs.get("mcp_servers") == mcp_payload
    assert len(captured_kwargs["mcp_servers"]) == 2


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
