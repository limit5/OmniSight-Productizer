"""U6-3 — L2 offline write-signal eval tests (offline, dormant).

Pins the §6 L2 metrics: a CLEAN closed candidate passes (schema-valid, no verbatim
copy, no sensitive data, useful), a LEAKY candidate is CAUGHT by ≥1 cleanliness
signal, and the qualification over the frozen corpus is NON-VACUOUS.
"""
from __future__ import annotations

import pytest

from backend.agents.u6_l2_eval import (
    EVAL_VERSION,
    MAX_VERBATIM_OVERLAP,
    L2_EVAL_CORPUS,
    EvalCase,
    Label,
    evaluate_candidate,
    evaluate_corpus,
    is_useful,
    schema_valid,
    sensitive_hit,
    verbatim_overlap,
)

_SRC = ["the deploy standard is checkpatch strict for every commit we push"]
_CLEAN = {
    "schema_version": 1, "outcome_kind": "task_created", "turn_count": 4,
    "resolved": True, "tasks_created": ["OP-9"], "topics": ["deployment"],
}


# ── clean candidate ──────────────────────────────────────────────────────────
def test_clean_candidate_is_clean_and_useful() -> None:
    ev = evaluate_candidate(_SRC, dict(_CLEAN))
    assert ev.schema_valid is True
    assert ev.verbatim_overlap == 0.0
    assert ev.sensitive is False
    assert ev.useful is True
    assert ev.clean is True


# ── verbatim overlap ─────────────────────────────────────────────────────────
def test_verbatim_overlap_detects_copied_source() -> None:
    # a value copying a >=4-token run of the source overlaps; short tokens don't.
    assert verbatim_overlap(["the deploy standard is checkpatch strict"], _SRC) > 0.0
    assert verbatim_overlap(["OP-9", "deployment"], _SRC) == 0.0


# ── sensitive detection ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "candidate",
    [
        {"model_fingerprint": "alice@example.com"},        # email
        {"notes": "card 4111111111111111"},                # long digit run
        {"summary": "password: hunter2"},                  # credential keyword
    ],
)
def test_sensitive_hit_flags_sensitive_values(candidate) -> None:
    assert sensitive_hit(candidate) is True


def test_clean_values_not_sensitive() -> None:
    assert sensitive_hit(_CLEAN) is False


@pytest.mark.parametrize(
    "candidate",
    [
        {"notes": "ssn 123-45-6789"},          # SSN (hyphens split the digit run)
        {"notes": "host 192.168.0.13"},         # IPv4
    ],
)
def test_sensitive_hit_catches_ssn_and_ipv4(candidate) -> None:
    assert sensitive_hit(candidate) is True


def test_high_numbered_task_ref_is_not_a_false_positive() -> None:
    # OP-1234567 is a legitimate 7-digit ref, not leaked sensitive data.
    assert sensitive_hit({"tasks_created": ["OP-1234567"]}) is False


def test_nested_dict_sensitive_value_is_caught() -> None:
    assert sensitive_hit({"meta": {"deep": "alice@example.com"}}) is True


def test_evaluate_candidate_fails_closed_on_non_dict() -> None:
    for bad in (None, "str", 5, ["x"]):
        ev = evaluate_candidate(["s"], bad)  # type: ignore[arg-type]
        assert ev.schema_valid is False and ev.clean is False and ev.useful is False


# ── schema validity + usefulness ─────────────────────────────────────────────
def test_schema_valid_accepts_closed_rejects_extra_field() -> None:
    assert schema_valid(_CLEAN) is True
    assert schema_valid({**_CLEAN, "notes": "anything"}) is False  # unknown field


def test_is_useful() -> None:
    assert is_useful(_CLEAN) is True
    assert is_useful({"outcome_kind": "no_outcome", "tasks_created": [], "topics": []}) is False
    assert is_useful({"outcome_kind": "no_outcome", "tasks_created": ["OP-1"], "topics": []}) is True


# ── leaky candidate is CAUGHT ────────────────────────────────────────────────
def test_free_text_leak_is_caught() -> None:
    leaky = {**_CLEAN, "summary": "the deploy standard is checkpatch strict for every commit"}
    ev = evaluate_candidate(_SRC, leaky)
    assert ev.schema_valid is False          # unknown field rejected by U6-2a
    assert ev.verbatim_overlap > 0.0         # ...and it copied source text
    assert ev.leak_caught is True


def test_sensitive_leak_is_caught_even_if_short() -> None:
    leaky = {**_CLEAN, "model_fingerprint": "alice@example.com"}
    ev = evaluate_candidate(_SRC, leaky)
    assert ev.sensitive is True
    assert ev.leak_caught is True


# ── corpus qualification (§6) — thresholds + non-vacuity ─────────────────────
def test_corpus_meets_thresholds() -> None:
    m = evaluate_corpus(L2_EVAL_CORPUS)
    assert m.clean_pass_rate >= 1.0     # every clean candidate passes
    assert m.leak_catch_rate >= 1.0     # every leaky candidate caught
    assert m.meets_thresholds is True


def test_qualification_is_non_vacuous() -> None:
    m = evaluate_corpus(L2_EVAL_CORPUS)
    assert m.n_clean > 0 and m.n_leaky > 0
    assert m.leaks_caught == m.n_leaky and m.leaks_caught > 0


def test_meets_thresholds_rejects_single_class_corpus() -> None:
    clean_only = tuple(c for c in L2_EVAL_CORPUS if c.label is Label.CLEAN)
    leaky_only = tuple(c for c in L2_EVAL_CORPUS if c.label is Label.LEAKY)
    assert evaluate_corpus(clean_only).meets_thresholds is False   # n_leaky == 0
    assert evaluate_corpus(leaky_only).meets_thresholds is False   # n_clean == 0
    assert evaluate_corpus(()).meets_thresholds is False           # empty


@pytest.mark.parametrize("case", L2_EVAL_CORPUS, ids=lambda c: f"{c.label.value}:{c.note[:24]}")
def test_every_corpus_case(case: EvalCase) -> None:
    ev = evaluate_candidate(list(case.source_texts), case.candidate)
    if case.label is Label.CLEAN:
        assert ev.clean and ev.useful
    else:
        assert ev.leak_caught


def test_thresholds_and_version_pinned() -> None:
    assert MAX_VERBATIM_OVERLAP == 0.0
    assert EVAL_VERSION == 1
