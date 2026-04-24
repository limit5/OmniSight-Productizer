"""Z.1 (#290) checkbox 4 — boundary contract for rate-limit header
capture.

Scope: lock the three explicit boundary guarantees this checkbox asks
for, so a regression in any one of them fails loudly rather than
silently degrading the rate-limit dashboard:

1. **Header missing → no raise, debug log only.** The underlying
   ``track_tokens`` / ``emit_turn_metrics`` pipeline must complete
   exactly the same way when a response carries no rate-limit
   headers (Ollama) as when it carries a full set (Anthropic). The
   callback logs at ``DEBUG`` but never at ``WARNING`` / ``ERROR`` —
   promoting a "no headers" event to ``WARNING`` would drown the
   operator in noise because Ollama responses are entirely headerless.
2. **Unknown provider (Ollama and any future keyless local runtime)
   → skipped.** ``_PROVIDER_RATELIMIT_HEADERS`` is the authoritative
   allow-list. A provider without a row short-circuits through
   ``_normalize_ratelimit_headers`` → ``{}`` → the ``on_llm_end`` KV
   write guard ``if self.provider and self.last_ratelimit_state`` →
   no SharedKV entry. The previously persisted snapshot from a
   sibling provider's turn is preserved (never clobbered with ``{}``).
3. **LangChain-internal path drift → graceful fallback.** The five
   candidate paths ``_extract_response_headers`` walks are an
   overlapping cover of every shape observed across langchain-openai,
   langchain-anthropic, and the other six OpenAI-compatible adapters
   at their current minor versions. A LangChain minor-version bump
   that shape-shifts *any* subset of those paths must either (a) hit
   one of the remaining paths, or (b) collapse to ``{}``. In no
   scenario does the turn abort; the LLM response reaches the caller
   regardless.

The three prior Z.1 test files cover each layer in isolation (extract,
normalise, SharedKV write). This file exercises the *contract* — it
asserts the three guarantees hold together, under a matrix of
combined-drift inputs, and that the downstream ``track_tokens`` call
always lands exactly once (so a partial-success path doesn't leak
observability into the happy-path branch).

The follow-on Z.1 checkbox (four-provider end-to-end mock) is a
separate file.
"""

from __future__ import annotations

import logging

import pytest

import backend.agents.llm as _llm_mod
from backend.agents.llm import TokenTrackingCallback
from backend.llm_adapter import AIMessage, ChatGeneration, LLMResult


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _chat_gen(response_metadata=None, generation_info=None):
    msg = AIMessage(content="", response_metadata=response_metadata or {})
    return ChatGeneration(message=msg, generation_info=generation_info)


def _result(*, llm_output=None, generations=None):
    return LLMResult(
        generations=generations if generations is not None else [[]],
        llm_output=llm_output,
    )


@pytest.fixture
def silent_track_tokens(monkeypatch):
    """Stub out the downstream token-usage pipeline so the boundary
    tests exercise *only* the rate-limit branch. Any side-effect of
    ``track_tokens`` (emit events, write DB) would mask a boundary
    regression behind an unrelated assertion."""
    import backend.routers.system as _sys
    calls = []
    monkeypatch.setattr(
        _sys, "track_tokens",
        lambda *a, **kw: calls.append((a, kw)),
    )
    return calls


@pytest.fixture
def fresh_kv(monkeypatch):
    """Cycle the module-level SharedKV singleton so each boundary test
    sees a clean provider_ratelimit namespace. Mirrors the fixture in
    ``test_ratelimit_kv_write.py`` — duplicated here so this file is
    self-contained and can be run in isolation."""
    monkeypatch.setattr(_llm_mod, "_ratelimit_kv_singleton", None)
    kv = _llm_mod._get_ratelimit_kv()
    for field in list(kv.get_all().keys()):
        kv.delete(field)
    return kv


# ─────────────────────────────────────────────────────────────────
# Boundary 1 — header missing never raises + debug log only
# ─────────────────────────────────────────────────────────────────


class TestHeaderMissingNeverRaises:
    """A response with no recoverable rate-limit headers must leave
    the LLM turn intact: no exception, no WARNING-or-higher log, no
    SharedKV write. The downstream ``track_tokens`` call still lands
    exactly once so token accounting doesn't silently drop a turn."""

    def test_headerless_anthropic_response(
        self, silent_track_tokens, fresh_kv, caplog,
    ):
        """Anthropic response where the SDK stripped headers (mock /
        test harness case). Callback swallows the absence, no KV entry,
        track_tokens fires once."""
        res = LLMResult(
            generations=[[]],
            llm_output={"token_usage": {"input_tokens": 5, "output_tokens": 3}},
        )
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        with caplog.at_level(logging.DEBUG, logger="backend.agents.llm"):
            cb.on_llm_end(res)

        # No raise, snapshot is empty dict (not None — truthiness contract).
        assert cb.last_response_headers == {}
        assert cb.last_ratelimit_state == {}
        # No SharedKV entry was written — skip condition fired.
        assert fresh_kv.get("anthropic", default="__missing__") == "__missing__"
        # Downstream token accounting fired exactly once.
        assert len(silent_track_tokens) == 1
        # Log floor: the only records emitted by *this* logger in this
        # turn must all be at DEBUG level. A WARNING would flood the
        # operator on every Ollama / headerless response.
        llm_logger_records = [
            r for r in caplog.records if r.name == "backend.agents.llm"
        ]
        assert llm_logger_records, "expected at least one debug record"
        assert all(r.levelno <= logging.DEBUG for r in llm_logger_records)

    def test_llm_output_none_headers_field(
        self, silent_track_tokens, fresh_kv, caplog,
    ):
        """An adapter reserving the ``llm_output['headers']`` slot but
        populating it with ``None`` (rather than omitting the key) must
        not crash ``_extract_response_headers``. Lands at ``{}``."""
        res = LLMResult(
            generations=[[]],
            llm_output={
                "headers": None,
                "token_usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
        cb = TokenTrackingCallback("gpt-4o", provider="openai")
        cb.on_llm_start()
        with caplog.at_level(logging.DEBUG, logger="backend.agents.llm"):
            cb.on_llm_end(res)

        assert cb.last_response_headers == {}
        assert cb.last_ratelimit_state == {}
        assert fresh_kv.get("openai", default="__missing__") == "__missing__"
        assert len(silent_track_tokens) == 1

    def test_partial_headers_normalise_keeps_known_fields(
        self, silent_track_tokens, fresh_kv,
    ):
        """A response carrying only one rate-limit field (e.g. a 429
        path that emits ``retry-after`` but no ``remaining-*``) must
        normalise the field it does have, not collapse to ``{}`` — the
        adaptive-backoff reader (Z.4) specifically needs
        ``retry_after_s`` on its own."""
        headers = {"retry-after": "30"}
        gen = _chat_gen(response_metadata=headers)
        res = LLMResult(
            generations=[[gen]],
            llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        cb.on_llm_end(res)

        assert cb.last_ratelimit_state["retry_after_s"] == 30.0
        assert cb.last_ratelimit_state["remaining_requests"] is None
        assert cb.last_ratelimit_state["remaining_tokens"] is None
        # SharedKV entry written (state is non-empty even if most fields None).
        stored = fresh_kv.get_with_ttl("anthropic")
        assert stored is not None
        assert stored["retry_after_s"] == 30.0


# ─────────────────────────────────────────────────────────────────
# Boundary 2 — unknown providers skipped, no accidental overwrites
# ─────────────────────────────────────────────────────────────────


class TestUnknownProviderSkipped:
    """Providers without a ``_PROVIDER_RATELIMIT_HEADERS`` row must
    never land a SharedKV entry, regardless of what their responses
    look like. This protects the dashboard from two failure modes:

      (a) Dashboard showing a garbage row for a provider with no
          meaningful rate-limit shape (Ollama is local-process so any
          number displayed would be a lie).
      (b) A sibling provider's genuine snapshot being overwritten by
          an Ollama turn's ``{}`` state (would hide the real provider's
          state until the next live turn).
    """

    def test_ollama_with_empty_headers(
        self, silent_track_tokens, fresh_kv,
    ):
        """Typical Ollama response: no rate-limit headers anywhere."""
        res = LLMResult(
            generations=[[]],
            llm_output={"token_usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )
        cb = TokenTrackingCallback("llama3.1", provider="ollama")
        cb.on_llm_start()
        cb.on_llm_end(res)

        assert cb.last_ratelimit_state == {}
        assert fresh_kv.get("ollama", default="__missing__") == "__missing__"

    def test_ollama_with_spurious_openai_headers_still_skipped(
        self, silent_track_tokens, fresh_kv,
    ):
        """Pathological case: an Ollama-fronted proxy rewrites headers
        to the OpenAI shape (e.g. some gateway experiments do this).
        The provider is *still* unmapped, so normalise must return
        ``{}`` and the KV write must skip. Rule is structural (mapping
        table), not content-based (header shape)."""
        headers = {
            "x-ratelimit-remaining-requests": "1000",
            "x-ratelimit-remaining-tokens": "500000",
            "x-ratelimit-reset-tokens": "12s",
        }
        gen = _chat_gen(response_metadata=headers)
        res = LLMResult(
            generations=[[gen]],
            llm_output={"token_usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )
        cb = TokenTrackingCallback("llama3.1", provider="ollama")
        cb.on_llm_start()
        cb.on_llm_end(res)

        # Raw extract still captures the shape (the extract function
        # is provider-agnostic), but normalise refuses to map it.
        assert cb.last_response_headers == headers
        assert cb.last_ratelimit_state == {}
        assert fresh_kv.get("ollama", default="__missing__") == "__missing__"

    def test_google_gemini_unmapped_same_treatment(
        self, silent_track_tokens, fresh_kv,
    ):
        """Google Gemini is the second deliberately-absent provider —
        langchain-google-genai doesn't surface rate-limit headers
        through any of the 5 extract paths. Treated as unmapped: no
        SharedKV entry under ``google``."""
        res = LLMResult(
            generations=[[]],
            llm_output={"token_usage": {"prompt_tokens": 5, "completion_tokens": 3}},
        )
        cb = TokenTrackingCallback("gemini-1.5-pro", provider="google")
        cb.on_llm_start()
        cb.on_llm_end(res)

        assert cb.last_ratelimit_state == {}
        assert fresh_kv.get("google", default="__missing__") == "__missing__"

    def test_unknown_provider_does_not_overwrite_sibling_snapshot(
        self, silent_track_tokens, fresh_kv,
    ):
        """Core dashboard invariant: a mapped provider's live snapshot
        is NOT clobbered by a subsequent unmapped provider turn. If an
        operator configures ``llm_provider=anthropic`` and a later
        utility call routes through Ollama, the Anthropic snapshot must
        survive the Ollama turn."""
        # Seed a live Anthropic snapshot.
        fresh_kv.set_with_ttl(
            "anthropic",
            {"remaining_requests": 47, "remaining_tokens": 199876,
             "reset_at_ts": None, "retry_after_s": 0.0},
            60.0,
        )

        # Run a headerless Ollama turn — must not clobber.
        res = LLMResult(
            generations=[[]],
            llm_output={"token_usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )
        cb = TokenTrackingCallback("llama3.1", provider="ollama")
        cb.on_llm_start()
        cb.on_llm_end(res)

        stored = fresh_kv.get_with_ttl("anthropic")
        assert stored is not None
        assert stored["remaining_requests"] == 47

    def test_provider_none_is_treated_as_unknown(
        self, silent_track_tokens, fresh_kv,
    ):
        """Legacy fixture / synthetic-test callback without a provider
        kwarg: treated identically to an unmapped provider — no KV
        write, no raise. Matches the NULL-vs-genuine-zero contract
        (missing provider = "we don't know", not "all providers")."""
        headers = {"anthropic-ratelimit-requests-remaining": "47"}
        gen = _chat_gen(response_metadata={"headers": headers})
        res = LLMResult(
            generations=[[gen]],
            llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        cb = TokenTrackingCallback("claude-opus-4-7")  # provider=None
        cb.on_llm_start()
        cb.on_llm_end(res)
        assert fresh_kv.get_all_with_ttl() == {}


# ─────────────────────────────────────────────────────────────────
# Boundary 3 — LangChain path drift graceful fallback
# ─────────────────────────────────────────────────────────────────


class TestLangChainPathDriftGraceful:
    """``_extract_response_headers`` walks 5 candidate paths. A
    LangChain minor-version bump that shape-shifts one or more paths
    must either (a) hit a remaining path that's still live, or
    (b) collapse to ``{}``. Either outcome is a graceful degradation —
    the turn never aborts and dashboards just miss one data point."""

    def test_primary_path_drift_falls_through_to_metadata(
        self, silent_track_tokens, fresh_kv,
    ):
        """LangChain removes the ``llm_output['headers']`` slot
        (happened in langchain-openai 0.2 → 0.3 migration). Extract
        must fall through to ``response_metadata['headers']`` (the
        newer Anthropic path) without losing the data."""
        headers = {
            "anthropic-ratelimit-requests-remaining": "7",
            "anthropic-ratelimit-tokens-remaining": "9000",
        }
        gen = _chat_gen(response_metadata={"headers": headers})
        res = LLMResult(
            generations=[[gen]],
            # Primary path absent — no ``llm_output['headers']``.
            llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        cb.on_llm_end(res)

        assert cb.last_response_headers == headers
        assert cb.last_ratelimit_state["remaining_requests"] == 7

    def test_metadata_path_drift_falls_through_to_flattened(
        self, silent_track_tokens, fresh_kv,
    ):
        """Adapter stops wrapping headers under
        ``response_metadata['headers']`` and inlines them directly —
        extract must detect the flattened shape and still surface it."""
        meta = {
            "model_name": "gpt-4o",
            "x-ratelimit-remaining-requests": "42",
            "x-ratelimit-remaining-tokens": "8000",
        }
        gen = _chat_gen(response_metadata=meta)
        res = LLMResult(
            generations=[[gen]],
            llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        cb = TokenTrackingCallback("gpt-4o", provider="openai")
        cb.on_llm_start()
        cb.on_llm_end(res)

        assert cb.last_response_headers == meta
        assert cb.last_ratelimit_state["remaining_requests"] == 42
        assert cb.last_ratelimit_state["remaining_tokens"] == 8000

    def test_metadata_path_drift_falls_through_to_generation_info(
        self, silent_track_tokens, fresh_kv,
    ):
        """Adapter reverts to the legacy ``generation_info['headers']``
        path — extract must still recover."""
        headers = {"x-ratelimit-remaining-requests": "3"}
        gen = _chat_gen(
            response_metadata={"model_name": "groq-x"},  # no rate-limit keys
            generation_info={"headers": headers},
        )
        res = LLMResult(
            generations=[[gen]],
            llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        cb = TokenTrackingCallback("llama-3.3-70b-versatile", provider="groq")
        cb.on_llm_start()
        cb.on_llm_end(res)
        assert cb.last_response_headers == headers
        assert cb.last_ratelimit_state["remaining_requests"] == 3

    def test_all_five_paths_absent_collapses_to_empty(
        self, silent_track_tokens, fresh_kv,
    ):
        """Worst case: every single path shape-shifted at once. Extract
        must return ``{}``, normalise must return ``{}``, no SharedKV
        write, track_tokens still fires."""
        gen = _chat_gen(
            response_metadata={"model_name": "claude-opus-4-7"},  # no headers
            generation_info={"finish_reason": "stop"},              # no headers
        )
        res = LLMResult(
            generations=[[gen]],
            llm_output={
                # No ``headers`` or ``response_headers`` keys.
                "token_usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        cb.on_llm_end(res)

        assert cb.last_response_headers == {}
        assert cb.last_ratelimit_state == {}
        assert fresh_kv.get_all_with_ttl() == {}
        assert len(silent_track_tokens) == 1

    def test_extract_raises_unexpected_exception_degrades_to_empty(
        self, silent_track_tokens, fresh_kv, caplog, monkeypatch,
    ):
        """Simulate a LangChain version where the extract walk itself
        raises (e.g. a pydantic model renamed a field and our
        ``isinstance`` checks can't be reached before an
        ``AttributeError`` on the new shape). The inner try/except
        around ``_extract_response_headers`` catches it, logs at DEBUG,
        pins ``last_response_headers = {}``, and the turn proceeds."""
        def boom(_response):
            raise RuntimeError("simulated langchain drift")

        monkeypatch.setattr(
            TokenTrackingCallback, "_extract_response_headers",
            staticmethod(boom),
        )

        res = LLMResult(
            generations=[[]],
            llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        with caplog.at_level(logging.DEBUG, logger="backend.agents.llm"):
            cb.on_llm_end(res)  # must not raise

        assert cb.last_response_headers == {}
        assert cb.last_ratelimit_state == {}
        assert len(silent_track_tokens) == 1
        assert any(
            "rate-limit header extraction skipped" in r.message
            for r in caplog.records
        )

    def test_normalise_raises_unexpected_exception_raw_snapshot_survives(
        self, silent_track_tokens, fresh_kv, caplog, monkeypatch,
    ):
        """Normalise drift (bug we haven't seen yet — e.g. a provider
        adds a fifth unified field and the dict-build raises a
        KeyError) must not unseat the raw snapshot. Proves the three
        inner try/excepts are actually independent."""
        def boom_normalise(_provider, _headers):
            raise RuntimeError("simulated normalise drift")

        monkeypatch.setattr(_llm_mod, "_normalize_ratelimit_headers", boom_normalise)

        headers = {"anthropic-ratelimit-requests-remaining": "47"}
        gen = _chat_gen(response_metadata={"headers": headers})
        res = LLMResult(
            generations=[[gen]],
            llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        with caplog.at_level(logging.DEBUG, logger="backend.agents.llm"):
            cb.on_llm_end(res)  # must not raise

        # Raw snapshot captured — extract succeeded before normalise
        # blew up. This is the whole point of the per-layer try/except.
        assert cb.last_response_headers == headers
        # Normalise branch degraded.
        assert cb.last_ratelimit_state == {}
        # KV write skipped because state is empty.
        assert fresh_kv.get_all_with_ttl() == {}
        assert any(
            "rate-limit header normalisation skipped" in r.message
            for r in caplog.records
        )


# ─────────────────────────────────────────────────────────────────
# Contract-level combined matrix — exhaustive never-raise guarantee
# ─────────────────────────────────────────────────────────────────


class TestNeverRaiseMatrix:
    """A sweep across corrupt-input shapes. No single member of the
    matrix is a likely production failure, but the *combined* lock
    proves the three independent try/except blocks in ``on_llm_end``
    (plus the outer try/except safety net) cover every path LangChain
    drift could conceivably send at us."""

    @pytest.mark.parametrize(
        "kind,llm_output,generations",
        [
            ("totally-empty", None, [[]]),
            ("empty-dict-llm-output", {}, [[]]),
            ("llm-output-None-headers", {"headers": None}, [[]]),
            ("llm-output-str-headers", {"headers": "not-a-dict"}, [[]]),
            ("llm-output-list-headers", {"headers": [1, 2, 3]}, [[]]),
            ("empty-generations-outer", {}, []),
            ("empty-generations-inner", {}, [[]]),
        ],
    )
    def test_corrupt_llm_result_never_raises(
        self, silent_track_tokens, fresh_kv,
        kind, llm_output, generations,
    ):
        """Any LLMResult shape that pydantic accepts must flow through
        ``on_llm_end`` without raising. Cases include nil headers,
        wrong-typed headers, empty generations list, etc."""
        res = LLMResult(generations=generations, llm_output=llm_output)
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        # The lock — no exception regardless of shape.
        cb.on_llm_end(res)

        # Snapshot degraded to empty dict, state empty, no KV entry.
        assert cb.last_response_headers == {}
        assert cb.last_ratelimit_state == {}
        assert fresh_kv.get("anthropic", default="__missing__") == "__missing__"

    def test_kv_write_failure_does_not_abort_turn(
        self, silent_track_tokens, fresh_kv, caplog, monkeypatch,
    ):
        """Boundary #1 continues past the KV layer: if the SharedKV
        write itself raises (Redis fell over, disk full on the in-
        memory fallback, JSON-encode edge case), the LLM turn still
        completes, ``last_ratelimit_state`` is still populated, and the
        degradation is logged at DEBUG."""
        class ExplodingKV:
            def set_with_ttl(self, *_a, **_kw):
                raise RuntimeError("simulated SharedKV blowup")

        monkeypatch.setattr(_llm_mod, "_get_ratelimit_kv", lambda: ExplodingKV())

        headers = {"anthropic-ratelimit-requests-remaining": "47"}
        gen = _chat_gen(response_metadata={"headers": headers})
        res = LLMResult(
            generations=[[gen]],
            llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        with caplog.at_level(logging.DEBUG, logger="backend.agents.llm"):
            cb.on_llm_end(res)  # must not raise

        # In-memory normalise succeeded — mirror is the only thing that failed.
        assert cb.last_ratelimit_state["remaining_requests"] == 47
        assert len(silent_track_tokens) == 1
        assert any(
            "provider_ratelimit SharedKV write skipped" in r.message
            for r in caplog.records
        )

    def test_triple_layer_failure_still_completes_turn(
        self, silent_track_tokens, fresh_kv, caplog, monkeypatch,
    ):
        """All three inner layers (extract, normalise, KV write) broken
        simultaneously. Proves the try/except chain is fully
        independent — one broken layer does not cascade into the next,
        and track_tokens still fires (operator's token accounting
        remains accurate even if rate-limit observability falls over
        entirely)."""
        def boom_extract(_response):
            raise RuntimeError("extract drift")
        def boom_normalise(_provider, _headers):
            raise RuntimeError("normalise drift")
        class ExplodingKV:
            def set_with_ttl(self, *_a, **_kw):
                raise RuntimeError("kv drift")

        monkeypatch.setattr(
            TokenTrackingCallback, "_extract_response_headers",
            staticmethod(boom_extract),
        )
        monkeypatch.setattr(_llm_mod, "_normalize_ratelimit_headers", boom_normalise)
        monkeypatch.setattr(_llm_mod, "_get_ratelimit_kv", lambda: ExplodingKV())

        res = LLMResult(
            generations=[[]],
            llm_output={"token_usage": {"input_tokens": 1, "output_tokens": 1}},
        )
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        with caplog.at_level(logging.DEBUG, logger="backend.agents.llm"):
            cb.on_llm_end(res)  # must not raise

        assert cb.last_response_headers == {}
        assert cb.last_ratelimit_state == {}
        # Token accounting — the non-negotiable part — still happened.
        assert len(silent_track_tokens) == 1
        # All three debug logs were emitted (each layer's catch path
        # records its own skip reason, independent of the others).
        messages = [r.message for r in caplog.records]
        assert any("extraction skipped" in m for m in messages)
        assert any("normalisation skipped" in m for m in messages)
        # Normalise degraded before state could be populated, so the KV
        # write skipped on the truthy-state guard before reaching the
        # exploding KV — the guard itself is the safety net here. The
        # kv-write test above exercises the ``set_with_ttl`` blowup path
        # directly.
