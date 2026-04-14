"""Phase 63-D D1 — IQ benchmark schema, loader, scorer."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from backend import iq_benchmark as ib


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  IQQuestion.matches
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_empty_answer_always_fails():
    q = ib.IQQuestion(id="q", prompt="?", expected_keywords=["foo"])
    assert q.matches("") is False
    assert q.matches("   ") is False


def test_question_with_no_positive_matcher_always_fails():
    """Defensive: a malformed question (no keywords + no regex) must
    NOT silently pass everything."""
    q = ib.IQQuestion(id="q", prompt="?")
    assert q.matches("any plausible answer") is False


def test_keyword_match_is_case_insensitive():
    q = ib.IQQuestion(id="q", prompt="?", expected_keywords=["Atomic"])
    assert q.matches("Use ATOMIC<int> here.") is True


def test_keyword_match_requires_all_keywords():
    q = ib.IQQuestion(id="q", prompt="?",
                      expected_keywords=["scale", "zero"])
    assert q.matches("compute the scale only") is False
    assert q.matches("compute scale and zero point") is True


def test_forbidden_keyword_disqualifies_even_when_positives_match():
    q = ib.IQQuestion(id="q", prompt="?",
                      expected_keywords=["initialize"],
                      forbidden_keywords=["i don't know"])
    assert q.matches("Initialize the var. I don't know exactly when.") is False
    assert q.matches("Initialize the var explicitly.") is True


def test_regex_matcher_passes():
    q = ib.IQQuestion(id="q", prompt="?",
                      expected_regex=r"--sysroot\b")
    assert q.matches("Pass --sysroot=/opt/vendor") is True
    assert q.matches("Pass -isysroot maybe?") is False


def test_invalid_regex_is_treated_as_failure_with_warning(caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="backend.iq_benchmark")
    q = ib.IQQuestion(id="qbad", prompt="?", expected_regex="(unbalanced")
    assert q.matches("anything") is False
    assert any("bad regex on qbad" in r.getMessage() for r in caplog.records)


def test_regex_OR_keywords_both_required():
    """Both matchers active → answer must satisfy BOTH."""
    q = ib.IQQuestion(id="q", prompt="?",
                      expected_keywords=["sysroot"],
                      expected_regex=r"--sysroot=\S+")
    # both required: keyword AND regex must hit
    assert q.matches("Pass --sysroot=/x; sysroot keyword") is True
    assert q.matches("set the sysroot somewhere") is False  # regex misses


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Loader
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "set.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_load_valid_set(tmp_path):
    p = _write_yaml(tmp_path, """
        schema_version: 1
        name: test-set
        description: tiny
        questions:
          - id: q1
            prompt: ask one
            expected_keywords: [hello]
          - id: q2
            prompt: ask two
            expected_regex: 'world'
            weight: 2.0
    """)
    bench = ib.load_benchmark(p)
    assert bench.name == "test-set"
    assert len(bench.questions) == 2
    assert bench.questions[1].weight == 2.0
    assert bench.total_weight() == 3.0


def test_load_rejects_unsupported_schema(tmp_path):
    p = _write_yaml(tmp_path, """
        schema_version: 99
        name: t
        questions:
          - id: q
            prompt: x
            expected_keywords: [a]
    """)
    with pytest.raises(ValueError, match="schema_version"):
        ib.load_benchmark(p)


def test_load_rejects_empty_questions(tmp_path):
    p = _write_yaml(tmp_path, """
        schema_version: 1
        name: t
        questions: []
    """)
    with pytest.raises(ValueError, match="no questions"):
        ib.load_benchmark(p)


def test_load_rejects_duplicate_ids(tmp_path):
    p = _write_yaml(tmp_path, """
        schema_version: 1
        name: dup
        questions:
          - id: q1
            prompt: a
            expected_keywords: [x]
          - id: q1
            prompt: b
            expected_keywords: [y]
    """)
    with pytest.raises(ValueError, match="duplicate question id"):
        ib.load_benchmark(p)


def test_load_all_skips_broken_files_with_log(tmp_path, caplog):
    import logging
    (tmp_path / "good.yaml").write_text(textwrap.dedent("""
        schema_version: 1
        name: good
        questions:
          - id: q1
            prompt: x
            expected_keywords: [foo]
    """))
    (tmp_path / "bad.yaml").write_text("schema_version: 99\nquestions: []\n")
    caplog.set_level(logging.ERROR, logger="backend.iq_benchmark")
    benches = ib.load_all(tmp_path)
    assert len(benches) == 1
    assert benches[0].name == "good"
    assert any("bad.yaml" in r.getMessage() for r in caplog.records)


def test_load_all_returns_empty_for_missing_dir(tmp_path):
    assert ib.load_all(tmp_path / "does-not-exist") == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Curated set sanity (the real shipped YAML)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_shipped_firmware_set_loads_and_has_10_questions():
    benches = ib.load_all()
    by_name = {b.name: b for b in benches}
    bench = by_name.get("firmware-debug-set-1")
    assert bench is not None, "shipped firmware-debug.yaml should load"
    assert len(bench.questions) == 10
    # Spot-check matchers actually catch a sane answer.
    q1 = next(q for q in bench.questions if q.id == "q01-memcpy-bounds")
    assert q1.matches(
        "Always check the source length against the destination size; "
        "use bounds-checked memcpy or memcpy_s."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scoring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _bench(qs):
    return ib.IQBenchmark(name="b", schema_version=1, description="", questions=qs)


def test_score_perfect():
    qs = [
        ib.IQQuestion(id="q1", prompt="?", expected_keywords=["alpha"]),
        ib.IQQuestion(id="q2", prompt="?", expected_keywords=["beta"]),
    ]
    s = ib.score_answers(_bench(qs), "model-x",
                         {"q1": "alpha here", "q2": "beta there"})
    assert s.pass_count == 2
    assert s.total_count == 2
    assert s.pass_rate == 1.0
    assert s.weighted_score == 1.0


def test_score_partial_with_weights():
    qs = [
        ib.IQQuestion(id="q1", prompt="?",
                      expected_keywords=["a"], weight=1.0),
        ib.IQQuestion(id="q2", prompt="?",
                      expected_keywords=["b"], weight=3.0),  # heavier
    ]
    # Get the heavy one right; miss the light one.
    s = ib.score_answers(_bench(qs), "m", {"q1": "nope", "q2": "b is here"})
    assert s.pass_count == 1
    assert s.total_count == 2
    # weighted: 3.0 / 4.0 = 0.75 (vs unweighted pass_rate 0.5)
    assert s.weighted_score == pytest.approx(0.75)
    assert s.pass_rate == 0.5


def test_score_missing_answer_counts_as_fail():
    qs = [ib.IQQuestion(id="q1", prompt="?", expected_keywords=["a"])]
    s = ib.score_answers(_bench(qs), "m", {})
    assert s.pass_count == 0


def test_aggregate_scores_averages_per_model():
    s_a1 = ib.BenchmarkScore("setA", "m1", 7, 10, 0.7, [])
    s_b1 = ib.BenchmarkScore("setB", "m1", 9, 10, 0.9, [])
    s_a2 = ib.BenchmarkScore("setA", "m2", 5, 10, 0.5, [])
    avg = ib.aggregate_scores([s_a1, s_b1, s_a2])
    assert avg["m1"] == pytest.approx(0.8)
    assert avg["m2"] == pytest.approx(0.5)


def test_aggregate_drops_models_with_no_samples():
    assert ib.aggregate_scores([]) == {}
