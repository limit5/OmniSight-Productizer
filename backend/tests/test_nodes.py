"""Tests for backend/agents/nodes.py — routing, tool extraction, error check."""

from __future__ import annotations

import pytest

from backend.agents.nodes import (
    _rule_based_route,
    _rule_based_tool_calls,
    error_check_node,
    _should_retry,
)
from backend.agents.state import GraphState, ToolResult


# ─── Rule-based routing ───


class TestRuleBasedRoute:

    def test_firmware_keywords(self):
        primary, _ = _rule_based_route("write a UVC driver for IMX335 sensor")
        assert primary == "firmware"

    def test_software_keywords(self):
        primary, _ = _rule_based_route("refactor the algorithm module")
        assert primary == "software"

    def test_validator_keywords(self):
        primary, _ = _rule_based_route("run test suite and check coverage")
        assert primary == "validator"

    def test_reporter_keywords(self):
        primary, _ = _rule_based_route("generate FCC compliance report")
        assert primary == "reporter"

    def test_no_match_returns_general(self):
        primary, secondary = _rule_based_route("hello world")
        assert primary == "general"
        assert secondary == []

    def test_highest_score_wins(self):
        primary, _ = _rule_based_route("firmware driver sensor test")
        assert primary == "firmware"

    def test_compound_returns_secondary(self):
        primary, secondary = _rule_based_route("write firmware driver and run tests")
        assert primary in ("firmware", "validator")
        assert len(secondary) >= 1


# ─── Rule-based tool extraction ───


class TestRuleBasedToolCalls:

    def test_read_file(self):
        calls = _rule_based_tool_calls("read file src/main.c")
        assert any(tc.tool_name == "read_file" for tc in calls)
        match = next(tc for tc in calls if tc.tool_name == "read_file")
        assert match.arguments["path"] == "src/main.c"

    def test_cat_file(self):
        calls = _rule_based_tool_calls("cat config.yaml")
        assert any(tc.tool_name == "read_file" for tc in calls)

    def test_git_status(self):
        calls = _rule_based_tool_calls("git status")
        assert any(tc.tool_name == "git_status" for tc in calls)

    def test_git_log(self):
        calls = _rule_based_tool_calls("git log")
        assert any(tc.tool_name == "git_log" for tc in calls)

    def test_list_directory(self):
        calls = _rule_based_tool_calls("ls src/")
        assert any(tc.tool_name == "list_directory" for tc in calls)

    def test_run_command(self):
        calls = _rule_based_tool_calls("run make -j4")
        assert any(tc.tool_name == "run_bash" for tc in calls)

    def test_make_command(self):
        calls = _rule_based_tool_calls("make clean")
        assert any(tc.tool_name == "run_bash" for tc in calls)

    def test_no_match(self):
        calls = _rule_based_tool_calls("explain the architecture")
        assert len(calls) == 0

    def test_search_pattern(self):
        calls = _rule_based_tool_calls("search 'init_sensor' in src/")
        assert any(tc.tool_name == "search_in_files" for tc in calls)

    def test_yaml_parse(self):
        calls = _rule_based_tool_calls("parse config.yaml")
        assert any(tc.tool_name == "read_yaml" for tc in calls)


# ─── Error check node (self-healing) ───


class TestErrorCheckNode:

    @pytest.mark.asyncio
    async def test_no_errors_passes_through(self):
        state = GraphState(
            tool_results=[
                ToolResult(tool_name="read_file", output="file content", success=True),
            ],
            retry_count=0,
            max_retries=3,
        )
        update = await error_check_node(state)
        assert update["last_error"] == ""
        assert update["rtk_bypass"] is False

    @pytest.mark.asyncio
    async def test_error_triggers_retry(self):
        state = GraphState(
            tool_results=[
                ToolResult(tool_name="read_file", output="[ERROR] File not found", success=False),
            ],
            retry_count=0,
            max_retries=2,
        )
        update = await error_check_node(state)
        assert update["retry_count"] == 1
        assert "read_file" in update["last_error"]
        assert update["tool_calls"] == []
        assert update["tool_results"] == []

    @pytest.mark.asyncio
    async def test_retries_exhausted_escalates(self):
        """When retries exhausted with errors, escalate to human."""
        state = GraphState(
            routed_to="firmware",
            tool_results=[
                ToolResult(tool_name="run_bash", output="[ERROR] compile failed", success=False),
            ],
            retry_count=3,
            max_retries=3,
        )
        update = await error_check_node(state)
        assert update["last_error"] == ""
        assert len(update["actions"]) == 1
        assert update["actions"][0].status == "awaiting_confirmation"

    @pytest.mark.asyncio
    async def test_retries_exhausted_no_errors_passes_through(self):
        """When retries exhausted but no errors, go to summarizer and reset bypass."""
        state = GraphState(
            tool_results=[
                ToolResult(tool_name="read_file", output="ok", success=True),
            ],
            retry_count=3,
            max_retries=3,
        )
        update = await error_check_node(state)
        assert update["last_error"] == ""
        assert update["rtk_bypass"] is False

    def test_should_retry_routes_to_specialist(self):
        """After error_check sets last_error, should retry the specialist."""
        state = GraphState(
            routed_to="firmware",
            tool_results=[],
            last_error="run_bash: [ERROR] compile failed",
            retry_count=1,
            max_retries=2,
        )
        assert _should_retry(state) == "firmware"

    def test_should_retry_routes_to_summarizer_on_success(self):
        """After error_check clears last_error (no errors), go to summarizer."""
        state = GraphState(
            routed_to="firmware",
            tool_results=[],
            last_error="",
            retry_count=0,
            max_retries=2,
        )
        assert _should_retry(state) == "summarizer"

    def test_should_retry_routes_to_summarizer_when_exhausted(self):
        """When retries exhausted, error_check clears last_error → summarizer."""
        state = GraphState(
            routed_to="firmware",
            tool_results=[],
            last_error="",
            retry_count=3,
            max_retries=3,
        )
        assert _should_retry(state) == "summarizer"
