"""ZZ.A1 (#303-1) — focused regression tests for the prompt-cache
observability path.

The broader ``test_shared_state.py::TestSharedTokenUsage`` suite already
locks the ``SharedTokenUsage`` shape — this file narrows on the three
gaps the Wave A spec (#303) calls out explicitly:

1. **Cache shape normalise Anthropic vs OpenAI.** Two different provider
   response shapes must collapse onto the same
   ``(cache_read, cache_create)`` tuple once the LLM callback has
   extracted them, so the downstream dashboard never branches on
   provider.
2. **NULL compatibility.** Pre-ZZ Redis payloads have no cache fields
   at all; the ``get_all()`` projection must surface ``None`` (not 0)
   so "legacy row, no data" stays distinguishable from "ZZ-era row
   that saw zero cache hits" — which is the whole reason the frontend
   renders an em-dash instead of ``0%`` in that case.
3. **Hit-ratio calculation.** ``cache_hit_ratio = cache_read /
   (input + cache_read)`` must hold across Anthropic (has both sides)
   and OpenAI (only reports reads, creation side normalises to 0), and
   the expected numeric values are locked so a future refactor can't
   silently change the formula.
"""

import pytest

from backend.agents.llm import TokenTrackingCallback
from backend.shared_state import SharedTokenUsage


class TestCacheShapeNormalise:
    """`TokenTrackingCallback._extract_cache_tokens` must normalise
    Anthropic and OpenAI response bodies onto one
    ``(cache_read, cache_create)`` tuple. The two providers disagree on
    both the field name AND the nesting depth — the callback is the
    single place that hides that, so it has to be locked by test."""

    def test_normalise_anthropic_vs_openai_shape(self):
        # Anthropic: flat keys on ``usage``, reports both read + create.
        anthropic_usage = {
            "input_tokens": 200,
            "output_tokens": 50,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 120,
        }
        # OpenAI: nested under ``prompt_tokens_details``, only reports
        # reads (no cache-creation concept in the OpenAI API).
        openai_usage = {
            "prompt_tokens": 1100,
            "completion_tokens": 60,
            "prompt_tokens_details": {"cached_tokens": 300},
        }

        anth_read, anth_create = TokenTrackingCallback._extract_cache_tokens(
            anthropic_usage,
        )
        oai_read, oai_create = TokenTrackingCallback._extract_cache_tokens(
            openai_usage,
        )

        # Anthropic surfaces both sides verbatim.
        assert (anth_read, anth_create) == (800, 120)
        # OpenAI: reads carry through, creation normalises to 0 so the
        # dashboard doesn't branch on provider (``None`` would force
        # every caller to ``?? 0`` — we centralise that here instead).
        assert (oai_read, oai_create) == (300, 0)
        # Crucial contract: both outputs are plain ``int`` — never
        # ``None`` — so SharedTokenUsage.track() can add them without
        # a type guard on the hot path.
        assert isinstance(anth_read, int) and isinstance(anth_create, int)
        assert isinstance(oai_read, int) and isinstance(oai_create, int)


class TestNullCompat:
    """Pre-ZZ Redis payloads (written before the cache columns existed)
    have no ``cache_*`` fields. ``get_all()`` must surface ``None`` —
    NOT 0, NOT missing-key — so the UI can render "—" to flag
    "no data" as distinct from "zero hits".
    """

    def test_get_all_surfaces_null_for_pre_zz_row(self):
        usage = SharedTokenUsage()
        usage.clear()
        # Seed a legacy row as though restored from a pre-ZZ Redis
        # payload — the three cache fields are absent entirely.
        usage._local["pre-zz"] = {
            "model": "pre-zz",
            "input_tokens": 500,
            "output_tokens": 200,
            "total_tokens": 700,
            "cost": 0.05,
            "request_count": 2,
            "avg_latency": 150,
            "last_used": "09:00:00",
        }
        projection = usage.get_all()["pre-zz"]

        # NULL marker, not 0 — that's the whole point of the
        # "distinguish legacy from genuine-zero" contract.
        assert projection["cache_read_tokens"] is None
        assert projection["cache_create_tokens"] is None
        assert projection["cache_hit_ratio"] is None
        # And the canonical fields are still intact — NULL compat
        # must NOT damage the rest of the shape.
        assert projection["total_tokens"] == 700
        assert projection["request_count"] == 2


class TestHitRatioCalculation:
    """``cache_hit_ratio = cache_read / (input + cache_read)``. Lock
    both provider shapes end-to-end — Anthropic + OpenAI — since the
    formula has to land the same way regardless of which callback
    path produced the raw numbers.
    """

    def test_hit_ratio_anthropic_and_openai_land_same_formula(self):
        usage = SharedTokenUsage()
        usage.clear()

        # Anthropic: 800 cache-read + 200 fresh input ⇒ 800 / (200 + 800) = 0.8.
        anth = usage.track(
            "claude-opus-4-7", 200, 50, 100.0, 0.002,
            cache_read_tokens=800, cache_create_tokens=120,
        )
        assert anth["cache_hit_ratio"] == pytest.approx(0.8, abs=1e-6)
        assert anth["cache_read_tokens"] == 800
        assert anth["cache_create_tokens"] == 120

        # OpenAI: 300 cache-read + 1000 fresh input ⇒ 300 / (1000 + 300)
        # ≈ 0.230769. Creation side is 0 (OpenAI has no equivalent).
        oai = usage.track(
            "gpt-4o", 1000, 60, 100.0, 0.003,
            cache_read_tokens=300, cache_create_tokens=0,
        )
        assert oai["cache_hit_ratio"] == pytest.approx(
            300 / (1000 + 300), abs=1e-6,
        )
        assert oai["cache_read_tokens"] == 300
        assert oai["cache_create_tokens"] == 0
