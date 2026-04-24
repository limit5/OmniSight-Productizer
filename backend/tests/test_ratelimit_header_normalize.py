"""Z.1 (#290) checkbox 2 — per-provider rate-limit header name
normalisation.

Scope:

1. ``_PROVIDER_RATELIMIT_HEADERS`` covers the seven providers that
   emit rate-limit headers today (anthropic + six OpenAI-compatible
   family members) and does *not* claim a mapping for providers that
   emit none (Ollama, Google Gemini) — normalize returns ``{}`` for
   those so the downstream SharedKV write (next Z.1 checkbox) skips.
2. ``_normalize_ratelimit_headers`` regularises headers into the
   unified ``{remaining_requests, remaining_tokens, reset_at_ts,
   retry_after_s}`` dict. Parsing handles Anthropic's ISO 8601
   absolute resets, the OpenAI family's duration-string resets
   (``"12ms"`` / ``"1s"`` / ``"1m30s"``), bare numeric fallbacks, and
   integer/HTTP-date ``Retry-After`` values.
3. ``on_llm_end`` propagates the normalised dict onto
   ``self.last_ratelimit_state`` alongside the raw snapshot in
   ``self.last_response_headers`` — a later normalise failure must not
   unseat the raw capture and vice-versa.

Scope boundary: the ``SharedKV("provider_ratelimit")`` write and 60s
TTL live in the *next* Z.1 checkbox and have their own test file —
this one deliberately does not assert any SharedKV state.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pytest

from backend.agents.llm import (
    TokenTrackingCallback,
    _PROVIDER_RATELIMIT_HEADERS,
    _normalize_ratelimit_headers,
    _parse_duration_seconds,
    _parse_int_or_none,
    _parse_reset_value,
    _parse_retry_after_seconds,
)
from backend.llm_adapter import AIMessage, ChatGeneration, LLMResult


def _chat_gen(response_metadata=None, generation_info=None):
    msg = AIMessage(content="", response_metadata=response_metadata or {})
    return ChatGeneration(message=msg, generation_info=generation_info)


def _result(*, llm_output=None, generations=None):
    return LLMResult(
        generations=generations if generations is not None else [[]],
        llm_output=llm_output,
    )


# ─────────────────────────────────────────────────────────────────
# Mapping table shape + coverage regressions
# ─────────────────────────────────────────────────────────────────


class TestMappingTableCoverage:

    def test_seven_rate_limited_providers_present(self):
        """Anthropic + the six OpenAI-compatible family members all
        have a row. Adding a new provider to the adapter surface
        without a mapping row would silently drop its rate-limit
        state on the floor — this lock ensures the operator notices
        if the mapping lags the adapter list."""
        assert set(_PROVIDER_RATELIMIT_HEADERS.keys()) == {
            "anthropic", "openai", "xai", "groq",
            "deepseek", "together", "openrouter",
        }

    def test_ollama_is_absent(self):
        """Ollama is the local-runtime provider — no HTTP = no rate
        limits. Must remain absent so normalize returns ``{}`` for
        it and the downstream SharedKV write skips."""
        assert "ollama" not in _PROVIDER_RATELIMIT_HEADERS

    @pytest.mark.parametrize(
        "provider", sorted(_PROVIDER_RATELIMIT_HEADERS.keys()),
    )
    def test_every_mapping_has_all_four_keys(self, provider):
        """The unified dict contract is four fields. A mapping row
        missing any of them would silently drop a field in the
        normalised output — guard so adapter drift surfaces at CI
        rather than in production."""
        mapping = _PROVIDER_RATELIMIT_HEADERS[provider]
        assert set(mapping.keys()) == {
            "remaining_requests", "remaining_tokens",
            "reset_at", "retry_after",
        }
        for header in mapping.values():
            assert isinstance(header, str) and header


# ─────────────────────────────────────────────────────────────────
# Duration parser (_parse_duration_seconds) — OpenAI-family resets
# ─────────────────────────────────────────────────────────────────


class TestParseDurationSeconds:

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("12ms", 0.012),
            ("0ms", 0.0),
            ("500ms", 0.5),
            ("1s", 1.0),
            ("7s", 7.0),
            ("1.5s", 1.5),
            ("1m", 60.0),
            ("1m30s", 90.0),
            ("2h", 7200.0),
            ("1h30m", 5400.0),
            ("1h30m15s", 5415.0),
            ("  1s  ", 1.0),  # surrounding whitespace tolerated
        ],
    )
    def test_recognised_shapes(self, raw, expected):
        assert _parse_duration_seconds(raw) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "raw",
        [
            "12",           # bare number, no unit — caller's job to fall through
            "",
            "   ",
            None,
            42,
            "abc",
            "1x",
            "s1",           # unit before value
        ],
    )
    def test_unrecognised_returns_none(self, raw):
        assert _parse_duration_seconds(raw) is None


# ─────────────────────────────────────────────────────────────────
# Reset-value parser (_parse_reset_value) — multi-shape
# ─────────────────────────────────────────────────────────────────


class TestParseResetValue:

    def test_iso8601_utc_z(self):
        out = _parse_reset_value("2026-04-24T13:00:00Z")
        expected = datetime(
            2026, 4, 24, 13, 0, 0, tzinfo=timezone.utc,
        ).timestamp()
        assert out == pytest.approx(expected, abs=1e-3)

    def test_iso8601_explicit_offset(self):
        out = _parse_reset_value("2026-04-24T15:00:00+02:00")
        expected = datetime(
            2026, 4, 24, 13, 0, 0, tzinfo=timezone.utc,
        ).timestamp()
        assert out == pytest.approx(expected, abs=1e-3)

    def test_duration_offset_uses_now_fn(self):
        out = _parse_reset_value("7s", now_fn=lambda: 1_000_000.0)
        assert out == pytest.approx(1_000_007.0)

    def test_bare_float_treated_as_seconds_offset(self):
        """OpenAI gateways occasionally strip the unit suffix — ``"30"``
        falls back to now + 30."""
        out = _parse_reset_value("30", now_fn=lambda: 2_000_000.0)
        assert out == pytest.approx(2_000_030.0)

    def test_malformed_returns_none(self):
        assert _parse_reset_value("not-a-timestamp") is None
        assert _parse_reset_value("") is None
        assert _parse_reset_value(None) is None
        assert _parse_reset_value(42) is None


# ─────────────────────────────────────────────────────────────────
# Retry-After parser (_parse_retry_after_seconds)
# ─────────────────────────────────────────────────────────────────


class TestParseRetryAfterSeconds:

    def test_integer_seconds(self):
        assert _parse_retry_after_seconds("30") == 30.0
        assert _parse_retry_after_seconds("0") == 0.0
        assert _parse_retry_after_seconds(5) == 5.0

    def test_float_seconds(self):
        assert _parse_retry_after_seconds("1.5") == 1.5

    def test_negative_clamped_to_zero(self):
        """``max(0, ...)`` so a malformed negative from upstream can't
        produce a past timestamp."""
        assert _parse_retry_after_seconds("-5") == 0.0

    def test_http_date_form(self):
        """RFC 9110 allows HTTP-date form. Cloudflare has been
        observed rewriting integer retry-after into this form."""
        # 30s in the future from a fixed 'now'
        fake_now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        http_date = "Fri, 24 Apr 2026 12:00:30 GMT"
        out = _parse_retry_after_seconds(http_date, now_fn=lambda: fake_now)
        assert out == pytest.approx(30.0, abs=1.0)

    def test_malformed_returns_none(self):
        assert _parse_retry_after_seconds(None) is None
        assert _parse_retry_after_seconds("") is None
        assert _parse_retry_after_seconds("abc") is None


# ─────────────────────────────────────────────────────────────────
# Int coercer (_parse_int_or_none)
# ─────────────────────────────────────────────────────────────────


class TestParseIntOrNone:

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("42", 42),
            ("0", 0),
            ("-5", -5),            # preserved — signals upstream drift
            ("42.0", 42),          # provider-float shape
            (100, 100),
            ("  7  ", 7),
        ],
    )
    def test_coerces(self, raw, expected):
        assert _parse_int_or_none(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [None, "", "abc", "1.2.3", object()],
    )
    def test_bad_input_returns_none(self, raw):
        assert _parse_int_or_none(raw) is None


# ─────────────────────────────────────────────────────────────────
# Anthropic end-to-end normalise
# ─────────────────────────────────────────────────────────────────


class TestAnthropicNormalise:

    def test_full_anthropic_headers(self):
        headers = {
            "anthropic-ratelimit-requests-remaining": "47",
            "anthropic-ratelimit-tokens-remaining": "199876",
            "anthropic-ratelimit-tokens-reset": "2026-04-24T13:00:00Z",
            "retry-after": "0",
            # Noise fields the mapping should ignore.
            "anthropic-ratelimit-requests-limit": "50",
            "anthropic-ratelimit-tokens-limit": "200000",
        }
        out = _normalize_ratelimit_headers("anthropic", headers)
        expected_reset = datetime(
            2026, 4, 24, 13, 0, 0, tzinfo=timezone.utc,
        ).timestamp()
        assert out == {
            "remaining_requests": 47,
            "remaining_tokens": 199876,
            "reset_at_ts": pytest.approx(expected_reset, abs=1e-3),
            "retry_after_s": 0.0,
        }

    def test_partial_anthropic_headers(self):
        """Adapter dropped the tokens-reset — remaining fields still
        normalise, missing one comes through as ``None`` (not 0)."""
        headers = {
            "anthropic-ratelimit-requests-remaining": "5",
            "anthropic-ratelimit-tokens-remaining": "10000",
            # no reset, no retry-after
        }
        out = _normalize_ratelimit_headers("anthropic", headers)
        assert out == {
            "remaining_requests": 5,
            "remaining_tokens": 10000,
            "reset_at_ts": None,
            "retry_after_s": None,
        }


# ─────────────────────────────────────────────────────────────────
# OpenAI + OpenAI-compatible family end-to-end normalise
# ─────────────────────────────────────────────────────────────────


class TestOpenAICompatibleNormalise:

    @pytest.mark.parametrize(
        "provider",
        ["openai", "xai", "groq", "deepseek", "together", "openrouter"],
    )
    def test_all_openai_family_providers_share_schema(self, provider):
        """All six OpenAI-compatible providers normalise identically —
        locks that the mapping table stays aligned across the family.
        If one provider drifts (e.g. Groq starts emitting a bespoke
        header), this test fails loudly and the operator adds a
        provider-specific row rather than silently mis-mapping."""
        headers = {
            "x-ratelimit-remaining-requests": "2500",
            "x-ratelimit-remaining-tokens": "99900",
            "x-ratelimit-reset-tokens": "12ms",
            "retry-after": "2",
            # Noise fields.
            "x-ratelimit-limit-requests": "3000",
            "x-ratelimit-reset-requests": "1s",
        }
        # Bracket the normalize call with real wall-clock reads so we
        # can assert ``reset_at_ts`` lands inside ``[before+0.012,
        # after+0.012]`` — avoids a module-wide monkeypatch on
        # ``time.time`` (which would affect unrelated libraries) and
        # still locks the "duration + now" semantics.
        before = time.time()
        out = _normalize_ratelimit_headers(provider, headers)
        after = time.time()

        assert out["remaining_requests"] == 2500
        assert out["remaining_tokens"] == 99900
        assert out["retry_after_s"] == 2.0
        assert before + 0.012 <= out["reset_at_ts"] <= after + 0.012 + 1e-6


# ─────────────────────────────────────────────────────────────────
# Unknown / empty / case-drift edge cases
# ─────────────────────────────────────────────────────────────────


class TestEdgeCases:

    def test_ollama_returns_empty(self):
        """Ollama has no mapping — normalize degrades to ``{}`` even
        if the caller mistakenly passes a populated OpenAI-shape dict
        (which could happen if a proxy rewrites headers)."""
        headers = {
            "x-ratelimit-remaining-requests": "100",
            "x-ratelimit-remaining-tokens": "50000",
        }
        assert _normalize_ratelimit_headers("ollama", headers) == {}

    def test_unknown_provider_returns_empty(self):
        assert _normalize_ratelimit_headers("totally-fake", {"x": "y"}) == {}

    def test_none_provider_returns_empty(self):
        assert _normalize_ratelimit_headers(None, {"x-ratelimit-remaining-requests": "1"}) == {}

    def test_empty_headers_returns_empty(self):
        assert _normalize_ratelimit_headers("openai", {}) == {}

    def test_none_headers_returns_empty(self):
        assert _normalize_ratelimit_headers("openai", None) == {}

    def test_non_dict_headers_returns_empty(self):
        assert _normalize_ratelimit_headers("openai", "not-a-dict") == {}
        assert _normalize_ratelimit_headers("openai", 42) == {}

    def test_known_provider_but_no_relevant_keys_returns_empty(self):
        """Anthropic mapping + only unrelated keys → all fields ``None``
        → collapses to ``{}`` so the truthiness branch still skips
        the write downstream. Prevents every single chat response
        from poisoning SharedKV with a dict of Nones."""
        headers = {"content-type": "application/json", "server": "cloudflare"}
        assert _normalize_ratelimit_headers("anthropic", headers) == {}

    def test_case_insensitive_lookup(self):
        """SDKs normalise headers to lowercase, but an ``httpx.Headers``
        view preserves case — defensive lower-casing avoids breaking
        the first time an adapter drops the normalise step."""
        headers = {
            "Anthropic-RateLimit-Requests-Remaining": "10",
            "ANTHROPIC-RATELIMIT-TOKENS-REMAINING": "50000",
            "Anthropic-Ratelimit-Tokens-Reset": "2026-04-24T13:00:00Z",
            "Retry-After": "0",
        }
        out = _normalize_ratelimit_headers("anthropic", headers)
        assert out["remaining_requests"] == 10
        assert out["remaining_tokens"] == 50000
        assert out["reset_at_ts"] is not None
        assert out["retry_after_s"] == 0.0

    def test_malformed_field_value_degrades_to_none(self):
        """A provider emitting a malformed individual field (e.g. the
        remaining count as ``"N/A"`` during a gateway hiccup) must not
        crash the normalise — other fields still come through."""
        headers = {
            "x-ratelimit-remaining-requests": "N/A",
            "x-ratelimit-remaining-tokens": "9000",
            "x-ratelimit-reset-tokens": "not-a-duration",
            "retry-after": "not-a-number",
        }
        out = _normalize_ratelimit_headers("openai", headers)
        assert out == {
            "remaining_requests": None,
            "remaining_tokens": 9000,
            "reset_at_ts": None,
            "retry_after_s": None,
        }


# ─────────────────────────────────────────────────────────────────
# on_llm_end wiring — ``self.last_ratelimit_state`` populated
# ─────────────────────────────────────────────────────────────────


class TestOnLlmEndRatelimitStateSnapshot:

    def test_last_ratelimit_state_initially_empty(self):
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        assert cb.last_ratelimit_state == {}

    def test_on_llm_end_populates_ratelimit_state_for_anthropic(
        self, monkeypatch,
    ):
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        headers = {
            "anthropic-ratelimit-requests-remaining": "47",
            "anthropic-ratelimit-tokens-remaining": "199876",
            "anthropic-ratelimit-tokens-reset": "2026-04-24T13:00:00Z",
            "retry-after": "0",
        }
        gen = _chat_gen(response_metadata={"headers": headers})
        res = LLMResult(
            generations=[[gen]],
            llm_output={"token_usage": {"input_tokens": 10, "output_tokens": 5}},
        )
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        cb.on_llm_end(res)

        assert cb.last_ratelimit_state["remaining_requests"] == 47
        assert cb.last_ratelimit_state["remaining_tokens"] == 199876
        assert cb.last_ratelimit_state["retry_after_s"] == 0.0
        # reset_at_ts is populated (ISO 8601 parse).
        assert isinstance(cb.last_ratelimit_state["reset_at_ts"], float)
        # Raw snapshot is also set — the two attributes are
        # independent; neither can mask a failure on the other side.
        assert cb.last_response_headers == headers

    def test_on_llm_end_ollama_leaves_ratelimit_state_empty(
        self, monkeypatch,
    ):
        """Ollama response has no headers *and* provider is unmapped —
        normalise short-circuits to ``{}`` twice over."""
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        res = LLMResult(
            generations=[[]],
            llm_output={"token_usage": {"prompt_tokens": 20, "completion_tokens": 10}},
        )
        cb = TokenTrackingCallback("llama3.1", provider="ollama")
        cb.on_llm_start()
        cb.on_llm_end(res)
        assert cb.last_ratelimit_state == {}
        assert cb.last_response_headers == {}

    def test_on_llm_end_normalise_failure_leaves_state_empty(
        self, monkeypatch, caplog,
    ):
        """Simulate a normalise bug — the raw snapshot must still land,
        ``last_ratelimit_state`` degrades to ``{}``, and a debug log
        records the drift so the operator can spot it."""
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        def boom(_provider, _headers):
            raise RuntimeError("simulated normaliser drift")

        monkeypatch.setattr(
            "backend.agents.llm._normalize_ratelimit_headers", boom,
        )

        headers = {
            "anthropic-ratelimit-requests-remaining": "47",
        }
        gen = _chat_gen(response_metadata={"headers": headers})
        res = LLMResult(
            generations=[[gen]],
            llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        with caplog.at_level(logging.DEBUG, logger="backend.agents.llm"):
            cb.on_llm_end(res)
        # Raw snapshot still captured.
        assert cb.last_response_headers == headers
        # Normalise branch degraded safely.
        assert cb.last_ratelimit_state == {}
        # Drift recorded at debug level.
        assert any(
            "rate-limit header normalisation skipped" in rec.message
            for rec in caplog.records
        )

    def test_on_llm_end_second_call_overwrites_ratelimit_state(
        self, monkeypatch,
    ):
        """Consecutive on_llm_end must overwrite, not merge — rate-limit
        state is point-in-time."""
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        first = {
            "anthropic-ratelimit-requests-remaining": "100",
            "anthropic-ratelimit-tokens-remaining": "10000",
        }
        second = {
            "anthropic-ratelimit-requests-remaining": "99",
        }

        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        cb.on_llm_end(_result(llm_output={"headers": first}))
        assert cb.last_ratelimit_state["remaining_requests"] == 100
        assert cb.last_ratelimit_state["remaining_tokens"] == 10000

        cb.on_llm_start()
        cb.on_llm_end(_result(llm_output={"headers": second}))
        assert cb.last_ratelimit_state["remaining_requests"] == 99
        # Tokens absent in second turn → ``None`` (not stale 10000).
        assert cb.last_ratelimit_state["remaining_tokens"] is None
