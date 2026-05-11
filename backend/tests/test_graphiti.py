"""OP-853 (C4) - Graphiti MCP temporal query integration tests."""
from __future__ import annotations

from pathlib import Path

import yaml

from backend.agents import mcp_integration as mcp
from backend.agents.graphiti_mcp_client import (
    GraphitiMCPUnavailable,
    GraphitiWriteRefused,
    TemporalQueryNoMatch,
    dispatch_graphiti_temporal_query,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_env_token_alias_is_omnisight_mcp_graphiti_token() -> None:
    assert (
        mcp.ENV_TOKEN_VAR_BY_NAME.get("mcp_graphiti")
        == "OMNISIGHT_MCP_GRAPHITI_TOKEN"
    )


def test_graphiti_catalog_entry_and_env_registry_with_url_override() -> None:
    catalog = mcp.default_catalog_by_name()
    assert "mcp_graphiti" in catalog
    entry = catalog["mcp_graphiti"]
    assert entry.default_url == "https://mcp-graphiti.local"
    assert set(entry.sample_tools) == {
        "getTicketTimeline",
        "findSimilarPriorTicketsByTimeline",
        "getBotSuccessRateByPattern",
    }

    registry = mcp.build_registry_from_env(
        env={
            "OMNISIGHT_MCP_GRAPHITI_TOKEN": "graphiti-token",
            "OMNISIGHT_MCP_GRAPHITI_URL": "https://graphiti.internal/mcp",
        }
    )
    server = registry.get("mcp_graphiti")
    assert server is not None
    assert server.authorization_token == "graphiti-token"
    assert server.url == "https://graphiti.internal/mcp"


def test_temporal_query_happy_path_uses_graphiti_mcp_tool_prefix() -> None:
    calls: list[tuple[str, dict]] = []

    def fake_dispatcher(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return {
            "key": payload["key"],
            "events": [
                {"type": "status_transition", "from": "To Do", "to": "In Progress"},
                {"type": "bot_activity", "bot": "codex-bot"},
                {"type": "gerrit_review", "event": "change_merged"},
            ],
        }

    result = dispatch_graphiti_temporal_query(
        "mcp__mcp_graphiti__getTicketTimeline",
        {"key": "OP-853"},
        dispatcher=fake_dispatcher,
    )

    assert calls == [("mcp__mcp_graphiti__getTicketTimeline", {"key": "OP-853"})]
    assert result["events"][0]["type"] == "status_transition"
    assert result["events"][1]["type"] == "bot_activity"
    assert result["events"][2]["type"] == "gerrit_review"


def test_graphiti_unavailable_and_no_match_degrade_to_no_context() -> None:
    def unavailable(tool_name: str, payload: dict) -> dict:  # noqa: ARG001
        raise GraphitiMCPUnavailable("server down")

    def no_match(tool_name: str, payload: dict) -> dict:  # noqa: ARG001
        raise TemporalQueryNoMatch("no similar ticket")

    assert (
        dispatch_graphiti_temporal_query(
            "mcp__mcp_graphiti__findSimilarPriorTicketsByTimeline",
            {"features": {"component": "MEDIUM"}},
            dispatcher=unavailable,
        )
        is None
    )
    assert (
        dispatch_graphiti_temporal_query(
            "mcp__mcp_graphiti__findSimilarPriorTicketsByTimeline",
            {"features": {"component": "MEDIUM"}},
            dispatcher=no_match,
        )
        is None
    )


def test_graphiti_read_only_guard_refuses_write_tools() -> None:
    assert mcp.is_graphiti_mcp_read_only_tool(
        "mcp__mcp_graphiti__getTicketTimeline"
    )
    assert mcp.is_graphiti_mcp_read_only_tool(
        "mcp__mcp_graphiti__findSimilarPriorTicketsByTimeline"
    )
    assert mcp.is_graphiti_mcp_read_only_tool("mcp__mcp_graphiti__queryTimeline")
    assert mcp.is_graphiti_mcp_read_only_tool("mcp__mcp_graphiti__listPatterns")

    assert not mcp.is_graphiti_mcp_read_only_tool(
        "mcp__mcp_graphiti__createEpisode"
    )
    assert not mcp.is_graphiti_mcp_read_only_tool(
        "mcp__mcp_graphiti__updateTicketTimeline"
    )
    assert not mcp.is_graphiti_mcp_read_only_tool(
        "mcp__mcp_graphiti__deleteEpisode"
    )
    assert not mcp.is_graphiti_mcp_read_only_tool(
        "mcp__mcp_jira__getTicketTimeline"
    )

    try:
        dispatch_graphiti_temporal_query(
            "mcp__mcp_graphiti__updateTicketTimeline",
            {"key": "OP-853"},
            dispatcher=lambda _tool, _payload: {"unexpected": True},
        )
    except GraphitiWriteRefused:
        pass
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("Graphiti write-shaped tool was not refused")


def test_graphiti_config_documents_ingestion_and_summary_weights() -> None:
    mcp_servers = yaml.safe_load(
        (REPO_ROOT / "config" / "mcp_servers.yaml").read_text(encoding="utf-8")
    )
    graphiti = mcp_servers["servers"]["mcp_graphiti"]
    assert graphiti["token_env"] == "OMNISIGHT_MCP_GRAPHITI_TOKEN"
    assert graphiti["url_env"] == "OMNISIGHT_MCP_GRAPHITI_URL"
    assert graphiti["ingestion"] == {
        "jira_changelog": ["ticket_status_transitions", "bot_activity"],
        "gerrit_stream_events": [
            "patch_set_created",
            "code_review_plus_two",
            "change_merged",
        ],
    }

    weights = yaml.safe_load(
        (REPO_ROOT / "config" / "graphiti_summary_weights.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert set(weights["summaries"]) == {
        "ticket_timeline",
        "similar_prior_tickets",
        "bot_success_pattern",
    }
    assert weights["summaries"]["bot_success_pattern"]["bot_success_rate"] > 1.0
