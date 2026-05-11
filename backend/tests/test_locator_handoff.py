"""B7 (OP-839) — Locator handoff schema parse/validate tests.

The schema is frozen at filing time per master-plan §2.9 AC #2; these
tests pin the structural invariants so any silent drift fails CI
loudly. Each test name maps to a specific AC clause:

* AC #2 schema shape — ``test_minimal_valid_handoff_parses``,
  ``test_round_trip_to_coder_prompt_contains_schema_version``
* AC #4 retry-on-malformed — every ``test_malformed_*`` case
* AC #4 pollution-reject — ``test_pollution_over_budget_rejects``,
  ``test_pollution_threshold_boundary``
* AC #2 summary cap — ``test_summary_word_cap_enforced``

Pure-data tests; no network, no LLM, no filesystem.
"""

from __future__ import annotations

import json

import pytest

from backend.agents.locator_handoff_schema import (
    POLLUTION_CHAR_BUDGET,
    SCHEMA_VERSION,
    SUMMARY_WORD_CAP,
    LocatorCandidate,
    LocatorHandoff,
    LocatorHandoffPollution,
    LocatorMalformedHandoff,
    parse_locator_handoff,
    schema_reminder_for_retry,
)


# ── Fixtures ─────────────────────────────────────────────────────────


def _make_valid_envelope(
    *,
    files: list | None = None,
    hypotheses: list | None = None,
    confidence: float = 0.7,
    summary: str = "Standard 5-word summary fits cap.",
) -> str:
    payload = {
        "files": files
        if files is not None
        else [
            {
                "path": "backend/agents/foo.py",
                "line_ranges": [[10, 25]],
                "why_relevant": "primary function lives here",
            },
        ],
        "hypotheses": hypotheses if hypotheses is not None else ["fix off-by-one"],
        "confidence": confidence,
        "summary": summary,
    }
    return json.dumps(payload)


# ── AC #2 schema shape ──────────────────────────────────────────────


def test_minimal_valid_handoff_parses():
    """Minimal valid envelope round-trips through parse_locator_handoff."""
    text = _make_valid_envelope()
    handoff = parse_locator_handoff(text)
    assert isinstance(handoff, LocatorHandoff)
    assert handoff.candidate_count == 1
    assert handoff.files[0].path == "backend/agents/foo.py"
    assert handoff.files[0].line_ranges == ((10, 25),)
    assert handoff.hypotheses == ("fix off-by-one",)
    assert handoff.confidence == 0.7
    assert "5-word summary" in handoff.summary
    assert handoff.is_zero_candidates is False
    assert handoff.is_too_many_candidates is False


def test_empty_files_list_is_valid_but_zero_candidates():
    """AC #5 — 0 candidates is a valid envelope, the *outcome* is fallback."""
    text = _make_valid_envelope(files=[])
    handoff = parse_locator_handoff(text)
    assert handoff.is_zero_candidates is True
    assert handoff.candidate_count == 0


def test_50_plus_files_flags_too_many():
    """AC #6 — >50 candidates flips ``is_too_many_candidates`` predicate."""
    many_files = [
        {
            "path": f"backend/agents/m{i}.py",
            "line_ranges": [[1, 5]],
            "why_relevant": "x",
        }
        for i in range(51)
    ]
    handoff = parse_locator_handoff(_make_valid_envelope(files=many_files))
    assert handoff.candidate_count == 51
    assert handoff.is_too_many_candidates is True


def test_round_trip_to_coder_prompt_contains_schema_version():
    """AC #2/#3 — coder prompt embeds the frozen schema version banner."""
    handoff = parse_locator_handoff(_make_valid_envelope())
    prompt = handoff.to_coder_prompt(ac_text="1. AC item")
    assert SCHEMA_VERSION in prompt
    assert "1. AC item" in prompt
    assert "backend/agents/foo.py" in prompt


def test_multiple_line_ranges_preserved_in_order():
    """Line-range ordering is a stable handoff property — coder relies on it."""
    handoff = parse_locator_handoff(
        _make_valid_envelope(
            files=[
                {
                    "path": "backend/x.py",
                    "line_ranges": [[1, 5], [20, 30], [100, 100]],
                    "why_relevant": "three hotspots",
                },
            ]
        )
    )
    assert handoff.files[0].line_ranges == ((1, 5), (20, 30), (100, 100))


# ── AC #4 malformed-handoff cases (retry class) ─────────────────────


def test_malformed_not_json_raises():
    """Garbage text → malformed (retry once)."""
    with pytest.raises(LocatorMalformedHandoff):
        parse_locator_handoff("definitely not JSON, just prose")


def test_malformed_missing_required_field_raises():
    """``confidence`` absent → malformed."""
    payload = {
        "files": [],
        "hypotheses": [],
        "summary": "ok",
    }
    with pytest.raises(LocatorMalformedHandoff, match="missing required fields"):
        parse_locator_handoff(json.dumps(payload))


def test_malformed_wrong_type_for_files_raises():
    """``files`` must be a list."""
    payload = {"files": "not a list", "confidence": 0.5, "summary": "x"}
    with pytest.raises(LocatorMalformedHandoff, match="files"):
        parse_locator_handoff(json.dumps(payload))


def test_malformed_confidence_out_of_range_raises():
    """``confidence`` must be in [0,1]."""
    with pytest.raises(LocatorMalformedHandoff, match="out of range"):
        parse_locator_handoff(_make_valid_envelope(confidence=1.5))
    with pytest.raises(LocatorMalformedHandoff, match="out of range"):
        parse_locator_handoff(_make_valid_envelope(confidence=-0.1))


def test_malformed_line_range_inverted_raises():
    """``[5, 3]`` is rejected — end must be >= start."""
    bad = _make_valid_envelope(
        files=[
            {
                "path": "x.py",
                "line_ranges": [[5, 3]],
                "why_relevant": "inverted",
            }
        ]
    )
    with pytest.raises(LocatorMalformedHandoff, match="invalid"):
        parse_locator_handoff(bad)


def test_malformed_line_range_non_int_raises():
    bad = _make_valid_envelope(
        files=[
            {
                "path": "x.py",
                "line_ranges": [["a", "b"]],
                "why_relevant": "strings not ints",
            }
        ]
    )
    with pytest.raises(LocatorMalformedHandoff, match="ints"):
        parse_locator_handoff(bad)


def test_malformed_empty_path_raises():
    bad = _make_valid_envelope(
        files=[
            {"path": "", "line_ranges": [[1, 2]], "why_relevant": "empty"}
        ]
    )
    with pytest.raises(LocatorMalformedHandoff, match="missing or empty"):
        parse_locator_handoff(bad)


def test_summary_word_cap_enforced():
    """AC #2 — summary > 200 words → malformed (retry to shrink)."""
    long_summary = " ".join(["word"] * (SUMMARY_WORD_CAP + 5))
    with pytest.raises(LocatorMalformedHandoff, match="200-word cap"):
        parse_locator_handoff(_make_valid_envelope(summary=long_summary))


def test_extracts_json_with_leading_preamble():
    """Haiku occasionally adds a one-line preamble; we strip it."""
    text = (
        "Here is the locator handoff:\n"
        f"{_make_valid_envelope()}"
    )
    handoff = parse_locator_handoff(text)
    assert handoff.candidate_count == 1


# ── AC #4 pollution class (terminal, no retry) ──────────────────────


def test_pollution_over_budget_rejects():
    """Raw envelope >8000 chars → LocatorHandoffPollution, no retry."""
    # Pollution check fires on RAW char length, before JSON parse, so
    # we don't even need a valid envelope — a fat JSON-ish blob will do.
    bloated = '{"files": ["' + ("x" * (POLLUTION_CHAR_BUDGET + 100)) + '"]}'
    with pytest.raises(LocatorHandoffPollution, match="exceeds"):
        parse_locator_handoff(bloated)


def test_pollution_threshold_boundary():
    """Exactly POLLUTION_CHAR_BUDGET chars → parse attempt (no pollution)."""
    # Build an envelope just under the budget. It will fail with
    # malformed (not pollution) because the padded blob isn't valid
    # JSON — the point is pollution didn't short-circuit it.
    raw = "x" * POLLUTION_CHAR_BUDGET
    with pytest.raises(LocatorMalformedHandoff):
        parse_locator_handoff(raw)


def test_pollution_distinct_from_malformed():
    """Pollution and malformed are different exception classes (AC #4)."""
    assert not issubclass(LocatorHandoffPollution, LocatorMalformedHandoff)
    assert not issubclass(LocatorMalformedHandoff, LocatorHandoffPollution)


# ── Retry reminder ──────────────────────────────────────────────────


def test_schema_reminder_for_retry_mentions_required_fields():
    """The strict-format reminder must list every required field."""
    reminder = schema_reminder_for_retry()
    for field in ("files", "line_ranges", "why_relevant",
                  "hypotheses", "confidence", "summary"):
        assert field in reminder, f"reminder missing {field!r}"
    assert str(SUMMARY_WORD_CAP) in reminder


# ── Dataclass smoke ─────────────────────────────────────────────────


def test_locator_candidate_to_dict_round_trip():
    """LocatorCandidate.to_dict matches the schema we accept on parse."""
    cand = LocatorCandidate(
        path="x.py", line_ranges=((1, 2),), why_relevant="why",
    )
    payload = {
        "files": [cand.to_dict()],
        "hypotheses": [],
        "confidence": 0.5,
        "summary": "ok",
    }
    handoff = parse_locator_handoff(json.dumps(payload))
    assert handoff.files[0] == cand
