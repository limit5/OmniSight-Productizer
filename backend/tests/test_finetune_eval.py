"""Phase 65 S2 — hold-out evaluation gate."""

from __future__ import annotations

import pytest

from backend import finetune_eval as fe
from backend import iq_benchmark as ib


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunable
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_default_regression_pp(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_FINETUNE_REGRESSION_PP", raising=False)
    assert fe._regression_pp() == 5.0


@pytest.mark.parametrize("raw,expected", [
    ("0", 0.0), ("3", 3.0), ("10", 10.0),
    ("99", 50.0),  # clamped to 50
    ("-5", 0.0),   # clamped to 0
    ("bad", 5.0),  # invalid → default
])
def test_regression_pp_env_clamp(monkeypatch, raw, expected):
    monkeypatch.setenv("OMNISIGHT_FINETUNE_REGRESSION_PP", raw)
    assert fe._regression_pp() == expected


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Hold-out lookup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_load_holdout_finds_shipped_yaml():
    bench = fe.load_holdout()
    assert bench is not None
    assert bench.name == "holdout-finetune-v1"
    assert len(bench.questions) >= 10  # v1 ships with 10


def test_load_holdout_unknown_returns_none():
    assert fe.load_holdout("does-not-exist") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  compare_models — pure with injected ask_fn + custom benchmark
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _tiny_bench() -> ib.IQBenchmark:
    """3-question test bench so we can drive scores precisely."""
    return ib.IQBenchmark(
        name="tiny", schema_version=1, description="",
        questions=[
            ib.IQQuestion(id="q1", prompt="say alpha",
                          expected_keywords=["alpha"]),
            ib.IQQuestion(id="q2", prompt="say beta",
                          expected_keywords=["beta"]),
            ib.IQQuestion(id="q3", prompt="say gamma",
                          expected_keywords=["gamma"]),
        ],
    )


def _scripted(answers_by_model: dict[str, dict[str, str]]):
    """Return ask_fn that consults a model→prompt-keyword→answer map."""
    async def f(model: str, prompt: str) -> tuple[str, int]:
        for kw, ans in answers_by_model.get(model, {}).items():
            if kw in prompt:
                return (ans, 50)
        return ("", 50)
    return f


@pytest.mark.asyncio
async def test_compare_promote_when_candidate_matches_baseline():
    bench = _tiny_bench()
    answers = {
        "base":   {"alpha": "alpha", "beta": "beta", "gamma": "gamma"},  # 3/3
        "cand":   {"alpha": "alpha", "beta": "beta", "gamma": "gamma"},  # 3/3
    }
    res = await fe.compare_models(
        "base", "cand",
        ask_fn=_scripted(answers), benchmark=bench, regression_pp=5,
    )
    assert res.decision == "promote"
    assert res.candidate_score == 1.0
    assert res.baseline_score == 1.0
    assert res.delta_pp == 0.0


@pytest.mark.asyncio
async def test_compare_promote_when_candidate_better():
    bench = _tiny_bench()
    answers = {
        "base":   {"alpha": "alpha"},                      # 1/3
        "cand":   {"alpha": "alpha", "beta": "beta"},      # 2/3
    }
    res = await fe.compare_models(
        "base", "cand",
        ask_fn=_scripted(answers), benchmark=bench, regression_pp=5,
    )
    assert res.decision == "promote"
    assert res.delta_pp > 0


@pytest.mark.asyncio
async def test_compare_reject_on_regression():
    bench = _tiny_bench()
    answers = {
        "base":   {"alpha": "alpha", "beta": "beta", "gamma": "gamma"},  # 3/3
        "cand":   {"alpha": "alpha"},                                    # 1/3
    }
    res = await fe.compare_models(
        "base", "cand",
        ask_fn=_scripted(answers), benchmark=bench, regression_pp=5,
    )
    assert res.decision == "reject"
    assert res.delta_pp < -5
    assert res.ok is False
    assert "Δ" in res.reason


@pytest.mark.asyncio
async def test_compare_within_threshold_still_promotes():
    """Slight regression but inside threshold → still promote."""
    bench = _tiny_bench()
    answers = {
        # baseline 100%, candidate 67% → 33pp drop, well past threshold
        # so we'll need a wider threshold for this case.
        "base":   {"alpha": "alpha", "beta": "beta", "gamma": "gamma"},
        "cand":   {"alpha": "alpha", "beta": "beta"},
    }
    res = await fe.compare_models(
        "base", "cand",
        ask_fn=_scripted(answers), benchmark=bench, regression_pp=50,
    )
    assert res.decision == "promote"


@pytest.mark.asyncio
async def test_compare_no_baseline_returns_no_baseline_status():
    bench = _tiny_bench()
    answers = {"cand": {"alpha": "alpha"}}
    res = await fe.compare_models(
        "", "cand",
        ask_fn=_scripted(answers), benchmark=bench,
    )
    assert res.decision == "no_baseline"
    assert res.ok is False


@pytest.mark.asyncio
async def test_compare_returns_reject_when_holdout_missing(monkeypatch):
    """No hold-out benchmark loadable → reject (caller has no ground
    truth to judge the candidate)."""
    monkeypatch.setattr(fe, "load_holdout", lambda *a, **kw: None)

    async def ask(m, p):
        return ("nope", 1)

    res = await fe.compare_models("base", "cand", ask_fn=ask)
    assert res.decision == "reject"
    assert "not found" in res.reason


@pytest.mark.asyncio
async def test_compare_publishes_eval_score_metrics():
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()

    bench = _tiny_bench()
    answers = {
        "base":   {"alpha": "alpha", "beta": "beta", "gamma": "gamma"},
        "cand":   {"alpha": "alpha", "beta": "beta"},
    }
    await fe.compare_models(
        "base", "cand",
        ask_fn=_scripted(answers), benchmark=bench,
    )
    samples = list(m.finetune_eval_score.collect()[0].samples)
    by_model = {s.labels.get("model"): s.value for s in samples}
    assert by_model.get("base") == 1.0
    assert by_model.get("cand") == pytest.approx(2 / 3)
