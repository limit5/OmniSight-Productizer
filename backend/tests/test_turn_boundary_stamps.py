"""ZZ.A3 (#303-3) checkbox 1 — per-turn LLM boundary stamps.

Locks the ``turn_started_at`` / ``turn_ended_at`` fields threaded
from ``TokenTrackingCallback.on_llm_start`` / ``on_llm_end`` through
``track_tokens`` → ``SharedTokenUsage.track`` → in-memory/Redis
entry dict, with the same NULL-vs-genuine-zero contract ZZ.A1
established for the prompt-cache fields.

Four gaps are guarded here explicitly — the broader
``test_shared_state.py::TestSharedTokenUsage`` suite already covers
the canonical shape so this file doesn't re-prove that:

1. **Callback captures both boundaries.** ``on_llm_start`` stashes
   an ISO-8601 UTC string on the instance; ``on_llm_end`` captures
   a second stamp at or after the first. Diff ≥ 0 and both strings
   are parseable as ISO timestamps.
2. **Last-turn-snapshot (overwrite, not accumulate).** Two
   consecutive track() calls leave the entry carrying the *second*
   pair — ZZ.A3's dashboard needs the latest turn's boundaries,
   not a historical average, to compute the inter-turn gap off the
   prior turn's ``turn_ended_at``.
3. **NULL compat on pre-ZZ rows.** A payload restored from Redis/PG
   with no ``turn_*`` fields must surface ``None`` (not "") from
   ``get_all()`` so the UI can render "—" for "no data" vs "" for
   "ZZ-era fresh row, turn still in progress".
4. **Back-compat default (omitted kwargs preserve prior value).**
   A caller that doesn't capture stamps (e.g. rule-based fallback
   or legacy test fixture) passes no kwargs — a previously populated
   stamp is NOT clobbered to None. Matches the partial-knowledge
   caller contract other fields on the entry already respect.
"""

from datetime import datetime

import pytest

from backend.agents.llm import TokenTrackingCallback
from backend.llm_adapter import LLMResult
from backend.shared_state import SharedTokenUsage


class TestCallbackCapturesBothBoundaries:
    """``on_llm_start`` must stash a wall-clock; ``on_llm_end`` must
    capture a second one AFTER the first and plumb both through to
    ``track_tokens``. The diff ``ended - started`` is the per-turn
    LLM compute time ZZ.A3 surfaces on the dashboard.
    """

    def test_on_llm_start_sets_iso_utc_stamp(self):
        cb = TokenTrackingCallback("test-model")
        # Fresh instance starts empty so on_llm_end of a never-started
        # callback degrades to "no data" rather than a fabricated stamp.
        assert cb._start_ts_utc == ""

        cb.on_llm_start()
        # Parseable as ISO-8601 UTC — ``fromisoformat`` with the ``+00:00``
        # suffix is the canonical round-trip.
        parsed = datetime.fromisoformat(cb._start_ts_utc)
        assert parsed.tzinfo is not None  # must be tz-aware
        assert parsed.utcoffset().total_seconds() == 0  # UTC, not local

    def test_on_llm_end_plumbs_both_stamps_to_track_tokens(self, monkeypatch):
        captured: dict = {}

        def fake_track_tokens(model, inp, out, latency_ms, **kw):
            captured["model"] = model
            captured["input_tokens"] = inp
            captured["output_tokens"] = out
            captured["latency_ms"] = latency_ms
            captured["turn_started_at"] = kw.get("turn_started_at")
            captured["turn_ended_at"] = kw.get("turn_ended_at")
            captured["cache_read_tokens"] = kw.get("cache_read_tokens")
            captured["cache_create_tokens"] = kw.get("cache_create_tokens")

        import backend.routers.system as _sys
        monkeypatch.setattr(_sys, "track_tokens", fake_track_tokens)

        # Synthesise a minimal Anthropic-shape response so
        # _extract_cache_tokens has real data to normalise.
        result = LLMResult(
            generations=[[]],
            llm_output={
                "token_usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 40,
                    "cache_creation_input_tokens": 10,
                }
            },
        )

        cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
        cb.on_llm_start()
        start_stamp = cb._start_ts_utc
        cb.on_llm_end(result)

        # Both stamps landed on track_tokens.
        assert captured["turn_started_at"] == start_stamp
        assert captured["turn_ended_at"] is not None
        # Ended-at is lexicographically ≥ started-at — ISO-8601 UTC
        # strings sort chronologically so a string compare is valid.
        assert captured["turn_ended_at"] >= captured["turn_started_at"]
        # Both parseable as tz-aware UTC.
        s = datetime.fromisoformat(captured["turn_started_at"])
        e = datetime.fromisoformat(captured["turn_ended_at"])
        assert s.tzinfo is not None and e.tzinfo is not None
        assert (e - s).total_seconds() >= 0


class TestSharedTokenUsageOverwriteSnapshot:
    """Consecutive track() calls must store the *latest* turn's pair,
    not accumulate or average. The inter-turn gap computation needs
    the last turn's ``turn_ended_at`` in the stored entry to subtract
    from this turn's ``turn_started_at`` — averaging would break it.
    """

    def test_track_overwrites_prior_turn_stamps(self):
        usage = SharedTokenUsage()
        usage.clear()

        usage.track(
            "claude-opus-4-7", 100, 50, 100.0, 0.001,
            turn_started_at="2026-04-24T12:00:00.000000+00:00",
            turn_ended_at="2026-04-24T12:00:02.500000+00:00",
        )
        second = usage.track(
            "claude-opus-4-7", 200, 75, 150.0, 0.002,
            turn_started_at="2026-04-24T12:00:05.000000+00:00",
            turn_ended_at="2026-04-24T12:00:08.250000+00:00",
        )

        # Latest turn wins — prior stamps GONE.
        assert second["turn_started_at"] == "2026-04-24T12:00:05.000000+00:00"
        assert second["turn_ended_at"] == "2026-04-24T12:00:08.250000+00:00"
        # Canonical counters still accumulate (this is the "cache_read +
        # timestamp" split contract) — so the test also proves we
        # didn't accidentally convert everything to overwrite.
        assert second["input_tokens"] == 300
        assert second["request_count"] == 2


class TestNullCompatPreZzRow:
    """A legacy payload predating ZZ.A3 has no ``turn_*`` keys.
    ``get_all()`` must surface ``None`` so the dashboard renders "—"
    — same NULL-vs-genuine-zero contract ZZ.A1 established for
    cache fields.
    """

    def test_get_all_surfaces_null_on_legacy_row(self):
        usage = SharedTokenUsage()
        usage.clear()
        # Post-ZZ.A1 but pre-ZZ.A3 row — carries cache fields but no
        # turn_* fields.
        usage._local["pre-zz-a3"] = {
            "model": "pre-zz-a3",
            "input_tokens": 500,
            "output_tokens": 200,
            "total_tokens": 700,
            "cost": 0.05,
            "request_count": 2,
            "avg_latency": 150,
            "last_used": "09:00:00",
            "cache_read_tokens": 400,
            "cache_create_tokens": 0,
            "cache_hit_ratio": 0.444444,
        }
        projection = usage.get_all()["pre-zz-a3"]

        # NULL marker for both turn stamps — dashboard renders "—".
        assert projection["turn_started_at"] is None
        assert projection["turn_ended_at"] is None
        # And the ZZ.A1 cache fields are intact — NULL-compat for
        # turn stamps must NOT damage the rest of the shape.
        assert projection["cache_read_tokens"] == 400
        assert projection["cache_hit_ratio"] == pytest.approx(0.444444, abs=1e-6)

    def test_fresh_zz_row_starts_empty_string_not_null(self):
        """A freshly created ZZ-era entry (via _fresh_token_entry)
        should NOT mask as "legacy" — it starts with empty string,
        and the very first track() call upgrades it to a real ISO
        timestamp. Legacy rows stay None, fresh rows start "".
        """
        usage = SharedTokenUsage()
        usage.clear()
        # Track with no stamps — the fresh entry starts "" per
        # _fresh_token_entry, and the None kwargs leave them alone.
        entry = usage.track("fresh-zz", 100, 50, 100.0, 0.001)
        assert entry["turn_started_at"] == ""
        assert entry["turn_ended_at"] == ""

        # Now a real stamped call upgrades both fields.
        stamp_start = "2026-04-24T15:00:00.000000+00:00"
        stamp_end = "2026-04-24T15:00:01.500000+00:00"
        entry = usage.track(
            "fresh-zz", 50, 25, 50.0, 0.0005,
            turn_started_at=stamp_start,
            turn_ended_at=stamp_end,
        )
        assert entry["turn_started_at"] == stamp_start
        assert entry["turn_ended_at"] == stamp_end


class TestPartialKnowledgeCallerPreservesPriorStamp:
    """A caller that omits turn_* kwargs (rule-based fallback, legacy
    test fixture, pre-ZZ.A3 worker) must NOT clobber a previously
    populated stamp to None. The stored value is authoritative until
    a ZZ.A3-aware caller overwrites it — partial-knowledge writes
    leave the field alone.
    """

    def test_omitted_kwargs_do_not_overwrite_prior_stamp(self):
        usage = SharedTokenUsage()
        usage.clear()

        # First turn: ZZ.A3 caller plumbs stamps through.
        usage.track(
            "claude-opus-4-7", 100, 50, 100.0, 0.001,
            turn_started_at="2026-04-24T12:00:00.000000+00:00",
            turn_ended_at="2026-04-24T12:00:02.000000+00:00",
        )
        # Second turn: legacy caller without stamps — must NOT erase
        # the prior stamp.
        entry = usage.track("claude-opus-4-7", 50, 25, 50.0, 0.0005)

        assert entry["turn_started_at"] == "2026-04-24T12:00:00.000000+00:00"
        assert entry["turn_ended_at"] == "2026-04-24T12:00:02.000000+00:00"
        # But canonical counters DID accumulate — this is a real turn
        # from the counter perspective, it just didn't capture stamps.
        assert entry["input_tokens"] == 150
        assert entry["request_count"] == 2
