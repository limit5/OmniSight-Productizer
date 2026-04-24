"""Z.1 (#290) checkbox 5 — four-provider end-to-end capture +
``SharedKV("provider_ratelimit")`` persistence + 60 s TTL expiry.

Scope: exercise the *full* ``TokenTrackingCallback.on_llm_end`` rate-
limit pipeline — raw-header extraction → per-provider normalisation →
``SharedKV.set_with_ttl`` mirror — against realistic-shaped mock
response headers from the four providers Z.2/Z.4 will consume:

- **Anthropic** (``anthropic-ratelimit-*`` + RFC 3339 absolute reset)
- **OpenAI** (``x-ratelimit-*`` + duration-string reset ``"12ms"`` etc.)
- **DeepSeek** (``x-ratelimit-*`` family, numeric-seconds reset)
- **OpenRouter** (``x-ratelimit-*`` family, OpenAI-style duration reset)

Scope boundary vs. the other four Z.1 test files:

- ``test_ratelimit_header_extract.py`` — tests the 5-path extract walk
  in isolation.
- ``test_ratelimit_header_normalize.py`` — tests the mapping table +
  per-parser helpers + unified dict shape in isolation.
- ``test_ratelimit_kv_write.py`` — tests the ``SharedKV`` TTL
  primitives + ``on_llm_end`` wiring in isolation.
- ``test_ratelimit_boundaries.py`` — locks the three graceful-fallback
  guarantees (header missing / unknown provider / LangChain path
  drift).

This file sits on top of all four: it does **not** re-prove the
primitives, it proves the *combined* pipeline for the four providers
Z.2 will read from, and pins the TTL boundary at ``60 s`` end-to-end
(write at t=1000 → live at t+30 s → pruned at t+61 s).

Scenarios:

1. **Per-provider normalise correctness** — four parametrised cases
   each fire an ``on_llm_end`` with vendor-shaped headers and assert
   the normalised dict matches the unified ``{remaining_requests,
   remaining_tokens, reset_at_ts, retry_after_s}`` contract.
2. **Per-provider SharedKV persistence** — the same parametrisation
   additionally verifies the normalised dict landed under
   ``SharedKV("provider_ratelimit")[provider]`` with the stored
   payload identical to ``cb.last_ratelimit_state``.
3. **Four providers coexist in one KV hash** — a single test fires
   four sequential turns (one per provider) and asserts all four keys
   are live, independent, and carry the correct normalised payload.
4. **60 s TTL expiry end-to-end** — writes with frozen clock at
   t=1000, asserts live at t+30 s, pruned at t+61 s. Exercised for
   *all four* providers to lock the contract that the TTL is the
   same across every vendor mapping (not special-cased per provider).
5. **Unknown provider skipped end-to-end** — Ollama + Google Gemini
   turns leave the KV untouched even when the sibling providers
   already have live entries (the dashboard must not lose Anthropic's
   state because an Ollama turn happened in between).
"""

from __future__ import annotations

import time

import pytest

import backend.agents.llm as _llm_mod
from backend.agents.llm import (
    _RATELIMIT_KV_NAMESPACE,
    _RATELIMIT_TTL_SECONDS,
    TokenTrackingCallback,
)
from backend.llm_adapter import AIMessage, ChatGeneration, LLMResult


# ─────────────────────────────────────────────────────────────────
# Fixtures & helpers
# ─────────────────────────────────────────────────────────────────


def _chat_gen(response_metadata=None, generation_info=None):
    msg = AIMessage(content="", response_metadata=response_metadata or {})
    return ChatGeneration(message=msg, generation_info=generation_info)


def _result_with_headers(headers: dict | None):
    """Build an ``LLMResult`` with ``response_metadata['headers']``
    set — the path ``_extract_response_headers`` finds on hit #3
    (``generations[0][0].message.response_metadata['headers']``)
    which is the most common live-traffic shape across the four
    target providers."""
    gen = _chat_gen(response_metadata={"headers": headers or {}})
    return LLMResult(
        generations=[[gen]],
        llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
    )


@pytest.fixture
def silent_track_tokens(monkeypatch):
    """Stub the downstream token-usage pipeline so this file exercises
    only the rate-limit branch — a DB write / event emit inside
    ``track_tokens`` would mask a pipeline regression behind an
    unrelated assertion."""
    import backend.routers.system as _sys
    calls = []
    monkeypatch.setattr(
        _sys, "track_tokens",
        lambda *a, **kw: calls.append((a, kw)),
    )
    return calls


@pytest.fixture
def fresh_kv(monkeypatch):
    """Cycle the module-level SharedKV singleton and purge in-memory
    residue so each test sees a clean ``provider_ratelimit`` namespace.
    Mirrors the fixture in ``test_ratelimit_kv_write.py`` — duplicated
    here to keep this file self-contained and runnable in isolation."""
    monkeypatch.setattr(_llm_mod, "_ratelimit_kv_singleton", None)
    kv = _llm_mod._get_ratelimit_kv()
    for field in list(kv.get_all().keys()):
        kv.delete(field)
    return kv


# ─────────────────────────────────────────────────────────────────
# Per-provider fixtures — each tuple is
# (provider_id, model_name, headers, expected_subset_or_assert)
# The expected payload is a subset — ``reset_at_ts`` needs float
# assertion rather than equality because OpenAI-family duration
# strings anchor to ``time.time()`` at parse time (wall-clock
# dependent). ``remaining_requests`` / ``remaining_tokens`` /
# ``retry_after_s`` are deterministic equality.
# ─────────────────────────────────────────────────────────────────


ANTHROPIC_HEADERS = {
    # Live sample shape from Anthropic's public API — ISO 8601 UTC.
    "anthropic-ratelimit-requests-limit": "1000",
    "anthropic-ratelimit-requests-remaining": "987",
    "anthropic-ratelimit-requests-reset": "2026-04-24T13:00:00Z",
    "anthropic-ratelimit-tokens-limit": "400000",
    "anthropic-ratelimit-tokens-remaining": "387654",
    "anthropic-ratelimit-tokens-reset": "2026-04-24T13:00:00Z",
    "retry-after": "0",
}

OPENAI_HEADERS = {
    # Live sample shape from OpenAI's public API — duration strings.
    "x-ratelimit-limit-requests": "10000",
    "x-ratelimit-remaining-requests": "9998",
    "x-ratelimit-reset-requests": "8.64s",
    "x-ratelimit-limit-tokens": "2000000",
    "x-ratelimit-remaining-tokens": "1999512",
    "x-ratelimit-reset-tokens": "14ms",
    "retry-after": "0",
}

DEEPSEEK_HEADERS = {
    # DeepSeek exposes the x-ratelimit-* family with bare numeric
    # seconds (no unit suffix) on the reset slot — a shape the
    # normalise helper's ``_parse_reset_value`` falls through to
    # after the ISO hint and duration-string branches miss.
    "x-ratelimit-remaining-requests": "450",
    "x-ratelimit-remaining-tokens": "1750000",
    "x-ratelimit-reset-tokens": "30",
    "retry-after": "0",
}

OPENROUTER_HEADERS = {
    # OpenRouter proxies the OpenAI family schema but sometimes
    # emits the reset with a larger duration unit (free-tier buckets
    # reset per-minute).
    "x-ratelimit-remaining-requests": "300",
    "x-ratelimit-remaining-tokens": "120000",
    "x-ratelimit-reset-tokens": "1m",
    "retry-after": "0",
}


PROVIDER_CASES = [
    pytest.param(
        "anthropic", "claude-opus-4-7", ANTHROPIC_HEADERS,
        {"remaining_requests": 987, "remaining_tokens": 387654,
         "retry_after_s": 0.0},
        id="anthropic-native-iso8601",
    ),
    pytest.param(
        "openai", "gpt-4o", OPENAI_HEADERS,
        {"remaining_requests": 9998, "remaining_tokens": 1999512,
         "retry_after_s": 0.0},
        id="openai-duration-string",
    ),
    pytest.param(
        "deepseek", "deepseek-chat", DEEPSEEK_HEADERS,
        {"remaining_requests": 450, "remaining_tokens": 1750000,
         "retry_after_s": 0.0},
        id="deepseek-numeric-seconds",
    ),
    pytest.param(
        "openrouter", "anthropic/claude-opus-4-7", OPENROUTER_HEADERS,
        {"remaining_requests": 300, "remaining_tokens": 120000,
         "retry_after_s": 0.0},
        id="openrouter-minute-duration",
    ),
]


# ─────────────────────────────────────────────────────────────────
# 1. Per-provider end-to-end normalise correctness
# ─────────────────────────────────────────────────────────────────


class TestFourProviderNormalise:

    @pytest.mark.parametrize(
        "provider,model,headers,expected", PROVIDER_CASES,
    )
    def test_on_llm_end_normalises_per_provider_shape(
        self, silent_track_tokens, fresh_kv,
        provider, model, headers, expected,
    ):
        """Realistic vendor headers → unified normalised dict with
        ``remaining_requests`` / ``remaining_tokens`` / ``retry_after_s``
        carrying the exact integer / float values and ``reset_at_ts``
        present as a float epoch (wall-clock anchored — assert shape
        rather than value)."""
        cb = TokenTrackingCallback(model, provider=provider)
        cb.on_llm_start()
        cb.on_llm_end(_result_with_headers(headers))

        state = cb.last_ratelimit_state
        assert state, f"{provider} produced empty ratelimit_state"
        for k, v in expected.items():
            assert state[k] == v, (
                f"{provider}.{k}: expected {v!r}, got {state[k]!r}"
            )
        # reset_at_ts must be a float epoch, regardless of whether the
        # source was ISO 8601 (absolute) or a duration string (relative
        # to now). Non-None + float guards the unified-type contract.
        assert state["reset_at_ts"] is not None
        assert isinstance(state["reset_at_ts"], float)
        # The raw snapshot must coexist — a normalise failure must not
        # unseat the raw capture and vice-versa (Z.1 checkbox 4 triple-
        # independent-try contract).
        assert cb.last_response_headers, (
            f"{provider} raw headers lost alongside normalise"
        )
        # track_tokens fired exactly once — the rate-limit branch did
        # not shadow or multiply the token-accounting call.
        assert len(silent_track_tokens) == 1


# ─────────────────────────────────────────────────────────────────
# 2. Per-provider end-to-end SharedKV persistence
# ─────────────────────────────────────────────────────────────────


class TestFourProviderSharedKVWrite:

    @pytest.mark.parametrize(
        "provider,model,headers,expected", PROVIDER_CASES,
    )
    def test_ratelimit_state_landed_under_provider_key(
        self, silent_track_tokens, fresh_kv,
        provider, model, headers, expected,
    ):
        """Normalised dict → ``SharedKV("provider_ratelimit")[provider]``
        mirror. The stored payload must equal ``cb.last_ratelimit_state``
        byte-for-byte — the KV is not allowed to diverge from the
        in-memory mirror."""
        cb = TokenTrackingCallback(model, provider=provider)
        cb.on_llm_start()
        cb.on_llm_end(_result_with_headers(headers))

        stored = fresh_kv.get_with_ttl(provider)
        assert stored is not None, (
            f"{provider} entry missing from SharedKV({_RATELIMIT_KV_NAMESPACE})"
        )
        # Payload equality — the KV is a pure mirror of the instance
        # snapshot. Any drift would be a checkbox-3 regression.
        assert stored == cb.last_ratelimit_state
        # No cross-provider leakage — only the provider that just fired
        # should have a KV entry.
        assert set(fresh_kv.get_all_with_ttl().keys()) == {provider}


# ─────────────────────────────────────────────────────────────────
# 3. Four providers coexist in a single KV hash
# ─────────────────────────────────────────────────────────────────


class TestFourProviderCoexistence:

    def test_four_sequential_turns_land_four_independent_keys(
        self, silent_track_tokens, fresh_kv,
    ):
        """Fire one ``on_llm_end`` per provider back-to-back within a
        single test, then snapshot the KV: all four keys must be live
        simultaneously with the normalised payload each turn wrote.

        This is the core Z.2/Z.4 dashboard invariant — the rate-limit
        card renders all four providers side-by-side and each vendor's
        state must survive the others' turns."""
        for provider, model, headers, _expected in (
            (c.values[0], c.values[1], c.values[2], c.values[3])
            for c in PROVIDER_CASES
        ):
            cb = TokenTrackingCallback(model, provider=provider)
            cb.on_llm_start()
            cb.on_llm_end(_result_with_headers(headers))

        snap = fresh_kv.get_all_with_ttl()
        assert set(snap.keys()) == {
            "anthropic", "openai", "deepseek", "openrouter",
        }
        # Spot-check: each entry carries the provider-specific
        # ``remaining_requests`` — mix-ups between providers (e.g.
        # OpenAI's 9998 landing under Anthropic's key) would be a
        # keying regression this assertion catches.
        assert snap["anthropic"]["remaining_requests"] == 987
        assert snap["openai"]["remaining_requests"] == 9998
        assert snap["deepseek"]["remaining_requests"] == 450
        assert snap["openrouter"]["remaining_requests"] == 300

    def test_later_turn_refreshes_own_ttl_not_siblings(
        self, silent_track_tokens, fresh_kv, monkeypatch,
    ):
        """Anthropic turn at t=1000, OpenAI turn at t=1050, read at
        t=1061. Anthropic's TTL (60s from t=1000) expired at t=1060 →
        pruned. OpenAI's TTL (60s from t=1050) expires at t=1110 →
        still live. Locks the per-field independence contract for the
        cross-provider case."""
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        cb_a = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb_a.on_llm_start()
        cb_a.on_llm_end(_result_with_headers(ANTHROPIC_HEADERS))

        monkeypatch.setattr(time, "time", lambda: 1050.0)
        cb_o = TokenTrackingCallback("gpt-4o", provider="openai")
        cb_o.on_llm_start()
        cb_o.on_llm_end(_result_with_headers(OPENAI_HEADERS))

        # Read at t=1061 — Anthropic expired at 1060, OpenAI expires at 1110.
        snap = fresh_kv.get_all_with_ttl(now=1061.0)
        assert set(snap.keys()) == {"openai"}, (
            f"per-field TTL regression — snap={snap}"
        )
        # Read at t=1111 — both expired.
        assert fresh_kv.get_all_with_ttl(now=1111.0) == {}


# ─────────────────────────────────────────────────────────────────
# 4. 60 s TTL expiry end-to-end, parametrised across all four
# ─────────────────────────────────────────────────────────────────


class TestFourProviderTTLExpiry:

    @pytest.mark.parametrize(
        "provider,model,headers,_expected", PROVIDER_CASES,
    )
    def test_entry_live_within_ttl_then_pruned(
        self, silent_track_tokens, fresh_kv, monkeypatch,
        provider, model, headers, _expected,
    ):
        """Write at t=1000 → live at t+30s → pruned at t+61s. The TTL
        constant is ``_RATELIMIT_TTL_SECONDS = 60.0`` and the contract
        must hold identically across all four providers (no per-vendor
        override)."""
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        cb = TokenTrackingCallback(model, provider=provider)
        cb.on_llm_start()
        cb.on_llm_end(_result_with_headers(headers))

        # Mid-window read — entry still live.
        assert fresh_kv.get_with_ttl(provider, now=1030.0) is not None
        # Just-expired read — 61 s > 60 s TTL → pruned.
        assert fresh_kv.get_with_ttl(provider, now=1061.0) is None
        # Pruning is destructive — the raw hash row is gone, not just
        # masked on read.
        assert fresh_kv.get(provider, default="__missing__") == "__missing__"

    def test_ttl_constant_is_60_seconds_end_to_end(
        self, silent_track_tokens, fresh_kv, monkeypatch,
    ):
        """Boundary precision: the entry must be live at t+59.999 and
        pruned at t+60.001 (the half-open interval the envelope
        ``_expires_at`` uses is ``now < _expires_at`` → ``live``)."""
        assert _RATELIMIT_TTL_SECONDS == 60.0
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        cb.on_llm_end(_result_with_headers(ANTHROPIC_HEADERS))

        # Inside the window.
        assert fresh_kv.get_with_ttl("anthropic", now=1059.999) is not None
        # Just past the window.
        assert fresh_kv.get_with_ttl("anthropic", now=1060.001) is None


# ─────────────────────────────────────────────────────────────────
# 5. Unknown provider leaves KV untouched even alongside live siblings
# ─────────────────────────────────────────────────────────────────


class TestUnknownProviderSkipsEndToEnd:
    """The checkbox 4 boundary test already locks the skip-on-unmapped-
    provider contract in isolation. Here we re-assert it under the
    four-provider mixed workload the dashboard actually ships with:
    an Ollama or Gemini turn interleaved between live Anthropic and
    OpenAI turns must not clobber the siblings' entries."""

    def test_ollama_turn_between_live_siblings_preserves_them(
        self, silent_track_tokens, fresh_kv,
    ):
        # Anthropic turn — lands normally.
        cb_a = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb_a.on_llm_start()
        cb_a.on_llm_end(_result_with_headers(ANTHROPIC_HEADERS))

        # Ollama turn — headerless (local runtime emits none). Skip.
        cb_oll = TokenTrackingCallback("llama3.1", provider="ollama")
        cb_oll.on_llm_start()
        cb_oll.on_llm_end(_result_with_headers({}))

        # OpenAI turn — lands normally.
        cb_o = TokenTrackingCallback("gpt-4o", provider="openai")
        cb_o.on_llm_start()
        cb_o.on_llm_end(_result_with_headers(OPENAI_HEADERS))

        snap = fresh_kv.get_all_with_ttl()
        # Ollama absent, siblings intact with their correct payloads.
        assert set(snap.keys()) == {"anthropic", "openai"}
        assert snap["anthropic"]["remaining_requests"] == 987
        assert snap["openai"]["remaining_requests"] == 9998

    def test_gemini_turn_between_live_siblings_preserves_them(
        self, silent_track_tokens, fresh_kv,
    ):
        """Google Gemini is the second unmapped provider today (see
        the docstring above ``_PROVIDER_RATELIMIT_HEADERS``). Even a
        spurious OpenAI-shape header set (proxy rewrite case) must
        not land — the mapping rule is structural, not content-based."""
        cb_a = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb_a.on_llm_start()
        cb_a.on_llm_end(_result_with_headers(ANTHROPIC_HEADERS))

        # Gemini turn with headers that *would* normalise if the
        # provider were registered — the allow-list must gate by
        # provider id regardless.
        cb_g = TokenTrackingCallback(
            "gemini-2.0-flash", provider="google_genai",
        )
        cb_g.on_llm_start()
        cb_g.on_llm_end(_result_with_headers(OPENAI_HEADERS))

        snap = fresh_kv.get_all_with_ttl()
        assert set(snap.keys()) == {"anthropic"}
        assert snap["anthropic"]["remaining_requests"] == 987

    def test_ollama_turn_before_first_live_turn_leaves_kv_empty(
        self, silent_track_tokens, fresh_kv,
    ):
        """Pre-seed scenario: first ever turn in a fresh dashboard is
        an Ollama call. The KV stays empty (no write, no raise). This
        matches the deployed-inactive production status — dashboard
        initialises empty, fills up as real-vendor turns happen."""
        cb_oll = TokenTrackingCallback("llama3.1", provider="ollama")
        cb_oll.on_llm_start()
        cb_oll.on_llm_end(_result_with_headers({}))

        assert fresh_kv.get_all_with_ttl() == {}
