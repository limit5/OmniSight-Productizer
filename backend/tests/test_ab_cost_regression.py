"""AB.10.4 — Cost regression test (golden table).

Locks the dollar-per-token math against a hand-computed expected
value table for every (model, scenario) combination that OmniSight
production agents commonly hit. Runs on every CI invocation so
unintentional pricing-table edits + estimator math drift fail
loudly before reaching production.

If Anthropic publishes new pricing:
  1. Update PRICING_TABLE in backend/agents/cost_guard.py
  2. Re-run this test — it will fail with explicit diff
  3. Update the expected values in this file
  4. Document the change in HANDOFF.md (operator-visible)

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §6.1
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.agents.cost_guard import (
    PRICING_TABLE,
    estimate_cost,
    get_pricing,
)


# ─── Golden expected values (hand-computed from §6.1 pricing) ────


@dataclass(frozen=True)
class CostScenario:
    """One (model, scenario) tuple with hand-computed expected USD."""

    name: str
    model: str
    input_tokens: int
    output_tokens: int
    is_batch: bool
    cache_read_tokens: int
    cache_creation_tokens: int
    expected_usd: float
    tolerance: float = 0.0001  # 0.01 cent on small numbers


# Reference rates (per MTok, Tier 4):
#   Opus 4.7    $15 / $75 / $1.50 cache_read / $18.75 cache_write
#   Sonnet 4.6  $3 / $15 / $0.30 / $3.75
#   Haiku 4.5   $1 / $5 / $0.10 / $1.25
# Batch = 50% off input + output (cache rates unchanged).

GOLDEN_SCENARIOS: tuple[CostScenario, ...] = (
    # ── Opus baselines ──
    CostScenario(
        name="opus_realtime_basic",
        model="claude-opus-4-7",
        input_tokens=1_000_000, output_tokens=1_000_000,
        is_batch=False,
        cache_read_tokens=0, cache_creation_tokens=0,
        # 1M*$15 + 1M*$75 = $90
        expected_usd=90.0,
    ),
    CostScenario(
        name="opus_batch_basic",
        model="claude-opus-4-7",
        input_tokens=1_000_000, output_tokens=1_000_000,
        is_batch=True,
        cache_read_tokens=0, cache_creation_tokens=0,
        # 50% off both → $45
        expected_usd=45.0,
    ),

    # ── Sonnet baselines ──
    CostScenario(
        name="sonnet_realtime_basic",
        model="claude-sonnet-4-6",
        input_tokens=1_000_000, output_tokens=1_000_000,
        is_batch=False,
        cache_read_tokens=0, cache_creation_tokens=0,
        # 1M*$3 + 1M*$15 = $18
        expected_usd=18.0,
    ),
    CostScenario(
        name="sonnet_batch_basic",
        model="claude-sonnet-4-6",
        input_tokens=1_000_000, output_tokens=1_000_000,
        is_batch=True,
        cache_read_tokens=0, cache_creation_tokens=0,
        expected_usd=9.0,
    ),

    # ── Haiku baselines ──
    CostScenario(
        name="haiku_realtime_basic",
        model="claude-haiku-4-5-20251001",
        input_tokens=1_000_000, output_tokens=1_000_000,
        is_batch=False,
        cache_read_tokens=0, cache_creation_tokens=0,
        # 1M*$1 + 1M*$5 = $6
        expected_usd=6.0,
    ),

    # ── Cache scenarios ──
    CostScenario(
        name="sonnet_cache_read_only",
        model="claude-sonnet-4-6",
        input_tokens=0, output_tokens=0,
        is_batch=False,
        cache_read_tokens=1_000_000, cache_creation_tokens=0,
        # 1M * $0.30 = $0.30
        expected_usd=0.30,
    ),
    CostScenario(
        name="sonnet_cache_write_only",
        model="claude-sonnet-4-6",
        input_tokens=0, output_tokens=0,
        is_batch=False,
        cache_read_tokens=0, cache_creation_tokens=1_000_000,
        # 1M * $3.75 = $3.75
        expected_usd=3.75,
    ),
    CostScenario(
        name="opus_cache_read",
        model="claude-opus-4-7",
        input_tokens=0, output_tokens=0,
        is_batch=False,
        cache_read_tokens=1_000_000, cache_creation_tokens=0,
        # 1M * $1.50 = $1.50
        expected_usd=1.50,
    ),
    CostScenario(
        name="haiku_cache_read",
        model="claude-haiku-4-5-20251001",
        input_tokens=0, output_tokens=0,
        is_batch=False,
        cache_read_tokens=1_000_000, cache_creation_tokens=0,
        # 1M * $0.10 = $0.10
        expected_usd=0.10,
    ),

    # ── Realistic mixed scenarios ──
    CostScenario(
        name="hd_parser_typical_batch",
        model="claude-sonnet-4-6",
        input_tokens=5_000, output_tokens=2_000,
        is_batch=True,
        cache_read_tokens=0, cache_creation_tokens=0,
        # 5K*$1.50/M + 2K*$7.50/M = 0.0075 + 0.015 = 0.0225
        expected_usd=0.0225,
    ),
    CostScenario(
        name="hd_diff_typical_realtime",
        model="claude-opus-4-7",
        input_tokens=15_000, output_tokens=5_000,
        is_batch=False,
        cache_read_tokens=0, cache_creation_tokens=0,
        # 15K*$15/M + 5K*$75/M = 0.225 + 0.375 = 0.6
        expected_usd=0.6,
    ),
    CostScenario(
        name="hd_datasheet_vision_batch",
        model="claude-sonnet-4-6",
        input_tokens=10_000, output_tokens=3_000,
        is_batch=True,
        cache_read_tokens=0, cache_creation_tokens=0,
        # 10K*$1.50/M + 3K*$7.50/M = 0.015 + 0.0225 = 0.0375
        expected_usd=0.0375,
    ),
    CostScenario(
        name="cve_impact_batch",
        model="claude-sonnet-4-6",
        input_tokens=3_000, output_tokens=1_000,
        is_batch=True,
        cache_read_tokens=0, cache_creation_tokens=0,
        # 3K*$1.50/M + 1K*$7.50/M = 0.0045 + 0.0075 = 0.012
        expected_usd=0.012,
    ),
    CostScenario(
        name="cached_long_context_realtime",
        model="claude-sonnet-4-6",
        input_tokens=2_000, output_tokens=1_500,
        is_batch=False,
        cache_read_tokens=50_000,  # 50K cached prefix
        cache_creation_tokens=0,
        # 2K*$3/M + 1.5K*$15/M + 50K*$0.30/M = 0.006 + 0.0225 + 0.015 = 0.0435
        expected_usd=0.0435,
    ),
    CostScenario(
        name="full_complex_realtime",
        model="claude-opus-4-7",
        input_tokens=1_000, output_tokens=500,
        is_batch=False,
        cache_read_tokens=20_000, cache_creation_tokens=5_000,
        # 1K*$15/M + 0.5K*$75/M + 20K*$1.50/M + 5K*$18.75/M
        # = 0.015 + 0.0375 + 0.030 + 0.09375 = 0.17625
        expected_usd=0.17625,
    ),
)


# ─── Tests ───────────────────────────────────────────────────────


@pytest.mark.parametrize("scenario", GOLDEN_SCENARIOS, ids=lambda s: s.name)
def test_cost_regression_golden(scenario: CostScenario):
    """Each golden scenario's computed cost must match the hand-computed
    expected within tolerance. Drift means PRICING_TABLE changed without
    updating this fixture — fail loud."""
    pricing = get_pricing(scenario.model)
    actual = pricing.cost_usd(
        input_tokens=scenario.input_tokens,
        output_tokens=scenario.output_tokens,
        cache_read_tokens=scenario.cache_read_tokens,
        cache_creation_tokens=scenario.cache_creation_tokens,
        is_batch=scenario.is_batch,
    )
    assert actual == pytest.approx(
        scenario.expected_usd, abs=scenario.tolerance,
    ), (
        f"Cost regression in {scenario.name!r}: expected ${scenario.expected_usd}, "
        f"got ${actual}. PRICING_TABLE drift OR golden value stale; "
        f"see test_ab_cost_regression.py header."
    )


@pytest.mark.parametrize("scenario", GOLDEN_SCENARIOS, ids=lambda s: s.name)
def test_estimate_cost_matches_pricing_direct(scenario: CostScenario):
    """estimate_cost() should produce the same result as
    ModelPricing.cost_usd() — guards against the helper drifting from
    the pricing primitive."""
    direct = get_pricing(scenario.model).cost_usd(
        input_tokens=scenario.input_tokens,
        output_tokens=scenario.output_tokens,
        cache_read_tokens=scenario.cache_read_tokens,
        cache_creation_tokens=scenario.cache_creation_tokens,
        is_batch=scenario.is_batch,
    )
    via_estimate = estimate_cost(
        model=scenario.model,
        input_tokens=scenario.input_tokens,
        output_tokens=scenario.output_tokens,
        cache_read_tokens=scenario.cache_read_tokens,
        cache_creation_tokens=scenario.cache_creation_tokens,
        is_batch=scenario.is_batch,
    )
    assert via_estimate.cost_usd_estimated == pytest.approx(direct, abs=1e-9)


def test_pricing_table_completeness():
    """All four shipped models present in PRICING_TABLE — drift guard
    against accidental deletion."""
    required = {
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-20250514",
    }
    assert required.issubset(PRICING_TABLE.keys())


def test_all_models_have_complete_pricing():
    """Every entry must define all 4 rates (no None / 0 unintentional)."""
    for model_name, pricing in PRICING_TABLE.items():
        assert pricing.input_per_mtok > 0, f"{model_name}: input_per_mtok must be positive"
        assert pricing.output_per_mtok > 0, f"{model_name}: output_per_mtok must be positive"
        assert pricing.cache_read_per_mtok > 0, f"{model_name}: cache_read_per_mtok must be positive"
        assert pricing.cache_write_per_mtok > 0, f"{model_name}: cache_write_per_mtok must be positive"


def test_batch_discount_is_50_percent_for_all_models():
    """Sanity guard: every model's batch price = 50% of realtime
    (cache rates unaffected — those are published nett)."""
    for model_name, pricing in PRICING_TABLE.items():
        realtime_only = pricing.cost_usd(
            input_tokens=1_000_000, output_tokens=1_000_000,
        )
        batch_only = pricing.cost_usd(
            input_tokens=1_000_000, output_tokens=1_000_000, is_batch=True,
        )
        ratio = batch_only / realtime_only
        assert ratio == pytest.approx(0.5, abs=0.001), (
            f"{model_name}: batch ratio {ratio} != 0.5"
        )


def test_cache_read_is_substantial_discount():
    """Each model's cache_read should be cheaper than its input rate
    (otherwise caching is pointless). Anthropic publishes ~90% off."""
    for model_name, pricing in PRICING_TABLE.items():
        ratio = pricing.cache_read_per_mtok / pricing.input_per_mtok
        # Allow some slack — exactly 0.10 is published rate
        assert ratio < 0.20, (
            f"{model_name}: cache_read ratio {ratio} not a meaningful discount"
        )
