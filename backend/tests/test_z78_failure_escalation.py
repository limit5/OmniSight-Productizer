"""Z.7.8 — unit tests for consecutive-failure counter in live_test_status router.

Tests that the POST handler logic correctly:
  - Increments ``consecutive_failures`` on each fail
  - Resets ``consecutive_failures`` to 0 on pass
  - Exposes ``consecutive_failures`` in the GET response

These tests call the router handler functions directly (bypassing the full
ASGI stack + bootstrap guard) to keep tests fast and dependency-free.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest


# ─── helpers ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clear_kv():
    """Reset the SharedKV namespace before each test for isolation."""
    from backend.shared_state import SharedKV
    kv = SharedKV("llm_live_test_status")
    for field in (
        "status", "consecutive_failures", "timestamp",
        "run_id", "tests_run", "tests_passed", "tests_skipped",
    ):
        kv.set(field, "")
    yield
    # Cleanup after test
    for field in (
        "status", "consecutive_failures", "timestamp",
        "run_id", "tests_run", "tests_passed", "tests_skipped",
    ):
        kv.set(field, "")


def _invoke_post(status: str, run_id: str = "run-1", token: str = "test-token") -> None:
    """Call the POST handler directly with a mocked authorization header."""
    from backend.routers.live_test_status import (
        LiveTestStatusWriteRequest,
        post_live_test_status,
    )
    body = LiveTestStatusWriteRequest(
        status=status,
        run_id=run_id,
        tests_run=3,
        tests_passed=0 if status == "fail" else 3,
    )
    with patch.dict(os.environ, {"OMNISIGHT_REPORTER_TOKEN": token}):
        post_live_test_status(body=body, authorization=f"Bearer {token}")


def _get_consecutive_failures() -> int | None:
    """Read consecutive_failures directly from SharedKV."""
    from backend.shared_state import SharedKV
    kv = SharedKV("llm_live_test_status")
    raw = kv.get("consecutive_failures", "")
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# ─── counter tests ──────────────────────────────────────────────────────────


class TestConsecutiveFailureCounter:
    def test_single_fail_sets_counter_to_1(self):
        _invoke_post("fail", "run-1")
        assert _get_consecutive_failures() == 1

    def test_two_consecutive_fails_reaches_2(self):
        _invoke_post("fail", "run-1")
        _invoke_post("fail", "run-2")
        assert _get_consecutive_failures() == 2

    def test_pass_resets_counter_to_0(self):
        _invoke_post("fail", "run-1")
        _invoke_post("fail", "run-2")
        assert _get_consecutive_failures() == 2
        _invoke_post("pass", "run-3")
        assert _get_consecutive_failures() == 0

    def test_fail_after_reset_starts_at_1(self):
        _invoke_post("fail", "run-1")
        _invoke_post("pass", "run-2")
        _invoke_post("fail", "run-3")
        assert _get_consecutive_failures() == 1

    def test_three_consecutive_fails_reaches_3(self):
        for i in range(3):
            _invoke_post("fail", f"run-{i}")
        assert _get_consecutive_failures() == 3

    def test_pass_after_pass_keeps_counter_0(self):
        _invoke_post("pass", "run-1")
        _invoke_post("pass", "run-2")
        assert _get_consecutive_failures() == 0

    def test_invalid_token_raises(self):
        from fastapi import HTTPException
        from backend.routers.live_test_status import (
            LiveTestStatusWriteRequest,
            post_live_test_status,
        )
        body = LiveTestStatusWriteRequest(status="fail")
        with patch.dict(os.environ, {"OMNISIGHT_REPORTER_TOKEN": "correct-token"}):
            with pytest.raises(HTTPException) as exc_info:
                post_live_test_status(body=body, authorization="Bearer wrong-token")
        assert exc_info.value.status_code == 401
        # Counter must not have been touched
        assert _get_consecutive_failures() is None

    def test_invalid_status_raises(self):
        """Pydantic validation: status must be a string, POST rejects invalid values."""
        from fastapi import HTTPException
        from backend.routers.live_test_status import (
            LiveTestStatusWriteRequest,
            post_live_test_status,
        )
        # LiveTestStatusWriteRequest accepts any string, but the handler checks
        # status in ("pass", "fail") and raises 422.
        body = LiveTestStatusWriteRequest(status="unknown")
        with patch.dict(os.environ, {"OMNISIGHT_REPORTER_TOKEN": "tok"}):
            with pytest.raises(HTTPException) as exc_info:
                post_live_test_status(body=body, authorization="Bearer tok")
        assert exc_info.value.status_code == 422

    def test_no_token_configured_raises_503(self):
        from fastapi import HTTPException
        from backend.routers.live_test_status import (
            LiveTestStatusWriteRequest,
            post_live_test_status,
        )
        body = LiveTestStatusWriteRequest(status="fail")
        env_without_token = {k: v for k, v in os.environ.items()
                             if k != "OMNISIGHT_REPORTER_TOKEN"}
        with patch.dict(os.environ, env_without_token, clear=True):
            with pytest.raises(HTTPException) as exc_info:
                post_live_test_status(body=body, authorization="Bearer tok")
        assert exc_info.value.status_code == 503

    def test_get_response_includes_consecutive_failures(self):
        """GET endpoint returns consecutive_failures field."""
        _invoke_post("fail", "run-1")
        _invoke_post("fail", "run-2")
        from backend.routers.live_test_status import get_live_test_status
        resp = get_live_test_status()
        assert resp.consecutive_failures == 2

    def test_get_response_shows_0_after_pass(self):
        _invoke_post("fail", "run-1")
        _invoke_post("pass", "run-2")
        from backend.routers.live_test_status import get_live_test_status
        resp = get_live_test_status()
        assert resp.consecutive_failures == 0


class TestDebugBotHelpers:
    """Unit tests for the llm_adapter_debug_bot helper functions (no gh CLI)."""

    def test_parse_provider_status_all_pass(self):
        from scripts.llm_adapter_debug_bot import _parse_provider_status

        report = {
            "tests": [
                {"nodeid": "test::TestAnthropicLive::test_tool_call", "outcome": "passed"},
                {"nodeid": "test::TestOpenAILive::test_tool_call", "outcome": "passed"},
                {"nodeid": "test::TestGeminiLive::test_tool_call", "outcome": "passed"},
            ]
        }
        result = _parse_provider_status(report)
        assert result == {"Anthropic": "pass", "OpenAI": "pass", "Gemini": "pass"}

    def test_parse_provider_status_one_fail(self):
        from scripts.llm_adapter_debug_bot import _parse_provider_status

        report = {
            "tests": [
                {"nodeid": "test::TestAnthropicLive::test_tool_call", "outcome": "failed"},
                {"nodeid": "test::TestOpenAILive::test_tool_call", "outcome": "passed"},
                {"nodeid": "test::TestGeminiLive::test_tool_call", "outcome": "skipped"},
            ]
        }
        result = _parse_provider_status(report)
        assert result["Anthropic"] == "fail"
        assert result["OpenAI"] == "pass"
        assert result["Gemini"] == "skip"

    def test_parse_provider_status_no_report(self):
        from scripts.llm_adapter_debug_bot import _parse_provider_status

        assert _parse_provider_status(None) == {}
        assert _parse_provider_status({}) == {}

    def test_pattern_analysis_all_failing(self):
        from scripts.llm_adapter_debug_bot import _pattern_analysis

        cur = {"Anthropic": "fail", "OpenAI": "fail", "Gemini": "fail"}
        text = _pattern_analysis(cur, cur)
        assert "All three providers" in text

    def test_pattern_analysis_isolated_provider(self):
        from scripts.llm_adapter_debug_bot import _pattern_analysis

        cur = {"Anthropic": "fail", "OpenAI": "pass", "Gemini": "pass"}
        prev = {"Anthropic": "fail", "OpenAI": "pass", "Gemini": "pass"}
        text = _pattern_analysis(cur, prev)
        assert "Anthropic" in text
        assert "Isolated" in text

    def test_pattern_analysis_no_data(self):
        from scripts.llm_adapter_debug_bot import _pattern_analysis

        text = _pattern_analysis({}, {})
        assert "No provider-level failures" in text

    def test_rca_checklist_has_anthropic_steps(self):
        from scripts.llm_adapter_debug_bot import _rca_checklist

        text = _rca_checklist(
            {"Anthropic": "fail"}, {"Anthropic": "fail"}, 100, 99
        )
        assert "ANTHROPIC_API_KEY_CI" in text
        assert "status.anthropic.com" in text

    def test_build_body_renders_table(self):
        from scripts.llm_adapter_debug_bot import _build_body

        cur = {"Anthropic": "fail", "OpenAI": "pass", "Gemini": "skip"}
        prev = {"Anthropic": "fail", "OpenAI": "pass", "Gemini": "pass"}
        body = _build_body(200, 199, {}, {}, cur, prev)
        assert "❌ fail" in body
        assert "✅ pass" in body
        assert "⏭️ skip" in body
        assert "Z.7.8" in body
        assert "llm-adapter-debug-bot" in body
