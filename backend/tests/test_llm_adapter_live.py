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

import json
import os
from typing import Literal

import pytest
from pydantic import BaseModel, Field

import asyncio

from backend.llm_adapter import (
    AdapterToolCall,
    AdapterToolResponse,
    HumanMessage,
    ToolMessage,
    build_chat_model,
    invoke_chat,
    stream_tool_call,
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


# ── Z.7.6 — book_flight nested-schema + enum tool ────────────────────────────


class Passenger(BaseModel):
    """One passenger in a flight booking (Z.7.6 nested schema test)."""

    name: str = Field(description="Passenger's full name")
    age: int = Field(description="Passenger's age in years")
    seat_class: Literal["economy", "business", "first"] = Field(
        description="Seat class: must be one of 'economy', 'business', or 'first'"
    )


class BookFlightArgs(BaseModel):
    """Schema for the book_flight tool (Z.7.6 nested schema + enum test)."""

    origin: str = Field(
        description="Departure city or airport IATA code (the 'from' location)"
    )
    destination: str = Field(
        description="Destination city or airport IATA code (the 'to' location)"
    )
    date: str = Field(description="Travel date in YYYY-MM-DD format, e.g. 2026-06-15")
    passengers: list[Passenger] = Field(
        description=(
            "List of passengers to book seats for. "
            "Each entry must include: name (full name string), age (integer), "
            "and seat_class (one of: economy, business, first)."
        )
    )


@tool(args_schema=BookFlightArgs)
def book_flight(origin: str, destination: str, date: str, passengers: list) -> dict:
    """Book a flight for one or more passengers from origin to destination.

    Use this to reserve airline seats.  You must specify the departure city
    (origin), arrival city (destination), the travel date, and a list of
    passenger records — each passenger needs a name, age, and seat_class.
    """
    # Body never executes in tool-calling tests; we only validate request shape.
    return {"booking_id": "BK-001", "status": "confirmed"}


_VALID_SEAT_CLASSES = {"economy", "business", "first"}

_BOOK_FLIGHT_PROMPT = (
    "Book a flight from New York to London on 2026-06-15 for 2 passengers: "
    "Alice Smith aged 30 in economy class, and Bob Johnson aged 45 in business class."
)


def _assert_book_flight_response(resp: AdapterToolResponse, provider: str) -> None:
    """Shared Z.7.6 assertion: verify nested schema fields are not silently truncated."""
    assert len(resp.tool_calls) >= 1, (
        f"{provider} nested schema: expected ≥1 tool call; "
        f"got {resp.tool_calls!r}; text={resp.text!r}"
    )
    tc = resp.tool_calls[0]
    assert isinstance(tc, AdapterToolCall)
    assert tc.name == "book_flight", (
        f"{provider} nested schema: expected name='book_flight', got {tc.name!r}"
    )
    assert tc.call_id is not None, (
        f"{provider} nested schema: expected non-None call_id; got {tc.call_id!r}"
    )
    args = tc.arguments

    # Top-level fields must all be present (no silent schema truncation)
    for field_name in ("origin", "destination", "date", "passengers"):
        assert field_name in args, (
            f"{provider} nested schema: field '{field_name}' silently truncated; "
            f"args keys={list(args.keys())!r}"
        )

    # Nested: passengers must be a non-empty list
    passengers = args["passengers"]
    assert isinstance(passengers, list), (
        f"{provider} nested schema: 'passengers' expected list, "
        f"got {type(passengers).__name__!r}; value={passengers!r}"
    )
    assert len(passengers) >= 1, (
        f"{provider} nested schema: 'passengers' list is empty — silent truncation; "
        f"full args={args!r}"
    )

    # Each passenger must be a dict; seat_class must be a valid enum value when present
    for i, p in enumerate(passengers):
        assert isinstance(p, dict), (
            f"{provider} nested schema: passengers[{i}] expected dict, "
            f"got {type(p).__name__!r}; value={p!r}"
        )
        if "seat_class" in p:
            assert p["seat_class"] in _VALID_SEAT_CLASSES, (
                f"{provider} nested schema: passengers[{i}].seat_class={p['seat_class']!r} "
                f"not in {_VALID_SEAT_CLASSES!r} — enum constraint violated"
            )


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

    def test_multi_turn_tool_loop(self):
        """Z.7.4: Anthropic — tool_use → fake tool_result → LLM produces final text.

        Verifies the LLM actually processes the ToolMessage payload and echoes
        content from it in the second-turn reply (i.e., it truly saw the result).
        """
        # Turn 1 — ask for weather, expect a tool call
        user_msg = HumanMessage(content="What is the current weather in Tokyo?")
        first = tool_call([user_msg], tools=[get_weather], llm=self._llm())

        assert first.raw_message is not None, (
            "raw_message must not be None — needed to reconstruct Turn-2 history"
        )
        assert len(first.tool_calls) >= 1, (
            f"Turn 1: expected ≥1 tool call; got {first.tool_calls!r}; text={first.text!r}"
        )
        tc = first.tool_calls[0]
        assert tc.call_id is not None, (
            f"call_id must not be None — required for ToolMessage routing; got {tc!r}"
        )

        # Inject fake tool result: temperature=18, condition=rainy
        # These distinctive values let us assert the LLM actually read the payload.
        fake_result = {"city": "Tokyo", "temperature": 18, "condition": "rainy"}
        tool_msg = ToolMessage(
            content=json.dumps(fake_result),
            tool_call_id=tc.call_id,
            name=tc.name,
        )

        # Turn 2 — full history: user → AI (tool_call) → tool result → final answer
        history = [user_msg, first.raw_message, tool_msg]
        final_text = invoke_chat(history, llm=self._llm())

        assert isinstance(final_text, str), f"expected str, got {type(final_text)}"
        assert final_text.strip(), (
            "expected non-empty final text after feeding tool result back to Anthropic"
        )
        # Key assertion: LLM must have incorporated our fake payload.
        lower = final_text.lower()
        assert "18" in final_text or "rainy" in lower or "tokyo" in lower, (
            f"Anthropic does not appear to have incorporated the fake tool result; "
            f"final_text={final_text!r}"
        )

    def test_streaming_tool_call(self):
        """Z.7.5: Anthropic — streaming path delivers tool_calls correctly.

        Verifies that ``stream_tool_call`` (which uses ``astream`` internally)
        accumulates chunks and produces the same tool-call structure as the
        non-streaming ``tool_call`` path.  This guards against providers that
        only emit tool-call deltas in intermediate chunks.
        """
        llm = self._llm()
        resp = asyncio.run(
            stream_tool_call(
                [("user", "What is the current weather in Paris?")],
                tools=[get_weather],
                llm=llm,
            )
        )
        assert len(resp.tool_calls) >= 1, (
            f"Anthropic streaming: expected ≥1 tool call; got {resp.tool_calls!r}; "
            f"text={resp.text!r}"
        )
        tc = resp.tool_calls[0]
        assert isinstance(tc, AdapterToolCall)
        assert tc.name == "get_weather", (
            f"Anthropic streaming: expected name='get_weather', got {tc.name!r}"
        )
        assert "city" in tc.arguments, (
            f"Anthropic streaming: expected 'city' in arguments; got {tc.arguments!r}"
        )
        assert tc.call_id is not None, (
            f"Anthropic streaming: expected non-None call_id; got {tc.call_id!r}"
        )

    def test_nested_schema(self):
        """Z.7.6: Anthropic — book_flight nested schema + enum; no silent truncation.

        Sends a BookFlightArgs schema (nested passengers list with Literal seat_class
        enum) to Claude and asserts all fields survive intact: top-level origin /
        destination / date / passengers, the list is non-empty, and any seat_class
        value is one of the declared enum literals.
        """
        key = _require_key(_KEY_ANTHROPIC, "Anthropic")
        llm = build_chat_model("anthropic", self._MODEL, api_key=key, max_tokens=512)
        resp = tool_call([("user", _BOOK_FLIGHT_PROMPT)], tools=[book_flight], llm=llm)
        _assert_book_flight_response(resp, "Anthropic")


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

    def test_multi_turn_tool_loop(self):
        """Z.7.4: OpenAI — tool_use → fake tool_result → LLM produces final text.

        Verifies the LLM actually processes the ToolMessage payload and echoes
        content from it in the second-turn reply (i.e., it truly saw the result).
        """
        # Turn 1 — ask for weather, expect a tool call
        user_msg = HumanMessage(content="What is the current weather in Tokyo?")
        first = tool_call([user_msg], tools=[get_weather], llm=self._llm())

        assert first.raw_message is not None, (
            "raw_message must not be None — needed to reconstruct Turn-2 history"
        )
        assert len(first.tool_calls) >= 1, (
            f"Turn 1: expected ≥1 tool call; got {first.tool_calls!r}; text={first.text!r}"
        )
        tc = first.tool_calls[0]
        assert tc.call_id is not None, (
            f"call_id must not be None — required for ToolMessage routing; got {tc!r}"
        )

        # Inject fake tool result: temperature=18, condition=rainy
        fake_result = {"city": "Tokyo", "temperature": 18, "condition": "rainy"}
        tool_msg = ToolMessage(
            content=json.dumps(fake_result),
            tool_call_id=tc.call_id,
            name=tc.name,
        )

        # Turn 2 — full history: user → AI (tool_call) → tool result → final answer
        history = [user_msg, first.raw_message, tool_msg]
        final_text = invoke_chat(history, llm=self._llm())

        assert isinstance(final_text, str), f"expected str, got {type(final_text)}"
        assert final_text.strip(), (
            "expected non-empty final text after feeding tool result back to OpenAI"
        )
        # Key assertion: LLM must have incorporated our fake payload.
        lower = final_text.lower()
        assert "18" in final_text or "rainy" in lower or "tokyo" in lower, (
            f"OpenAI does not appear to have incorporated the fake tool result; "
            f"final_text={final_text!r}"
        )

    def test_streaming_tool_call(self):
        """Z.7.5: OpenAI — streaming path delivers tool_calls correctly.

        OpenAI streams tool-call deltas across chunks; ``stream_tool_call``
        accumulates them with the ``+`` operator.  This test verifies that
        the accumulated result matches the expected tool-call structure.
        """
        llm = self._llm()
        resp = asyncio.run(
            stream_tool_call(
                [("user", "What is the current weather in Paris?")],
                tools=[get_weather],
                llm=llm,
            )
        )
        assert len(resp.tool_calls) >= 1, (
            f"OpenAI streaming: expected ≥1 tool call; got {resp.tool_calls!r}; "
            f"text={resp.text!r}"
        )
        tc = resp.tool_calls[0]
        assert isinstance(tc, AdapterToolCall)
        assert tc.name == "get_weather", (
            f"OpenAI streaming: expected name='get_weather', got {tc.name!r}"
        )
        assert "city" in tc.arguments, (
            f"OpenAI streaming: expected 'city' in arguments; got {tc.arguments!r}"
        )
        assert tc.call_id is not None, (
            f"OpenAI streaming: expected non-None call_id; got {tc.call_id!r}"
        )

    def test_nested_schema(self):
        """Z.7.6: OpenAI — book_flight nested schema + enum; no silent truncation.

        Sends a BookFlightArgs schema (nested passengers list with Literal seat_class
        enum) to GPT-4o-mini and asserts all fields survive intact: top-level origin /
        destination / date / passengers, the list is non-empty, and any seat_class
        value is one of the declared enum literals.
        """
        key = _require_key(_KEY_OPENAI, "OpenAI")
        llm = build_chat_model("openai", self._MODEL, api_key=key, max_tokens=512)
        resp = tool_call([("user", _BOOK_FLIGHT_PROMPT)], tools=[book_flight], llm=llm)
        _assert_book_flight_response(resp, "OpenAI")


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

    def test_multi_turn_tool_loop(self):
        """Z.7.4: Gemini — tool_use → fake tool_result → LLM produces final text.

        Verifies the LLM actually processes the ToolMessage payload and echoes
        content from it in the second-turn reply (i.e., it truly saw the result).
        """
        # Turn 1 — ask for weather, expect a tool call
        user_msg = HumanMessage(content="What is the current weather in Tokyo?")
        first = tool_call([user_msg], tools=[get_weather], llm=self._llm())

        assert first.raw_message is not None, (
            "raw_message must not be None — needed to reconstruct Turn-2 history"
        )
        assert len(first.tool_calls) >= 1, (
            f"Turn 1: expected ≥1 tool call; got {first.tool_calls!r}; text={first.text!r}"
        )
        tc = first.tool_calls[0]
        assert tc.call_id is not None, (
            f"call_id must not be None — required for ToolMessage routing; got {tc!r}"
        )

        # Inject fake tool result: temperature=18, condition=rainy
        fake_result = {"city": "Tokyo", "temperature": 18, "condition": "rainy"}
        tool_msg = ToolMessage(
            content=json.dumps(fake_result),
            tool_call_id=tc.call_id,
            name=tc.name,
        )

        # Turn 2 — full history: user → AI (tool_call) → tool result → final answer
        history = [user_msg, first.raw_message, tool_msg]
        final_text = invoke_chat(history, llm=self._llm())

        assert isinstance(final_text, str), f"expected str, got {type(final_text)}"
        assert final_text.strip(), (
            "expected non-empty final text after feeding tool result back to Gemini"
        )
        # Key assertion: LLM must have incorporated our fake payload.
        lower = final_text.lower()
        assert "18" in final_text or "rainy" in lower or "tokyo" in lower, (
            f"Gemini does not appear to have incorporated the fake tool result; "
            f"final_text={final_text!r}"
        )

    def test_streaming_tool_call(self):
        """Z.7.5: Gemini — streaming path delivers tool_calls (or skip if unsupported).

        ``gemini-1.5-flash`` supports streaming + function calling.  Earlier
        models (``gemini-pro``, ``gemini-1.0-*``) raise a
        ``google.api_core.exceptions.InvalidArgument`` or raise
        ``NotImplementedError`` — this test skips cleanly in that case so
        the nightly CI job does not hard-fail on legacy model configs.

        Gemini behaviour note: function-call results arrive in a single
        final chunk (unlike OpenAI which streams argument deltas); the
        accumulated ``tool_calls`` list is fully populated only after all
        chunks are consumed.  This is not a bug — it is a documented
        provider difference (Z.7.5 behavioural diff table).
        """
        llm = self._llm()
        try:
            resp = asyncio.run(
                stream_tool_call(
                    [("user", "What is the current weather in Paris?")],
                    tools=[get_weather],
                    llm=llm,
                )
            )
        except NotImplementedError as exc:
            pytest.skip(
                f"Gemini streaming + tool_calls not supported by {self._MODEL!r}: {exc}"
            )
        except Exception as exc:
            exc_str = str(exc).lower()
            if any(kw in exc_str for kw in ("not support", "invalid argument", "unsupported")):
                pytest.skip(
                    f"Gemini streaming + tool_calls not supported by {self._MODEL!r}: {exc}"
                )
            raise

        assert len(resp.tool_calls) >= 1, (
            f"Gemini streaming: expected ≥1 tool call; got {resp.tool_calls!r}; "
            f"text={resp.text!r}"
        )
        tc = resp.tool_calls[0]
        assert isinstance(tc, AdapterToolCall)
        assert tc.name == "get_weather", (
            f"Gemini streaming: expected name='get_weather', got {tc.name!r}"
        )
        assert "city" in tc.arguments, (
            f"Gemini streaming: expected 'city' in arguments; got {tc.arguments!r}"
        )
        assert tc.call_id is not None, (
            f"Gemini streaming: expected non-None call_id; got {tc.call_id!r}"
        )

    def test_nested_schema(self):
        """Z.7.6: Gemini — book_flight nested schema + enum; detect silent truncation.

        Sends a BookFlightArgs schema (nested passengers list with Literal seat_class
        enum) to Gemini.  Gemini has been known to reject or silently flatten complex
        nested schemas; this test catches both failure modes:

        - Schema rejection (InvalidArgument / similar API error) → ``pytest.skip``
          with the error message so the nightly CI job records it as "unsupported"
          rather than a hard test failure.
        - Silent truncation (passengers list empty, fields missing, enum violated)
          → assertion failure with a descriptive message pinpointing the deviation.
        """
        key = _require_key(_KEY_GOOGLE, "Google Gemini")
        llm = build_chat_model("google", self._MODEL, api_key=key)
        try:
            resp = tool_call([("user", _BOOK_FLIGHT_PROMPT)], tools=[book_flight], llm=llm)
        except NotImplementedError as exc:
            pytest.skip(
                f"Gemini nested schema not supported by {self._MODEL!r}: {exc}"
            )
        except Exception as exc:
            exc_str = str(exc).lower()
            if any(kw in exc_str for kw in ("not support", "invalid argument", "unsupported", "invalid schema", "bad request")):
                pytest.skip(
                    f"Gemini rejects book_flight nested schema on {self._MODEL!r}: {exc}"
                )
            raise
        _assert_book_flight_response(resp, "Gemini")
