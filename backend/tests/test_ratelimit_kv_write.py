"""Z.1 (#290) checkbox 3 — ``SharedKV("provider_ratelimit")`` write
+ 60 s TTL.

Scope:

1. ``SharedKV.set_with_ttl`` / ``get_with_ttl`` / ``get_all_with_ttl``
   provide per-field lazy-prune TTL semantics — setting one field
   does not refresh another's expiry, and expired entries are deleted
   on read (self-healing).
2. ``TokenTrackingCallback.on_llm_end`` mirrors
   ``self.last_ratelimit_state`` into ``SharedKV("provider_ratelimit")``
   keyed by provider name with the TTL honoured by the
   ``_RATELIMIT_TTL_SECONDS`` module constant (60 s).
3. Write skip conditions: ``provider=None``, empty ratelimit state
   (Ollama / unmapped provider / response without rate-limit headers),
   SharedKV raise. All three are silent — no LLM turn ever aborts
   because of a SharedKV problem.

Scope boundary: the mapping-table and normalise semantics belong to
``test_ratelimit_header_normalize.py``. The raw-headers extract
belongs to ``test_ratelimit_header_extract.py``. This file tests only
the SharedKV persistence layer + its end-to-end wiring from
``on_llm_end``.
"""

from __future__ import annotations

import logging
import time

import pytest

from backend.agents.llm import (
    _RATELIMIT_KV_NAMESPACE,
    _RATELIMIT_TTL_SECONDS,
    TokenTrackingCallback,
    _get_ratelimit_kv,
)
from backend.llm_adapter import AIMessage, ChatGeneration, LLMResult
from backend.shared_state import SharedKV


def _chat_gen(response_metadata=None, generation_info=None):
    msg = AIMessage(content="", response_metadata=response_metadata or {})
    return ChatGeneration(message=msg, generation_info=generation_info)


@pytest.fixture
def fresh_ratelimit_kv(monkeypatch):
    """Reset the ``llm.py`` module-level SharedKV singleton + purge any
    in-memory state that leaked from prior tests. Using a unique
    namespace per test avoids cross-test pollution when the whole
    suite runs under the same process (``SharedKV`` in-memory dict
    lives on the instance, but the Redis key is process-shared — the
    latter matters for CI's ``pg-live-integration`` job but we don't
    exercise Redis here)."""
    import backend.agents.llm as _llm_mod
    monkeypatch.setattr(_llm_mod, "_ratelimit_kv_singleton", None)
    kv = _get_ratelimit_kv()
    # Purge any residue a previous test may have left in the in-memory
    # fallback under the same namespace.
    for field in list(kv.get_all().keys()):
        kv.delete(field)
    return kv


# ─────────────────────────────────────────────────────────────────
# SharedKV TTL primitives
# ─────────────────────────────────────────────────────────────────


class TestSharedKVSetWithTTL:

    def test_round_trip_within_ttl(self):
        kv = SharedKV("test_ttl_rt")
        kv.set_with_ttl("k", {"x": 1}, 60.0)
        assert kv.get_with_ttl("k") == {"x": 1}

    def test_round_trip_scalar_value(self):
        """Payload can be any JSON-serialisable shape (str / int / list)
        — the Z.1 checkbox uses dicts but the primitive is generic."""
        kv = SharedKV("test_ttl_scalar")
        kv.set_with_ttl("k_str", "hello", 10.0)
        kv.set_with_ttl("k_int", 42, 10.0)
        kv.set_with_ttl("k_list", [1, "two", 3.0], 10.0)
        assert kv.get_with_ttl("k_str") == "hello"
        assert kv.get_with_ttl("k_int") == 42
        assert kv.get_with_ttl("k_list") == [1, "two", 3.0]

    def test_absent_field_returns_none(self):
        kv = SharedKV("test_ttl_absent")
        assert kv.get_with_ttl("never-written") is None

    def test_expired_entry_returns_none_and_prunes(self):
        """Core TTL contract — once the embedded ``_expires_at`` is in
        the past, read returns ``None`` *and* the entry is deleted from
        the underlying hash (lazy prune)."""
        kv = SharedKV("test_ttl_expired")
        # Freeze "now" when writing so the expiry lands at a known ts.
        kv.set_with_ttl("k", {"v": 1}, 10.0, now=1000.0)
        # Read with a "now" that's past the expiry.
        assert kv.get_with_ttl("k", now=1011.0) is None
        # Raw hash entry was pruned — not merely hidden.
        assert kv.get("k", default="__missing__") == "__missing__"

    def test_setting_one_field_does_not_refresh_another(self):
        """Per-field TTL semantics. Regression guard against the
        ``r.expire(hash_key)`` implementation which would cross-
        contaminate expiries."""
        kv = SharedKV("test_ttl_independent")
        kv.set_with_ttl("a", "first", 10.0, now=1000.0)
        # Second write happens 5 s later with a fresh 10 s TTL.
        kv.set_with_ttl("b", "second", 10.0, now=1005.0)
        # Read at t=1012 — ``a`` expired at 1010, ``b`` expires at 1015.
        snap = kv.get_all_with_ttl(now=1012.0)
        assert snap == {"b": "second"}

    def test_zero_ttl_raises(self):
        kv = SharedKV("test_ttl_zero")
        with pytest.raises(ValueError):
            kv.set_with_ttl("k", "v", 0)

    def test_negative_ttl_raises(self):
        kv = SharedKV("test_ttl_neg")
        with pytest.raises(ValueError):
            kv.set_with_ttl("k", "v", -5)

    def test_malformed_raw_entry_treated_as_absent(self):
        """A legacy / malformed entry written via plain ``set`` (no
        envelope) must not crash ``get_with_ttl`` — the store is
        self-healing: unwrap failure deletes the entry and returns
        ``None``."""
        kv = SharedKV("test_ttl_malformed")
        kv.set("legacy", "not-json-envelope")
        assert kv.get_with_ttl("legacy") is None
        # Lazy prune removed the malformed row.
        assert kv.get("legacy", default="__missing__") == "__missing__"

    def test_overwrite_refreshes_own_ttl(self):
        """Writing the same field twice resets that field's expiry —
        ``anthropic`` being called again every 30 s keeps its entry
        alive even though 60 s TTL elapsed since the original write."""
        kv = SharedKV("test_ttl_overwrite")
        kv.set_with_ttl("provider", {"v": 1}, 60.0, now=1000.0)
        kv.set_with_ttl("provider", {"v": 2}, 60.0, now=1050.0)
        # t=1100 is 100 s after the first write but only 50 s after
        # the second — still live, with the second value.
        assert kv.get_with_ttl("provider", now=1100.0) == {"v": 2}


class TestSharedKVGetAllWithTTL:

    def test_returns_only_live_entries(self):
        kv = SharedKV("test_ttl_getall_live")
        kv.set_with_ttl("alive", "x", 100.0, now=1000.0)
        kv.set_with_ttl("dead", "y", 5.0, now=1000.0)
        snap = kv.get_all_with_ttl(now=1010.0)
        assert snap == {"alive": "x"}

    def test_empty_hash_returns_empty_dict(self):
        kv = SharedKV("test_ttl_getall_empty")
        assert kv.get_all_with_ttl() == {}

    def test_prunes_malformed_entries(self):
        kv = SharedKV("test_ttl_getall_malformed")
        kv.set_with_ttl("good", "x", 60.0, now=1000.0)
        kv.set("broken", "{not json")
        kv.set("no-envelope", '{"just": "a dict"}')
        snap = kv.get_all_with_ttl(now=1005.0)
        assert snap == {"good": "x"}
        # The two malformed entries were removed.
        assert kv.get("broken", default="__m__") == "__m__"
        assert kv.get("no-envelope", default="__m__") == "__m__"


# ─────────────────────────────────────────────────────────────────
# ``on_llm_end`` → SharedKV wiring
# ─────────────────────────────────────────────────────────────────


class TestOnLlmEndSharedKVWrite:

    def test_anthropic_ratelimit_state_landed_under_provider_key(
        self, monkeypatch, fresh_ratelimit_kv,
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

        # Key is the provider name, unscoped by tenant.
        stored = fresh_ratelimit_kv.get_with_ttl("anthropic")
        assert stored is not None
        assert stored["remaining_requests"] == 47
        assert stored["remaining_tokens"] == 199876
        assert stored["retry_after_s"] == 0.0
        assert isinstance(stored["reset_at_ts"], float)
        # And ``last_ratelimit_state`` still holds the same in-memory
        # snapshot — the two sources are mirrors.
        assert cb.last_ratelimit_state == stored

    def test_openai_family_four_providers_coexist_in_kv(
        self, monkeypatch, fresh_ratelimit_kv,
    ):
        """Four sequential LLM turns across four providers land under
        four distinct keys. Validates (a) key shape is provider name
        unscoped, (b) TTL is per-field so concurrent providers don't
        invalidate each other."""
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        scenarios = [
            ("openai", {
                "x-ratelimit-remaining-requests": "100",
                "x-ratelimit-remaining-tokens": "500000",
                "x-ratelimit-reset-tokens": "12s",
                "retry-after": "0",
            }),
            ("xai", {
                "x-ratelimit-remaining-requests": "50",
                "x-ratelimit-remaining-tokens": "250000",
                "x-ratelimit-reset-tokens": "30s",
                "retry-after": "0",
            }),
            ("groq", {
                "x-ratelimit-remaining-requests": "30",
                "x-ratelimit-remaining-tokens": "120000",
                "x-ratelimit-reset-tokens": "5s",
                "retry-after": "0",
            }),
            ("deepseek", {
                "x-ratelimit-remaining-requests": "1000",
                "x-ratelimit-remaining-tokens": "2000000",
                "x-ratelimit-reset-tokens": "60s",
                "retry-after": "0",
            }),
        ]
        for provider, headers in scenarios:
            gen = _chat_gen(response_metadata={"headers": headers})
            res = LLMResult(
                generations=[[gen]],
                llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
            )
            cb = TokenTrackingCallback(f"{provider}-model", provider=provider)
            cb.on_llm_start()
            cb.on_llm_end(res)

        snap = fresh_ratelimit_kv.get_all_with_ttl()
        assert set(snap.keys()) == {"openai", "xai", "groq", "deepseek"}
        assert snap["openai"]["remaining_requests"] == 100
        assert snap["xai"]["remaining_requests"] == 50
        assert snap["groq"]["remaining_requests"] == 30
        assert snap["deepseek"]["remaining_requests"] == 1000

    def test_ollama_skipped_no_kv_entry(
        self, monkeypatch, fresh_ratelimit_kv,
    ):
        """Unmapped provider → ``last_ratelimit_state == {}`` → no
        SharedKV write. Validates the truthiness skip contract."""
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        res = LLMResult(
            generations=[[]],
            llm_output={"token_usage": {"prompt_tokens": 20, "completion_tokens": 10}},
        )
        cb = TokenTrackingCallback("llama3.1", provider="ollama")
        cb.on_llm_start()
        cb.on_llm_end(res)

        assert fresh_ratelimit_kv.get_all_with_ttl() == {}
        assert fresh_ratelimit_kv.get("ollama", default="__missing__") == "__missing__"

    def test_provider_none_skipped_no_kv_entry(
        self, monkeypatch, fresh_ratelimit_kv,
    ):
        """Legacy fixture that instantiates the callback without
        ``provider`` → write is skipped silently, no raise. This
        matches the ZZ.A2 NULL-vs-genuine-zero contract: missing
        provider is "we don't know", not "provider=<empty string>"."""
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        headers = {
            "anthropic-ratelimit-requests-remaining": "47",
        }
        gen = _chat_gen(response_metadata={"headers": headers})
        res = LLMResult(
            generations=[[gen]],
            llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        cb = TokenTrackingCallback("claude-opus-4-7")  # provider=None
        cb.on_llm_start()
        cb.on_llm_end(res)
        assert fresh_ratelimit_kv.get_all_with_ttl() == {}

    def test_empty_ratelimit_state_skipped_no_kv_entry(
        self, monkeypatch, fresh_ratelimit_kv,
    ):
        """Mapped provider but response carried no rate-limit headers
        at all (e.g. streaming response dropped the trailer) →
        ``last_ratelimit_state == {}`` → no write. Writing ``{}`` would
        overwrite a genuine prior snapshot from a sibling turn with
        stale 'no data' which is strictly worse than leaving the prior
        entry to age out naturally."""
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        # Pre-seed a live snapshot from an earlier turn.
        fresh_ratelimit_kv.set_with_ttl(
            "anthropic",
            {"remaining_requests": 500, "remaining_tokens": 100000,
             "reset_at_ts": None, "retry_after_s": None},
            _RATELIMIT_TTL_SECONDS,
        )

        # Second turn has no headers.
        res = LLMResult(
            generations=[[]],
            llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        cb.on_llm_end(res)

        # Prior snapshot must still be there — the empty-state turn
        # must NOT have overwritten it.
        stored = fresh_ratelimit_kv.get_with_ttl("anthropic")
        assert stored is not None
        assert stored["remaining_requests"] == 500

    def test_kv_entry_expires_after_60_seconds(
        self, monkeypatch, fresh_ratelimit_kv,
    ):
        """End-to-end TTL verification. The entry is written by
        ``on_llm_end`` with a 60 s TTL; a read 61 s later must return
        ``None`` *and* the entry is pruned."""
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        # Freeze on_llm_end's write clock at t=1000.
        monkeypatch.setattr(time, "time", lambda: 1000.0)

        headers = {
            "anthropic-ratelimit-requests-remaining": "47",
            "anthropic-ratelimit-tokens-remaining": "199876",
        }
        gen = _chat_gen(response_metadata={"headers": headers})
        res = LLMResult(
            generations=[[gen]],
            llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        cb.on_llm_end(res)

        # Immediately live.
        assert fresh_ratelimit_kv.get_with_ttl("anthropic", now=1030.0) is not None
        # 61 s later — past the 60 s TTL — pruned.
        assert fresh_ratelimit_kv.get_with_ttl("anthropic", now=1061.0) is None
        # Pruning removed the underlying row.
        assert fresh_ratelimit_kv.get("anthropic", default="__m__") == "__m__"

    def test_kv_write_failure_does_not_abort_llm_turn(
        self, monkeypatch, fresh_ratelimit_kv, caplog,
    ):
        """A SharedKV bug / Redis explosion during the write path
        must degrade to a debug log — the LLM turn is already done by
        the time we reach the mirror, and throwing here would mask
        the response from the caller."""
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        class ExplodingKV:
            def set_with_ttl(self, *_a, **_kw):
                raise RuntimeError("simulated SharedKV blowup")

        import backend.agents.llm as _llm_mod
        monkeypatch.setattr(
            _llm_mod, "_get_ratelimit_kv", lambda: ExplodingKV(),
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
            cb.on_llm_end(res)  # must not raise
        assert any(
            "provider_ratelimit SharedKV write skipped" in rec.message
            for rec in caplog.records
        )
        # In-memory normalised state is still populated — only the
        # mirror failed.
        assert cb.last_ratelimit_state["remaining_requests"] == 47


# ─────────────────────────────────────────────────────────────────
# Module-const sanity — lock 60 s TTL + namespace so a future drift
# would be caught by the test suite rather than silently changing
# dashboard behaviour.
# ─────────────────────────────────────────────────────────────────


class TestModuleConsts:

    def test_ttl_is_sixty_seconds(self):
        assert _RATELIMIT_TTL_SECONDS == 60.0

    def test_namespace_is_provider_ratelimit(self):
        assert _RATELIMIT_KV_NAMESPACE == "provider_ratelimit"

    def test_get_ratelimit_kv_returns_singleton(self):
        """Two calls return the same instance — matches the
        ``session_presence`` module-singleton pattern."""
        a = _get_ratelimit_kv()
        b = _get_ratelimit_kv()
        assert a is b
