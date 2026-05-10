"""OP-813 (A5) — MCP-JIRA integration tests.

The runner gains a tool surface for asking the ticket graph (parent META,
sibling tickets, AC history) without inlining JIRA blobs into the prompt.
Mutations (transitions, comments) MUST stay in ``jira_dispatch`` so the
audit / governance trail remains single-sourced. These tests pin both
contracts: registration via env, and structural read-only enforcement.
"""
from __future__ import annotations

from backend.agents import mcp_integration as mcp


def test_env_token_alias_is_omnisight_mcp_jira_token() -> None:
    """AC#1: ``OMNISIGHT_MCP_JIRA_TOKEN`` is the documented env var.

    Pinning the alias name here makes the operator-facing contract
    grep-discoverable from tests; renaming the env var is then a
    deliberate, test-breaking change rather than a silent drift.
    """
    assert mcp.ENV_TOKEN_VAR_BY_NAME.get("mcp_jira") == "OMNISIGHT_MCP_JIRA_TOKEN"


def test_jira_catalog_entry_present_with_read_only_sample_tools() -> None:
    """AC#2: catalog advertises only read methods (``getTicket``,
    ``searchTickets``, ``getComments``). No write-shaped sample.
    """
    catalog = mcp.default_catalog_by_name()
    assert "mcp_jira" in catalog, \
        f"mcp_jira missing from default catalog; got {sorted(catalog)}"
    entry = catalog["mcp_jira"]
    assert set(entry.sample_tools) == {"getTicket", "searchTickets", "getComments"}
    # Description must mention the read-only contract so an operator
    # browsing the catalog understands the boundary.
    assert "read" in entry.description.lower()


def test_build_registry_picks_up_jira_token_from_env() -> None:
    """AC#1 (cont.): ``build_registry_from_env`` returns a registry whose
    only entry is the JIRA MCP when only the JIRA token is set.
    """
    env = {"OMNISIGHT_MCP_JIRA_TOKEN": "test-jira-token-XXX"}
    registry = mcp.build_registry_from_env(env=env)
    servers = list(registry.list_all(enabled_only=True))
    assert len(servers) == 1
    assert servers[0].name == "mcp_jira"
    assert servers[0].authorization_token == "test-jira-token-XXX"
    assert servers[0].url == "https://mcp-atlassian.local/jira"


def test_build_registry_honours_jira_url_override() -> None:
    """AC#1 (cont.): operator may point at a self-hosted ``mcp-atlassian``
    instance via ``OMNISIGHT_MCP_JIRA_URL``.
    """
    env = {
        "OMNISIGHT_MCP_JIRA_TOKEN": "test-token",
        "OMNISIGHT_MCP_JIRA_URL": "https://internal.example.com/mcp/jira",
    }
    registry = mcp.build_registry_from_env(env=env)
    servers = list(registry.list_all(enabled_only=True))
    assert len(servers) == 1
    assert servers[0].url == "https://internal.example.com/mcp/jira"


def test_build_registry_skips_jira_when_no_token() -> None:
    """An operator who hasn't completed JIRA OAuth gets a registry without
    the JIRA MCP — empty token = silent skip (no half-configured request
    that fails the whole turn).
    """
    env = {"OMNISIGHT_MCP_JIRA_TOKEN": ""}  # explicitly empty
    registry = mcp.build_registry_from_env(env=env)
    assert len(list(registry.list_all(enabled_only=True))) == 0


def test_jira_url_override_ignored_without_token() -> None:
    """URL override alone (no token) does not enable the JIRA MCP. This
    closes a config-mistake path where an operator sets the URL but
    forgets the token.
    """
    env = {"OMNISIGHT_MCP_JIRA_URL": "https://internal.example.com/mcp/jira"}
    registry = mcp.build_registry_from_env(env=env)
    assert len(list(registry.list_all(enabled_only=True))) == 0


def test_disable_all_takes_precedence_over_jira_token() -> None:
    """``OMNISIGHT_MCP_DISABLE_ALL=1`` blocks the JIRA MCP even when a
    token is present. Useful for deterministic CI runs.
    """
    env = {
        "OMNISIGHT_MCP_JIRA_TOKEN": "real-token",
        "OMNISIGHT_MCP_DISABLE_ALL": "1",
    }
    registry = mcp.build_registry_from_env(env=env)
    assert len(list(registry.list_all(enabled_only=True))) == 0


def test_is_jira_mcp_read_only_tool_accepts_allowlist() -> None:
    """AC#2 + AC#4: the read-only allowlist matches the AC's enumerated
    methods.
    """
    assert mcp.is_jira_mcp_read_only_tool("mcp__mcp_jira__getTicket")
    assert mcp.is_jira_mcp_read_only_tool("mcp__mcp_jira__searchTickets")
    assert mcp.is_jira_mcp_read_only_tool("mcp__mcp_jira__getComments")


def test_is_jira_mcp_read_only_tool_refuses_writes() -> None:
    """AC#4: structural enforcement. A misbehaving server (or compromised
    token) advertising mutation methods is refused before dispatch.
    """
    assert not mcp.is_jira_mcp_read_only_tool("mcp__mcp_jira__transitionTicket")
    assert not mcp.is_jira_mcp_read_only_tool("mcp__mcp_jira__addComment")
    assert not mcp.is_jira_mcp_read_only_tool("mcp__mcp_jira__updateAssignee")
    assert not mcp.is_jira_mcp_read_only_tool("mcp__mcp_jira__deleteTicket")
    assert not mcp.is_jira_mcp_read_only_tool("mcp__mcp_jira__createIssue")


def test_is_jira_mcp_read_only_tool_refuses_other_servers() -> None:
    """The check is namespace-scoped: even read-shaped methods on OTHER
    MCP servers don't pass — this function answers only "is this a
    read-only JIRA call?".
    """
    assert not mcp.is_jira_mcp_read_only_tool("mcp__claude_ai_Figma__getTicket")
    assert not mcp.is_jira_mcp_read_only_tool("mcp__some_other__searchTickets")


def test_is_jira_mcp_read_only_tool_refuses_non_mcp_names() -> None:
    """Plain (non-MCP) tool names short-circuit to False so the check
    composes safely with non-MCP dispatch.
    """
    assert not mcp.is_jira_mcp_read_only_tool("Read")
    assert not mcp.is_jira_mcp_read_only_tool("Bash")
    assert not mcp.is_jira_mcp_read_only_tool("")
    assert not mcp.is_jira_mcp_read_only_tool("getTicket")  # no mcp__ prefix


def test_read_only_allowlist_is_the_whole_contract() -> None:
    """Pin the constant so adding a new method requires intent. Future
    PRs that try to add ``transitionTicket`` etc. to the allowlist will
    fail this test and force a code review on the governance change.
    """
    assert mcp.MCP_JIRA_READ_ONLY_TOOLS == frozenset({
        "getTicket", "searchTickets", "getComments",
    })
