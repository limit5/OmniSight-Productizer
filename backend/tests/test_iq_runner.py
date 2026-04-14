"""Phase 63-D D2 — IQ benchmark runner with injectable ask_fn."""

from __future__ import annotations

import asyncio

import pytest

from backend import iq_benchmark as ib
from backend import iq_runner as ir


def _bench(name: str = "b") -> ib.IQBenchmark:
    return ib.IQBenchmark(
        name=name, schema_version=1, description="",
        questions=[
            ib.IQQuestion(id="q1", prompt="say alpha",
                          expected_keywords=["alpha"]),
            ib.IQQuestion(id="q2", prompt="say beta",
                          expected_keywords=["beta"]),
            ib.IQQuestion(id="q3", prompt="say gamma",
                          expected_keywords=["gamma"]),
        ],
    )


def _scripted_ask(answers: dict[str, str], tokens: int = 100):
    """Return an ask_fn that maps prompt → canned answer."""
    async def f(model: str, prompt: str) -> tuple[str, int]:
        for kw, ans in answers.items():
            if kw in prompt:
                return (ans, tokens)
        return ("", tokens)
    return f


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  run_benchmark — happy path + token cap
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_run_perfect_model():
    bench = _bench()
    ask = _scripted_ask({"alpha": "alpha here", "beta": "beta now",
                         "gamma": "gamma found"}, tokens=50)
    r = await ir.run_benchmark(bench, "perfect", ask_fn=ask, token_budget=10_000)
    assert r.score.pass_count == 3
    assert r.tokens_used == 150
    assert r.truncated_at_question is None
    assert r.errors == []


@pytest.mark.asyncio
async def test_run_partial_model():
    bench = _bench()
    ask = _scripted_ask({"alpha": "alpha", "beta": "wrong",
                         "gamma": "gamma"}, tokens=10)
    r = await ir.run_benchmark(bench, "partial", ask_fn=ask, token_budget=10_000)
    assert r.score.pass_count == 2
    assert r.score.weighted_score == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_token_budget_truncates_run():
    bench = _bench()
    # 3 questions × 80 tokens = 240; budget 100 → truncates at q2
    # (q1 used 80, then q2's check sees 80 < 100 → runs → 160 ≥ 100 stops next)
    ask = _scripted_ask({"alpha": "alpha", "beta": "beta",
                         "gamma": "gamma"}, tokens=80)
    r = await ir.run_benchmark(bench, "tight", ask_fn=ask, token_budget=100)
    assert r.truncated_at_question == "q3"
    # Only the first two ran; score reflects partial answers.
    assert r.score.pass_count == 2
    assert r.tokens_used == 160


@pytest.mark.asyncio
async def test_zero_budget_truncates_immediately():
    bench = _bench()
    ask = _scripted_ask({"alpha": "alpha"}, tokens=10)
    r = await ir.run_benchmark(bench, "broke", ask_fn=ask, token_budget=0)
    assert r.truncated_at_question == "q1"
    assert r.score.pass_count == 0
    assert r.tokens_used == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Errors / timeouts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_per_question_timeout_skips_and_records():
    async def slow(model, prompt):
        await asyncio.sleep(2.0)
        return ("nope", 50)

    bench = _bench()
    r = await ir.run_benchmark(
        bench, "slow", ask_fn=slow,
        token_budget=10_000, per_question_timeout_s=0.05,
    )
    assert len(r.errors) == 3
    assert all("timeout" in e for e in r.errors)
    assert r.score.pass_count == 0


@pytest.mark.asyncio
async def test_exception_in_ask_fn_counts_as_fail_not_crash():
    async def boomer(model, prompt):
        raise RuntimeError("provider down")
    bench = _bench()
    r = await ir.run_benchmark(
        bench, "broken", ask_fn=boomer, token_budget=10_000,
    )
    assert len(r.errors) == 3
    assert all("provider down" in e for e in r.errors)
    assert r.score.pass_count == 0


@pytest.mark.asyncio
async def test_partial_failure_other_questions_proceed():
    """Failing on q2 must NOT stop q1 + q3 from running."""
    calls = {"n": 0}

    async def flaky(model, prompt):
        calls["n"] += 1
        if "beta" in prompt:
            raise RuntimeError("flake")
        if "alpha" in prompt:
            return ("alpha works", 30)
        if "gamma" in prompt:
            return ("gamma works", 30)
        return ("", 0)

    bench = _bench()
    r = await ir.run_benchmark(bench, "flaky", ask_fn=flaky, token_budget=10_000)
    assert calls["n"] == 3
    assert r.score.pass_count == 2  # q1 + q3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  run_all — cross product + per-model budget
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_run_all_returns_cross_product_in_order():
    b1 = _bench("set1")
    b2 = _bench("set2")
    ask = _scripted_ask({"alpha": "alpha"}, tokens=10)
    reports = await ir.run_all([b1, b2], ["m1", "m2"],
                               ask_fn=ask, token_budget_per_model=10_000)
    pairs = [(r.score.model, r.score.benchmark) for r in reports]
    assert pairs == [
        ("m1", "set1"), ("m1", "set2"),
        ("m2", "set1"), ("m2", "set2"),
    ]


@pytest.mark.asyncio
async def test_run_all_per_model_budget_isolates_models():
    """If model m1 burns its budget, m2 must STILL get its full budget."""
    b1 = _bench("set1")
    # 3 Q × 50 tokens = 150 per model per benchmark.
    ask = _scripted_ask({"alpha": "alpha", "beta": "beta",
                         "gamma": "gamma"}, tokens=50)
    reports = await ir.run_all([b1, b1], ["m1", "m2"],
                               ask_fn=ask, token_budget_per_model=200)
    # m1: first benchmark uses 150; second has only 50 left → truncates.
    m1_reports = [r for r in reports if r.score.model == "m1"]
    assert m1_reports[0].truncated_at_question is None
    assert m1_reports[1].truncated_at_question is not None  # truncated
    # m2 starts fresh — first benchmark again uses 150; second truncates same way.
    m2_reports = [r for r in reports if r.score.model == "m2"]
    assert m2_reports[0].truncated_at_question is None
    assert m2_reports[1].truncated_at_question is not None
    # Critically: m1 burning didn't affect m2's first benchmark.
    assert m2_reports[0].tokens_used == 150
