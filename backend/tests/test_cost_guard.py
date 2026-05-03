"""AB.6 — Cost estimator + budget guard tests.

Locks:
  - Pricing math: real-time, batch 50% off, cache read 90% off
    (75% on Haiku), cache write +25%, mixed cache
  - Estimator: builds CostEstimate with all scope keys, propagates
    call_id, raises on unknown model
  - Three-tier alert classification: <80% none, [80,100) warn,
    [100,120) cap, >=120 over
  - Budget check: per_batch / daily / monthly caps each trigger,
    multi-scope (global + priority + task_type) all evaluated,
    block action surfaces in BudgetCheck.allowed=False
  - Alert sink invoked + alert_sink exceptions caught, alerts
    persisted to store, alerts_since query
  - In-memory store: matches by scope kind correctly across all
    5 scope kinds (global / workspace / priority / task_type / model)
  - actual cost overrides estimate in spend calc once recorded

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §6
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.agents.cost_guard import (
    BudgetAlert,
    BudgetCap,
    CostActual,
    CostEstimate,
    CostGuard,
    InMemoryCostStore,
    PRICING_TABLE,
    ScopeKey,
    estimate_cost,
    get_pricing,
)


# ─── Pricing math ────────────────────────────────────────────────


def test_pricing_table_has_4x_models():
    assert "claude-opus-4-7" in PRICING_TABLE
    assert "claude-sonnet-4-6" in PRICING_TABLE
    assert "claude-haiku-4-5-20251001" in PRICING_TABLE


def test_get_pricing_unknown_raises():
    with pytest.raises(KeyError, match="No pricing"):
        get_pricing("claude-unknown-x")


def test_pricing_realtime_sonnet():
    p = get_pricing("claude-sonnet-4-6")
    cost = p.cost_usd(input_tokens=1_000_000, output_tokens=1_000_000)
    # 1M input * $3 + 1M output * $15 = $18
    assert cost == pytest.approx(18.0, abs=0.001)


def test_pricing_batch_50_pct_off():
    p = get_pricing("claude-sonnet-4-6")
    realtime = p.cost_usd(input_tokens=1_000_000, output_tokens=1_000_000)
    batch = p.cost_usd(input_tokens=1_000_000, output_tokens=1_000_000, is_batch=True)
    assert batch == pytest.approx(realtime * 0.5, abs=0.001)


def test_pricing_cache_read_90_off_sonnet():
    """Sonnet cache read = $0.30/MTok (90% off $3.00 input)."""
    p = get_pricing("claude-sonnet-4-6")
    cost = p.cost_usd(
        input_tokens=0, output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert cost == pytest.approx(0.30, abs=0.001)


def test_pricing_cache_write_premium_sonnet():
    """Sonnet cache write = $3.75/MTok (input $3.00 + 25% premium)."""
    p = get_pricing("claude-sonnet-4-6")
    cost = p.cost_usd(
        input_tokens=0, output_tokens=0,
        cache_creation_tokens=1_000_000,
    )
    assert cost == pytest.approx(3.75, abs=0.001)


def test_pricing_haiku_75_off_cache():
    """Haiku cache read = $0.10/MTok (90% off $1.00 input)."""
    p = get_pricing("claude-haiku-4-5-20251001")
    cost = p.cost_usd(
        input_tokens=0, output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert cost == pytest.approx(0.10, abs=0.001)


def test_pricing_mixed_cache_realtime():
    """Realistic call: 5K fresh input + 10K cache read + 1K cache write + 2K output."""
    p = get_pricing("claude-sonnet-4-6")
    cost = p.cost_usd(
        input_tokens=5_000,
        output_tokens=2_000,
        cache_read_tokens=10_000,
        cache_creation_tokens=1_000,
    )
    expected = (
        5_000 * 3.0 / 1_000_000      # 0.015
        + 2_000 * 15.0 / 1_000_000    # 0.030
        + 10_000 * 0.30 / 1_000_000   # 0.003
        + 1_000 * 3.75 / 1_000_000    # 0.00375
    )
    assert cost == pytest.approx(expected, abs=0.0001)


def test_pricing_opus_realtime():
    p = get_pricing("claude-opus-4-7")
    # 100K input + 50K output: 100K * $15 + 50K * $75 = $1.5 + $3.75 = $5.25
    cost = p.cost_usd(input_tokens=100_000, output_tokens=50_000)
    assert cost == pytest.approx(5.25, abs=0.001)


# ─── estimate_cost helper ────────────────────────────────────────


def test_estimate_cost_propagates_scope():
    est = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
        workspace="dev",
        priority="HD",
        task_type="hd_parse_kicad",
        call_id="my_id",
    )
    assert est.call_id == "my_id"
    assert est.workspace == "dev"
    assert est.priority == "HD"
    assert est.task_type == "hd_parse_kicad"
    assert est.cost_usd_estimated > 0


def test_estimate_cost_unknown_model_raises():
    with pytest.raises(KeyError):
        estimate_cost(
            model="claude-bogus-99",
            input_tokens=10,
            output_tokens=10,
        )


def test_estimate_cost_auto_call_id():
    est = estimate_cost(
        model="claude-sonnet-4-6", input_tokens=10, output_tokens=10
    )
    assert est.call_id.startswith("call_")
    assert len(est.call_id) > 10


def test_estimate_cost_batch_cheaper_than_realtime():
    rt = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=10_000, output_tokens=5_000,
    )
    batch = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=10_000, output_tokens=5_000,
        is_batch=True,
    )
    assert batch.cost_usd_estimated == pytest.approx(rt.cost_usd_estimated * 0.5, abs=0.0001)


# ─── Alert level classification ──────────────────────────────────


@pytest.mark.asyncio
async def test_classify_level_below_warn():
    g = CostGuard()
    assert g._classify_level(projected=70, limit=100) is None


@pytest.mark.asyncio
async def test_classify_level_warn_80():
    g = CostGuard()
    assert g._classify_level(projected=80, limit=100) == "warn_80"
    assert g._classify_level(projected=99.99, limit=100) == "warn_80"


@pytest.mark.asyncio
async def test_classify_level_cap_100():
    g = CostGuard()
    assert g._classify_level(projected=100, limit=100) == "cap_100"
    assert g._classify_level(projected=119.99, limit=100) == "cap_100"


@pytest.mark.asyncio
async def test_classify_level_over_120():
    g = CostGuard()
    assert g._classify_level(projected=120, limit=100) == "over_120"
    assert g._classify_level(projected=500, limit=100) == "over_120"


@pytest.mark.asyncio
async def test_classify_level_zero_limit_returns_none():
    """Zero / negative limit means 'no enforcement, just track'."""
    g = CostGuard()
    assert g._classify_level(projected=10, limit=0) is None


# ─── Budget check / alerts ───────────────────────────────────────


@pytest.mark.asyncio
async def test_check_no_budget_allows_all():
    g = CostGuard()
    est = estimate_cost(
        model="claude-sonnet-4-6", input_tokens=1000, output_tokens=500,
        priority="HD",
    )
    result = await g.check(est)
    assert result.allowed
    assert result.triggered_alerts == ()


@pytest.mark.asyncio
async def test_check_per_batch_cap_blocks():
    g = CostGuard()
    await g.configure_budget(
        ScopeKey(kind="priority", key="HD"),
        per_batch_limit_usd=0.05,
    )
    # Pricey estimate: 1M input on Sonnet = $3
    est = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=1_000_000, output_tokens=0,
        priority="HD",
    )
    result = await g.check(est, per_batch_observed_usd=0.0)
    assert not result.allowed
    assert "Budget exceeded" in result.reason
    assert any(a.level == "over_120" for a in result.triggered_alerts)
    # Alert persisted
    saved = await g.alerts_since()
    assert len(saved) >= 1


@pytest.mark.asyncio
async def test_check_per_batch_skipped_without_observed():
    """Without per_batch_observed_usd passed, per_batch cap is skipped."""
    g = CostGuard()
    await g.configure_budget(
        ScopeKey(kind="priority", key="HD"),
        per_batch_limit_usd=0.001,  # tight
    )
    est = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=10_000, output_tokens=5_000,
        priority="HD",
    )
    # No per_batch_observed_usd → skip per_batch enforcement
    result = await g.check(est)
    assert result.allowed


@pytest.mark.asyncio
async def test_check_daily_cap_blocks():
    g = CostGuard()
    # Daily cap $1
    await g.configure_budget(
        ScopeKey(kind="priority", key="HD"),
        daily_limit_usd=1.0,
    )
    # Submit and record a $0.5 actual
    est1 = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=100_000, output_tokens=10_000,
        priority="HD",
    )
    await g.record_estimate(est1)
    await g.record_actual(CostActual(call_id=est1.call_id, input_tokens=100_000,
                                      output_tokens=10_000, cost_usd=0.5))
    # Next call is $0.7 → projected $1.2 > $1 cap → block
    est2 = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=200_000, output_tokens=10_000,
        priority="HD",
    )
    result = await g.check(est2)
    assert not result.allowed


@pytest.mark.asyncio
async def test_check_monthly_cap_blocks():
    g = CostGuard()
    await g.configure_budget(
        ScopeKey(kind="priority", key="HD"),
        monthly_limit_usd=1.0,
    )
    est1 = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=100_000, output_tokens=10_000,
        priority="HD",
    )
    await g.record_estimate(est1)
    await g.record_actual(CostActual(call_id=est1.call_id, input_tokens=100_000,
                                      output_tokens=10_000, cost_usd=0.5))

    est2 = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=200_000, output_tokens=10_000,
        priority="HD",
    )
    result = await g.check(est2)
    assert not result.allowed
    assert any(a.period == "monthly" for a in result.triggered_alerts)


@pytest.mark.asyncio
async def test_check_warn_80_does_not_block():
    """80% trip fires warn but doesn't block submission."""
    g = CostGuard()
    await g.configure_budget(
        ScopeKey(kind="priority", key="HD"),
        daily_limit_usd=10.0,
    )
    # Submit estimate for $8.5 (85% of $10 cap, in warn band)
    est = estimate_cost(
        model="claude-opus-4-7",
        input_tokens=200_000, output_tokens=80_000,  # 200K*$15 + 80K*$75 = $3 + $6 = $9
        priority="HD",
    )
    result = await g.check(est)
    assert result.allowed  # warn doesn't block
    assert any(a.level == "warn_80" for a in result.triggered_alerts)


@pytest.mark.asyncio
async def test_check_cap_100_action_throttle():
    """100% cap fires throttle action."""
    g = CostGuard()
    await g.configure_budget(
        ScopeKey(kind="priority", key="HD"),
        daily_limit_usd=10.0,
    )
    # Spend something already
    est_prior = estimate_cost(
        model="claude-sonnet-4-6", input_tokens=100_000, output_tokens=100_000,
        priority="HD",
    )
    # 100K*$3 + 100K*$15 = $1.8
    await g.record_estimate(est_prior)
    await g.record_actual(CostActual(call_id=est_prior.call_id, input_tokens=100_000,
                                      output_tokens=100_000, cost_usd=1.8))
    # Now check estimate for $9 → projected $10.8 → 108% of $10 = cap_100
    est = estimate_cost(
        model="claude-opus-4-7",
        input_tokens=200_000, output_tokens=80_000,
        priority="HD",
    )
    result = await g.check(est)
    cap_alerts = [a for a in result.triggered_alerts if a.level == "cap_100"]
    assert cap_alerts, f"Expected cap_100 alert, got {[a.level for a in result.triggered_alerts]}"
    assert all(a.action == "throttle" for a in cap_alerts)
    # cap_100 throttles but doesn't block
    assert result.allowed


@pytest.mark.asyncio
async def test_check_multi_scope_aggregates_alerts():
    """A call hitting a global + priority cap fires alerts for each."""
    g = CostGuard()
    await g.configure_budget(ScopeKey("global", "*"), daily_limit_usd=10.0)
    await g.configure_budget(ScopeKey("priority", "HD"), daily_limit_usd=5.0)
    # $9 estimate → 90% of global ($10), 180% of HD priority ($5)
    est = estimate_cost(
        model="claude-opus-4-7",
        input_tokens=200_000, output_tokens=80_000,  # ~$9
        priority="HD",
    )
    result = await g.check(est)
    scopes_hit = {a.scope.kind for a in result.triggered_alerts}
    assert "global" in scopes_hit
    assert "priority" in scopes_hit


@pytest.mark.asyncio
async def test_check_disabled_budget_skipped():
    """enabled=False budget is ignored."""
    g = CostGuard()
    await g.configure_budget(
        ScopeKey(kind="priority", key="HD"),
        daily_limit_usd=0.0001,  # absurdly tight
        enabled=False,
    )
    est = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=10_000, output_tokens=5_000,
        priority="HD",
    )
    result = await g.check(est)
    assert result.allowed
    assert result.triggered_alerts == ()


@pytest.mark.asyncio
async def test_check_alert_sink_invoked():
    g_calls: list[BudgetAlert] = []

    async def sink(alert: BudgetAlert) -> None:
        g_calls.append(alert)

    g = CostGuard(alert_sink=sink)
    await g.configure_budget(ScopeKey("priority", "HD"), daily_limit_usd=0.01)
    est = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=10_000, output_tokens=10_000,
        priority="HD",
    )
    await g.check(est)
    assert len(g_calls) >= 1
    assert all(a.scope.key == "HD" for a in g_calls)


@pytest.mark.asyncio
async def test_check_alert_sink_exception_does_not_break():
    """Sink raising must not break the check (logged, not propagated)."""

    async def boom(alert: BudgetAlert) -> None:
        raise RuntimeError("downstream burst")

    g = CostGuard(alert_sink=boom)
    await g.configure_budget(ScopeKey("priority", "HD"), daily_limit_usd=0.01)
    est = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=10_000, output_tokens=10_000,
        priority="HD",
    )
    # Doesn't raise even though sink does
    result = await g.check(est)
    assert result.triggered_alerts


# ─── Scope matching ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spend_matches_global_scope():
    store = InMemoryCostStore()
    g = CostGuard(store=store)
    est_a = estimate_cost(model="claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0,
                          priority="HD", workspace="prod")
    est_b = estimate_cost(model="claude-sonnet-4-6", input_tokens=2_000_000, output_tokens=0,
                          priority="WP", workspace="dev")
    await g.record_estimate(est_a)
    await g.record_estimate(est_b)
    total = await store.spend_in_period(ScopeKey("global", "*"), "daily")
    assert total == pytest.approx(est_a.cost_usd_estimated + est_b.cost_usd_estimated)


@pytest.mark.asyncio
async def test_spend_matches_priority_scope():
    store = InMemoryCostStore()
    g = CostGuard(store=store)
    est_hd = estimate_cost(model="claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0,
                            priority="HD")
    est_wp = estimate_cost(model="claude-sonnet-4-6", input_tokens=2_000_000, output_tokens=0,
                            priority="WP")
    await g.record_estimate(est_hd)
    await g.record_estimate(est_wp)
    hd_total = await store.spend_in_period(ScopeKey("priority", "HD"), "daily")
    wp_total = await store.spend_in_period(ScopeKey("priority", "WP"), "daily")
    assert hd_total == pytest.approx(est_hd.cost_usd_estimated)
    assert wp_total == pytest.approx(est_wp.cost_usd_estimated)


@pytest.mark.asyncio
async def test_spend_matches_workspace_scope():
    store = InMemoryCostStore()
    g = CostGuard(store=store)
    est_prod = estimate_cost(model="claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0,
                             workspace="prod")
    est_dev = estimate_cost(model="claude-sonnet-4-6", input_tokens=2_000_000, output_tokens=0,
                            workspace="dev")
    await g.record_estimate(est_prod)
    await g.record_estimate(est_dev)
    prod_total = await store.spend_in_period(ScopeKey("workspace", "prod"), "daily")
    dev_total = await store.spend_in_period(ScopeKey("workspace", "dev"), "daily")
    assert prod_total == pytest.approx(est_prod.cost_usd_estimated)
    assert dev_total == pytest.approx(est_dev.cost_usd_estimated)


@pytest.mark.asyncio
async def test_spend_matches_task_type_scope():
    store = InMemoryCostStore()
    g = CostGuard(store=store)
    est_parse = estimate_cost(model="claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0,
                              task_type="hd_parse_kicad")
    est_diff = estimate_cost(model="claude-sonnet-4-6", input_tokens=2_000_000, output_tokens=0,
                             task_type="hd_diff")
    await g.record_estimate(est_parse)
    await g.record_estimate(est_diff)
    parse_total = await store.spend_in_period(ScopeKey("task_type", "hd_parse_kicad"), "daily")
    diff_total = await store.spend_in_period(ScopeKey("task_type", "hd_diff"), "daily")
    assert parse_total == pytest.approx(est_parse.cost_usd_estimated)
    assert diff_total == pytest.approx(est_diff.cost_usd_estimated)


@pytest.mark.asyncio
async def test_spend_matches_model_scope():
    store = InMemoryCostStore()
    g = CostGuard(store=store)
    est_sonnet = estimate_cost(model="claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0)
    est_opus = estimate_cost(model="claude-opus-4-7", input_tokens=1_000_000, output_tokens=0)
    await g.record_estimate(est_sonnet)
    await g.record_estimate(est_opus)
    sonnet_total = await store.spend_in_period(ScopeKey("model", "claude-sonnet-4-6"), "daily")
    opus_total = await store.spend_in_period(ScopeKey("model", "claude-opus-4-7"), "daily")
    assert sonnet_total == pytest.approx(3.0, abs=0.01)  # 1M * $3
    assert opus_total == pytest.approx(15.0, abs=0.01)   # 1M * $15


@pytest.mark.asyncio
async def test_actual_overrides_estimate_in_spend():
    """Once CostActual is recorded, spend uses the actual not the estimate."""
    store = InMemoryCostStore()
    g = CostGuard(store=store)
    est = estimate_cost(model="claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0,
                        priority="HD")
    await g.record_estimate(est)
    # Estimate said $3; actual usage was $5 (more output than predicted)
    await g.record_actual(CostActual(call_id=est.call_id, input_tokens=1_000_000,
                                      output_tokens=200_000, cost_usd=5.0))
    total = await store.spend_in_period(ScopeKey("priority", "HD"), "daily")
    assert total == pytest.approx(5.0, abs=0.01)


# ─── alerts_since query ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_alerts_since_filters_by_scope():
    g = CostGuard()
    await g.configure_budget(ScopeKey("priority", "HD"), daily_limit_usd=0.01)
    await g.configure_budget(ScopeKey("priority", "WP"), daily_limit_usd=0.01)
    await g.check(estimate_cost(model="claude-sonnet-4-6",
                                  input_tokens=10_000, output_tokens=10_000, priority="HD"))
    await g.check(estimate_cost(model="claude-sonnet-4-6",
                                  input_tokens=10_000, output_tokens=10_000, priority="WP"))
    hd_alerts = await g.alerts_since(ScopeKey("priority", "HD"))
    wp_alerts = await g.alerts_since(ScopeKey("priority", "WP"))
    assert all(a.scope.key == "HD" for a in hd_alerts)
    assert all(a.scope.key == "WP" for a in wp_alerts)
    assert hd_alerts and wp_alerts


@pytest.mark.asyncio
async def test_alerts_since_filters_by_time():
    g = CostGuard()
    await g.configure_budget(ScopeKey("priority", "HD"), daily_limit_usd=0.01)
    early = datetime(2026, 1, 1, tzinfo=timezone.utc)
    late = datetime(2027, 1, 1, tzinfo=timezone.utc)
    await g.check(
        estimate_cost(model="claude-sonnet-4-6", input_tokens=10_000, output_tokens=10_000, priority="HD"),
        now=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    after_early = await g.alerts_since(since=early)
    after_late = await g.alerts_since(since=late)
    assert len(after_early) >= 1
    assert len(after_late) == 0


# ─── Budget config ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_configure_budget_persists():
    g = CostGuard()
    cap = await g.configure_budget(
        ScopeKey("priority", "HD"),
        daily_limit_usd=10.0,
        monthly_limit_usd=200.0,
        per_batch_limit_usd=5.0,
    )
    fetched = await g.store.get_budget(ScopeKey("priority", "HD"))
    assert fetched == cap
    assert fetched.daily_limit_usd == 10.0
    assert fetched.monthly_limit_usd == 200.0
    assert fetched.per_batch_limit_usd == 5.0


@pytest.mark.asyncio
async def test_list_budgets_enabled_filter():
    g = CostGuard()
    await g.configure_budget(ScopeKey("priority", "HD"), daily_limit_usd=10.0, enabled=True)
    await g.configure_budget(ScopeKey("priority", "WP"), daily_limit_usd=5.0, enabled=False)
    all_budgets = await g.store.list_budgets()
    enabled_only = await g.store.list_budgets(enabled_only=True)
    assert len(all_budgets) == 2
    assert len(enabled_only) == 1
    assert enabled_only[0].scope.key == "HD"
