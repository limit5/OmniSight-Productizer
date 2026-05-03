"""Z.7.2 — Live integration test scaffold for the LLM adapter.

Tests decorated with ``@pytest.mark.live`` hit real provider APIs.
They require CI sandbox keys (low credit / budget-capped):

  ANTHROPIC_API_KEY_CI  — Anthropic Claude sandbox
  OPENAI_API_KEY_CI     — OpenAI sandbox
  GOOGLE_API_KEY_CI     — Google Gemini sandbox

Auto-skipped locally when none of the three keys are present (see
``conftest.py::pytest_collection_modifyitems``).  To run against a
specific provider, export its key and use ``-m live``:

  ANTHROPIC_API_KEY_CI=sk-ant-... pytest -m live -k anthropic

Nightly CI wires all three keys and runs ``pytest -m live``; see
``.github/workflows/llm-live-tests.yml`` (Z.7.7).

This file is the scaffold; Z.7.3–Z.7.6 add the tool-call, multi-turn,
streaming, and nested-schema test classes here.
"""

from __future__ import annotations

import os

import pytest

from backend.llm_adapter import (
    AdapterToolCall,
    AdapterToolResponse,
    build_chat_model,
    invoke_chat,
    tool,
    tool_call,
)

# ── per-provider CI-key helpers ───────────────────────────────────────────────

_KEY_ANTHROPIC = "ANTHROPIC_API_KEY_CI"
_KEY_OPENAI = "OPENAI_API_KEY_CI"
_KEY_GOOGLE = "GOOGLE_API_KEY_CI"


def _ci_key(env_var: str) -> str | None:
    """Return the CI sandbox key or None if the variable is unset/empty."""
    return os.environ.get(env_var, "").strip() or None


def _require_key(env_var: str, provider_display: str) -> str:
    """Return the CI key or skip the calling test if the key is absent.

    The global conftest hook skips *all* live tests when no key at all
    is present; this helper provides per-provider granularity — if only
    one of the three keys is set, tests for the other two skip cleanly.
    """
    key = _ci_key(env_var)
    if key is None:
        pytest.skip(
            f"{provider_display} live test skipped — {env_var!r} not set. "
            "Export the CI sandbox key and re-run with ``pytest -m live``."
        )
    return key  # type: ignore[return-value]  # mypy can't see pytest.skip noreturn


# ── Z.7.3 — shared get_weather tool (all three provider tests) ───────────────


@tool
def get_weather(city: str) -> dict:
    """Get the current weather for a city.

    Args:
        city: The name of the city to get weather for.
    """
    # Body is never executed in tool-call tests — the LLM only emits the
    # call request; we validate the request shape (name / args / id).
    return {"city": city, "temperature": 22, "condition": "sunny"}


# ── Anthropic ─────────────────────────────────────────────────────────────────


@pytest.mark.live
class TestAnthropicLive:
    """Live integration tests against the Anthropic Claude API.

    Uses ``claude-haiku-4-5-20251001`` (cheapest Claude 4 model) with a
    256-token cap to keep per-run cost minimal.  Z.7.3–Z.7.6 extend this
    class with tool-call, multi-turn, streaming, and nested-schema tests.
    """

    # Default to Haiku (cheapest Claude 4) to minimise CI spend.
    _MODEL = "claude-haiku-4-5-20251001"

    def _llm(self, model: str | None = None):
        key = _require_key(_KEY_ANTHROPIC, "Anthropic")
        return build_chat_model(
            "anthropic",
            model or self._MODEL,
            api_key=key,
            max_tokens=256,
        )

    def test_basic_invoke(self):
        """Smoke: Claude returns a non-empty text reply."""
        llm = self._llm()
        result = invoke_chat([("user", "Reply with exactly the word: pong")], llm=llm)
        assert isinstance(result, str), f"expected str, got {type(result)}"
        assert result.strip(), "expected non-empty response from Anthropic"

    def test_tool_call(self):
        """Z.7.3: Anthropic returns a get_weather tool call with correct name/args/id."""
        llm = self._llm()
        resp = tool_call(
            [("user", "What is the current weather in London?")],
            tools=[get_weather],
            llm=llm,
        )
        assert len(resp.tool_calls) >= 1, (
            f"expected ≥1 tool call; got {resp.tool_calls!r}; text={resp.text!r}"
        )
        tc = resp.tool_calls[0]
        assert isinstance(tc, AdapterToolCall)
        assert tc.name == "get_weather", f"expected name='get_weather', got {tc.name!r}"
        assert "city" in tc.arguments, (
            f"expected 'city' key in arguments; got {tc.arguments!r}"
        )
        assert tc.call_id is not None, f"expected non-None call_id; got {tc.call_id!r}"


# ── OpenAI ────────────────────────────────────────────────────────────────────


@pytest.mark.live
class TestOpenAILive:
    """Live integration tests against the OpenAI API.

    Uses ``gpt-4o-mini`` (cost-efficient model) with a 256-token cap.
    Z.7.3–Z.7.6 extend this class with tool-call, multi-turn, streaming,
    and nested-schema tests.
    """

    _MODEL = "gpt-4o-mini"

    def _llm(self, model: str | None = None):
        key = _require_key(_KEY_OPENAI, "OpenAI")
        return build_chat_model(
            "openai",
            model or self._MODEL,
            api_key=key,
            max_tokens=256,
        )

    def test_basic_invoke(self):
        """Smoke: GPT returns a non-empty text reply."""
        llm = self._llm()
        result = invoke_chat([("user", "Reply with exactly the word: pong")], llm=llm)
        assert isinstance(result, str), f"expected str, got {type(result)}"
        assert result.strip(), "expected non-empty response from OpenAI"

    def test_tool_call(self):
        """Z.7.3: OpenAI returns a get_weather tool call with correct name/args/id."""
        llm = self._llm()
        resp = tool_call(
            [("user", "What is the current weather in London?")],
            tools=[get_weather],
            llm=llm,
        )
        assert len(resp.tool_calls) >= 1, (
            f"expected ≥1 tool call; got {resp.tool_calls!r}; text={resp.text!r}"
        )
        tc = resp.tool_calls[0]
        assert isinstance(tc, AdapterToolCall)
        assert tc.name == "get_weather", f"expected name='get_weather', got {tc.name!r}"
        assert "city" in tc.arguments, (
            f"expected 'city' key in arguments; got {tc.arguments!r}"
        )
        assert tc.call_id is not None, f"expected non-None call_id; got {tc.call_id!r}"


# ── Google Gemini ─────────────────────────────────────────────────────────────


@pytest.mark.live
class TestGeminiLive:
    """Live integration tests against the Google Gemini API.

    Uses ``gemini-1.5-flash`` (fast, low-cost model).  Z.7.3–Z.7.6 extend
    this class with tool-call, multi-turn, streaming, and nested-schema
    tests.  Streaming + tool_calls support varies by Gemini model version;
    Z.7.5 will add ``pytest.skip`` guards where needed.
    """

    _MODEL = "gemini-1.5-flash"

    def _llm(self, model: str | None = None):
        key = _require_key(_KEY_GOOGLE, "Google Gemini")
        return build_chat_model(
            "google",
            model or self._MODEL,
            api_key=key,
        )

    def test_basic_invoke(self):
        """Smoke: Gemini returns a non-empty text reply."""
        llm = self._llm()
        result = invoke_chat([("user", "Reply with exactly the word: pong")], llm=llm)
        assert isinstance(result, str), f"expected str, got {type(result)}"
        assert result.strip(), "expected non-empty response from Google Gemini"

    def test_tool_call(self):
        """Z.7.3: Gemini returns a get_weather tool call with correct name/args/id."""
        llm = self._llm()
        resp = tool_call(
            [("user", "What is the current weather in London?")],
            tools=[get_weather],
            llm=llm,
        )
        assert len(resp.tool_calls) >= 1, (
            f"expected ≥1 tool call; got {resp.tool_calls!r}; text={resp.text!r}"
        )
        tc = resp.tool_calls[0]
        assert isinstance(tc, AdapterToolCall)
        assert tc.name == "get_weather", f"expected name='get_weather', got {tc.name!r}"
        assert "city" in tc.arguments, (
            f"expected 'city' key in arguments; got {tc.arguments!r}"
        )
        assert tc.call_id is not None, f"expected non-None call_id; got {tc.call_id!r}"
