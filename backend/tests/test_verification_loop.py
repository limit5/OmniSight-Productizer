"""Tests for Generator-Verifier closed loop (Phase 22)."""

import pytest


class TestGraphStateVerificationFields:

    def test_defaults(self):
        from backend.agents.state import GraphState
        s = GraphState()
        assert s.verification_loop_iteration == 0
        assert s.max_verification_iterations == 2
        assert s.last_verification_failure == ""

    def test_set_values(self):
        from backend.agents.state import GraphState
        s = GraphState(
            verification_loop_iteration=1,
            last_verification_failure="run_simulation: 2/5 tests failed",
        )
        assert s.verification_loop_iteration == 1
        assert "2/5" in s.last_verification_failure


class TestShouldRetryWithVerification:

    def test_verification_failure_retries(self):
        from backend.agents.nodes import _should_retry
        from backend.agents.state import GraphState
        s = GraphState(
            last_verification_failure="tests failed",
            verification_loop_iteration=1,
            max_verification_iterations=2,
            routed_to="firmware",
        )
        assert _should_retry(s) == "firmware"

    def test_verification_exhausted_goes_to_summarizer(self):
        from backend.agents.nodes import _should_retry
        from backend.agents.state import GraphState
        s = GraphState(
            last_verification_failure="tests failed",
            verification_loop_iteration=3,
            max_verification_iterations=2,
        )
        assert _should_retry(s) == "summarizer"

    def test_tool_error_still_retries(self):
        from backend.agents.nodes import _should_retry
        from backend.agents.state import GraphState
        s = GraphState(
            last_error="run_bash: command not found",
            retry_count=1,
            max_retries=3,
            routed_to="software",
        )
        assert _should_retry(s) == "software"

    def test_loop_breaker_overrides_all(self):
        from backend.agents.nodes import _should_retry
        from backend.agents.state import GraphState
        s = GraphState(
            loop_breaker_triggered=True,
            last_verification_failure="tests failed",
            verification_loop_iteration=1,
        )
        assert _should_retry(s) == "summarizer"

    def test_no_errors_goes_to_summarizer(self):
        from backend.agents.nodes import _should_retry
        from backend.agents.state import GraphState
        s = GraphState()
        assert _should_retry(s) == "summarizer"


class TestErrorCheckNodeVerification:

    def test_fail_prefix_detected(self):
        """[FAIL] in tool output triggers verification loop, not retry loop."""
        from backend.agents.nodes import error_check_node
        from backend.agents.state import GraphState, ToolResult

        state = GraphState(
            tool_results=[
                ToolResult(tool_name="run_simulation", output="[FAIL] 2/5 tests failed", success=True),
            ],
            verification_loop_iteration=0,
            max_verification_iterations=2,
        )
        result = error_check_node(state)
        assert result.get("verification_loop_iteration") == 1
        assert result.get("last_verification_failure")
        assert "run_simulation" in result["last_verification_failure"]

    def test_pass_prefix_not_verification_failure(self):
        """[PASS] should not trigger verification loop."""
        from backend.agents.nodes import error_check_node
        from backend.agents.state import GraphState, ToolResult

        state = GraphState(
            tool_results=[
                ToolResult(tool_name="run_simulation", output="[PASS] 5/5 tests passed", success=True),
            ],
        )
        result = error_check_node(state)
        assert result.get("verification_loop_iteration", 0) == 0
        assert not result.get("last_verification_failure")

    def test_verification_exhausted(self):
        """After max iterations, verification stops."""
        from backend.agents.nodes import error_check_node
        from backend.agents.state import GraphState, ToolResult

        state = GraphState(
            tool_results=[
                ToolResult(tool_name="run_simulation", output="[FAIL] still failing", success=True),
            ],
            verification_loop_iteration=2,
            max_verification_iterations=2,
        )
        result = error_check_node(state)
        # Should NOT increment further; clears failure state for summarizer
        assert result.get("last_verification_failure") == ""
