"""Z.1 (#290) checkbox 1 — raw rate-limit header extraction.

Scope is deliberately narrow: this file locks the behaviour of
:meth:`backend.agents.llm.TokenTrackingCallback._extract_response_headers`
and the ``on_llm_end`` snapshot onto ``self.last_response_headers``.
Per-provider header name normalisation (``anthropic-ratelimit-*`` →
``remaining_requests``) and the ``SharedKV("provider_ratelimit")``
write live under subsequent Z.1 checkboxes and will get their own
test files. Keeping the split honest avoids backfilling the Z.1
mapping table prematurely.

What IS covered here:

1. Header dict surfaces from every candidate path the docstring
   enumerates (``llm_output['headers']`` / ``response_metadata
   ['headers']`` / flattened ``response_metadata`` with
   ``x-ratelimit-*`` keys / ``generation_info['headers']``).
2. Empty ``LLMResult`` degrades to ``{}`` — no raise, no None (the
   NULL-vs-genuine-zero contract ZZ.A1 established for cache fields
   extends to headers: downstream branches on truthiness).
3. Malformed input (``None``, wrong type, missing ``.generations``)
   degrades to ``{}`` with no exception — LangChain minor-version
   bumps routinely move paths and must not crash the turn.
4. ``on_llm_end`` writes to ``self.last_response_headers`` even when
   the subsequent token-pipeline branch is monkeypatched to raise,
   so a late failure doesn't silently eat the snapshot.
"""

from __future__ import annotations

import logging

import pytest

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration

from backend.agents.llm import TokenTrackingCallback
from backend.llm_adapter import LLMResult


# ─────────────────────────────────────────────────────────────────
# Helpers — build real LangChain ``ChatGeneration`` / ``AIMessage``
# payloads. ``LLMResult`` is a pydantic v2 model and validates the
# inner types, so duck typing isn't enough — but the extractor only
# reads attributes, so we're exercising the exact shape production
# code sees.
# ─────────────────────────────────────────────────────────────────


def _chat_gen(response_metadata=None, generation_info=None):
    """Build a ``ChatGeneration`` carrying the given message
    metadata. ``content=""`` is fine — the extractor never touches
    body text, only ``response_metadata`` / ``generation_info``."""
    msg = AIMessage(content="", response_metadata=response_metadata or {})
    return ChatGeneration(message=msg, generation_info=generation_info)


def _chat_gen_broken_metadata():
    """AIMessage forbids non-dict response_metadata at construction
    time (pydantic validation). Simulate a provider that handed back
    a wrong-typed metadata field by poking the dict post-construction
    via ``__dict__`` — mirrors how a LangChain adapter could in
    theory drift without re-validating."""
    msg = AIMessage(content="")
    # Bypass pydantic's setattr validation via __dict__.
    msg.__dict__["response_metadata"] = "not-a-dict"
    return ChatGeneration(message=msg)


def _result(
    *,
    llm_output=None,
    generations=None,
):
    return LLMResult(
        generations=generations if generations is not None else [[]],
        llm_output=llm_output,
    )


# ─────────────────────────────────────────────────────────────────
# Path 1 + 2: ``llm_output['headers']`` / ``['response_headers']``
# ─────────────────────────────────────────────────────────────────


class TestLlmOutputPath:

    def test_llm_output_headers_dict_is_returned(self):
        headers = {
            "x-ratelimit-remaining-requests": "42",
            "x-ratelimit-remaining-tokens": "9001",
        }
        res = _result(llm_output={"headers": headers})
        out = TokenTrackingCallback._extract_response_headers(res)
        assert out == headers
        # Defensive copy — caller must not be able to mutate the
        # response's internal dict by mutating the return value.
        out["poisoned"] = True
        assert "poisoned" not in res.llm_output["headers"]

    def test_llm_output_alternate_key_response_headers(self):
        headers = {"anthropic-ratelimit-requests-remaining": "3"}
        res = _result(llm_output={"response_headers": headers})
        assert TokenTrackingCallback._extract_response_headers(res) == headers

    def test_llm_output_headers_takes_precedence_over_response_headers(self):
        preferred = {"x-ratelimit-remaining-requests": "10"}
        fallback = {"x-ratelimit-remaining-requests": "99"}
        res = _result(
            llm_output={"headers": preferred, "response_headers": fallback},
        )
        assert TokenTrackingCallback._extract_response_headers(res) == preferred

    def test_llm_output_empty_headers_dict_falls_through(self):
        """An *empty* ``llm_output['headers']`` should not short-circuit —
        subsequent candidate paths still get a chance. Captures the "adapter
        reserved the slot but didn't populate it" case."""
        flat_meta = {
            "x-ratelimit-remaining-requests": "5",
        }
        gen = _chat_gen(response_metadata=flat_meta)
        res = _result(llm_output={"headers": {}}, generations=[[gen]])
        out = TokenTrackingCallback._extract_response_headers(res)
        assert out == flat_meta


# ─────────────────────────────────────────────────────────────────
# Path 3: ``response_metadata['headers']`` (langchain-anthropic ≥ 0.3)
# ─────────────────────────────────────────────────────────────────


class TestResponseMetadataWrappedHeaders:

    def test_anthropic_shape_wrapped_headers(self):
        headers = {
            "anthropic-ratelimit-requests-remaining": "50",
            "anthropic-ratelimit-tokens-remaining": "100000",
            "anthropic-ratelimit-tokens-reset": "2026-04-24T13:00:00Z",
        }
        gen = _chat_gen(
            response_metadata={"headers": headers, "model_name": "claude-opus-4-7"},
        )
        res = _result(generations=[[gen]])
        assert TokenTrackingCallback._extract_response_headers(res) == headers


# ─────────────────────────────────────────────────────────────────
# Path 4: flattened ``response_metadata`` (no ``headers`` wrapper)
# ─────────────────────────────────────────────────────────────────


class TestFlattenedResponseMetadata:

    def test_openai_flattened_x_ratelimit(self):
        meta = {
            "model_name": "gpt-4o",
            "finish_reason": "stop",
            "x-ratelimit-remaining-requests": "10000",
            "x-ratelimit-remaining-tokens": "2000000",
            "x-ratelimit-reset-requests": "12ms",
        }
        gen = _chat_gen(response_metadata=meta)
        res = _result(generations=[[gen]])
        out = TokenTrackingCallback._extract_response_headers(res)
        # Entire flattened metadata dict is returned — downstream Z.1
        # checkbox owns the filtering step, we just surface the raw.
        assert out == meta

    def test_flattened_anthropic_prefix(self):
        meta = {
            "stop_reason": "end_turn",
            "anthropic-ratelimit-requests-remaining": "47",
        }
        gen = _chat_gen(response_metadata=meta)
        res = _result(generations=[[gen]])
        assert (
            TokenTrackingCallback._extract_response_headers(res)
            == meta
        )

    def test_flattened_retry_after_alone(self):
        """A bare ``retry-after`` (the 429 path) is enough to flag
        response_metadata as rate-limit-bearing even without the
        prefix keys."""
        meta = {"retry-after": "30"}
        gen = _chat_gen(response_metadata=meta)
        res = _result(generations=[[gen]])
        assert (
            TokenTrackingCallback._extract_response_headers(res)
            == meta
        )

    def test_response_metadata_without_ratelimit_keys_is_ignored(self):
        """Without any rate-limit-shaped key, ``response_metadata`` is
        not misidentified as headers — otherwise every chat response
        would poison the SharedKV with garbage metadata."""
        meta = {"model_name": "gpt-4o", "finish_reason": "stop"}
        gen = _chat_gen(response_metadata=meta)
        res = _result(generations=[[gen]])
        assert TokenTrackingCallback._extract_response_headers(res) == {}


# ─────────────────────────────────────────────────────────────────
# Path 5: ``generation_info['headers']`` (older adapters)
# ─────────────────────────────────────────────────────────────────


class TestGenerationInfoPath:

    def test_generation_info_headers(self):
        headers = {"x-ratelimit-remaining-requests": "7"}
        gen = _chat_gen(generation_info={"headers": headers})
        res = _result(generations=[[gen]])
        assert TokenTrackingCallback._extract_response_headers(res) == headers


# ─────────────────────────────────────────────────────────────────
# Empty / malformed / Ollama-shape inputs → {}
# ─────────────────────────────────────────────────────────────────


class TestEmptyAndMalformed:

    def test_empty_llm_result_returns_empty_dict(self):
        assert TokenTrackingCallback._extract_response_headers(_result()) == {}

    def test_none_returns_empty_dict(self):
        assert TokenTrackingCallback._extract_response_headers(None) == {}

    def test_wrong_type_returns_empty_dict(self):
        assert TokenTrackingCallback._extract_response_headers("not-a-result") == {}
        assert TokenTrackingCallback._extract_response_headers(42) == {}
        assert TokenTrackingCallback._extract_response_headers({}) == {}

    def test_ollama_shape_no_headers_returns_empty_dict(self):
        """Ollama is local, emits no HTTP rate-limit headers — its
        response carries neither ``llm_output['headers']`` nor the
        relevant ``response_metadata`` keys. Must degrade to ``{}``
        without logging a warning or raising."""
        gen = _chat_gen(
            response_metadata={"model_name": "llama3.1", "done_reason": "stop"},
        )
        res = _result(llm_output={"token_usage": {"prompt_tokens": 10}},
                      generations=[[gen]])
        assert TokenTrackingCallback._extract_response_headers(res) == {}

    def test_generations_list_is_empty(self):
        """Some adapters return an empty generations list on a
        streaming-only path — extractor must not IndexError."""
        res = _result(generations=[[]])
        assert TokenTrackingCallback._extract_response_headers(res) == {}

    def test_message_with_nondict_response_metadata(self):
        """``response_metadata`` typed wrong must not crash the
        extractor — just skip that path."""
        gen = _chat_gen_broken_metadata()
        res = _result(generations=[[gen]])
        assert TokenTrackingCallback._extract_response_headers(res) == {}


# ─────────────────────────────────────────────────────────────────
# on_llm_end wiring — ``self.last_response_headers`` is set even
# if a later step raises.
# ─────────────────────────────────────────────────────────────────


class TestOnLlmEndSnapshot:

    def test_last_response_headers_initially_empty(self):
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        assert cb.last_response_headers == {}

    def test_on_llm_end_captures_headers_from_anthropic_shape(
        self, monkeypatch,
    ):
        """End-to-end: an Anthropic-shape response with rate-limit
        headers wrapped inside ``response_metadata['headers']`` lands
        on the callback instance after on_llm_end."""
        # Stub out downstream track_tokens / emit_* so the test
        # stays focused on header capture (the ZZ.A3 / ZZ.B1 suites
        # cover those branches independently).
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        headers = {
            "anthropic-ratelimit-requests-remaining": "47",
            "anthropic-ratelimit-tokens-remaining": "199876",
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

        assert cb.last_response_headers == headers

    def test_on_llm_end_empty_dict_on_no_headers(self, monkeypatch):
        """An Ollama-shape response (no headers anywhere) leaves
        ``last_response_headers`` at ``{}`` — downstream SharedKV
        write (future checkbox) will skip the provider based on
        truthiness."""
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        res = LLMResult(
            generations=[[]],
            llm_output={"token_usage": {"prompt_tokens": 20, "completion_tokens": 10}},
        )
        cb = TokenTrackingCallback("llama3.1", provider="ollama")
        cb.on_llm_start()
        cb.on_llm_end(res)
        assert cb.last_response_headers == {}

    def test_on_llm_end_degrades_when_extraction_raises(
        self, monkeypatch, caplog,
    ):
        """A broken ``_extract_response_headers`` (simulating a
        LangChain upgrade that shape-shifts the path) must not abort
        the turn — the inner try/except pins last_response_headers
        to ``{}`` and logs at debug."""
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        def boom(_response):
            raise RuntimeError("simulated langchain path drift")

        monkeypatch.setattr(
            TokenTrackingCallback, "_extract_response_headers",
            staticmethod(boom),
        )

        res = LLMResult(generations=[[]], llm_output={})
        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        with caplog.at_level(logging.DEBUG, logger="backend.agents.llm"):
            cb.on_llm_end(res)  # must not raise
        assert cb.last_response_headers == {}
        # Debug log recorded — future operator can spot the drift.
        assert any(
            "rate-limit header extraction skipped" in rec.message
            for rec in caplog.records
        )

    def test_on_llm_end_second_call_overwrites_prior_snapshot(
        self, monkeypatch,
    ):
        """Consecutive on_llm_end calls must *overwrite*, not merge —
        rate-limit state is point-in-time, stale headers from a
        prior turn must not leak into the current snapshot."""
        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", lambda *a, **kw: None)

        first_headers = {"x-ratelimit-remaining-requests": "100"}
        second_headers = {"x-ratelimit-remaining-requests": "99"}

        cb = TokenTrackingCallback("gpt-4o", provider="openai")
        cb.on_llm_start()
        cb.on_llm_end(_result(llm_output={"headers": first_headers}))
        assert cb.last_response_headers == first_headers

        cb.on_llm_start()
        cb.on_llm_end(_result(llm_output={"headers": second_headers}))
        assert cb.last_response_headers == second_headers


# ─────────────────────────────────────────────────────────────────
# Priority order between paths — regression guards
# ─────────────────────────────────────────────────────────────────


class TestPathPriority:

    def test_llm_output_beats_response_metadata(self):
        """If both paths have headers, ``llm_output['headers']`` wins —
        it's the more authoritative location (top-level SDK mirror
        vs. per-message metadata) and tying here prevents a
        metadata-carried stale header from clobbering the current
        turn's SDK-reported one."""
        top = {"x-ratelimit-remaining-requests": "10"}
        nested = {"x-ratelimit-remaining-requests": "999"}
        gen = _chat_gen(response_metadata={"headers": nested})
        res = _result(llm_output={"headers": top}, generations=[[gen]])
        assert TokenTrackingCallback._extract_response_headers(res) == top

    def test_response_metadata_wrapped_beats_flattened(self):
        """``response_metadata['headers']`` wins over flattened
        ``x-ratelimit-*`` sibling keys on the same metadata dict.
        Wrapped form is the newer, more explicit shape."""
        wrapped = {"x-ratelimit-remaining-requests": "10"}
        meta = {
            "headers": wrapped,
            "x-ratelimit-remaining-requests": "999",  # flattened stale
        }
        gen = _chat_gen(response_metadata=meta)
        res = _result(generations=[[gen]])
        assert TokenTrackingCallback._extract_response_headers(res) == wrapped

    def test_response_metadata_beats_generation_info(self):
        """The newer ``AIMessage.response_metadata`` path beats the
        legacy ``generation_info['headers']`` path — adapters that
        ship both converge on the metadata one."""
        new = {"x-ratelimit-remaining-requests": "42"}
        old = {"x-ratelimit-remaining-requests": "0"}
        gen = _chat_gen(
            response_metadata={"headers": new},
            generation_info={"headers": old},
        )
        res = _result(generations=[[gen]])
        assert TokenTrackingCallback._extract_response_headers(res) == new
