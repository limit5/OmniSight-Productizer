"""OP-1065 — CostGuard race audit fixtures.

These tests exercise the existing CostGuard path without changing production
code. They use in-process stores only: no DB, no network, no external systems.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents.cost_guard import (
    BudgetAlert,
    BudgetCap,
    CostActual,
    CostEstimate,
    CostGuard,
    ScopeKey,
    estimate_cost,
)


class _RaceStore:
    """Store mock that lets concurrent checks observe the same pre-write spend."""

    def __init__(self, budget: BudgetCap, spend_usd: float) -> None:
        self.budget = budget
        self.spend_usd = spend_usd
        self.alerts: list[BudgetAlert] = []
        self.reads = 0
        self._both_read = asyncio.Event()

    async def save_estimate(self, estimate: CostEstimate) -> None:
        return None

    async def get_estimate(self, call_id: str) -> CostEstimate | None:
        return None

    async def update_actual(self, actual: CostActual) -> None:
        return None

    async def spend_in_period(self, scope: ScopeKey, period: str, *, now=None) -> float:
        self.reads += 1
        if self.reads >= 2:
            self._both_read.set()
        await self._both_read.wait()
        return self.spend_usd

    async def upsert_budget(self, budget: BudgetCap) -> None:
        self.budget = budget

    async def get_budget(self, scope: ScopeKey) -> BudgetCap | None:
        return self.budget if scope == self.budget.scope else None

    async def list_budgets(self, *, enabled_only: bool = False) -> list[BudgetCap]:
        return [self.budget]

    async def save_alert(self, alert: BudgetAlert) -> None:
        self.alerts.append(alert)

    async def list_alerts(self, scope: ScopeKey | None = None, *, since=None) -> list[BudgetAlert]:
        return list(self.alerts)


class _TimedRecoveryStore:
    """Store mock where daily spend ages out at a known recovery timestamp."""

    def __init__(self, budget: BudgetCap, recovery_at: datetime) -> None:
        self.budget = budget
        self.recovery_at = recovery_at
        self.alerts: list[BudgetAlert] = []

    async def save_estimate(self, estimate: CostEstimate) -> None:
        return None

    async def get_estimate(self, call_id: str) -> CostEstimate | None:
        return None

    async def update_actual(self, actual: CostActual) -> None:
        return None

    async def spend_in_period(self, scope: ScopeKey, period: str, *, now=None) -> float:
        return 1.21 if now < self.recovery_at else 0.0

    async def upsert_budget(self, budget: BudgetCap) -> None:
        self.budget = budget

    async def get_budget(self, scope: ScopeKey) -> BudgetCap | None:
        return self.budget if scope == self.budget.scope else None

    async def list_budgets(self, *, enabled_only: bool = False) -> list[BudgetCap]:
        return [self.budget]

    async def save_alert(self, alert: BudgetAlert) -> None:
        self.alerts.append(alert)

    async def list_alerts(self, scope: ScopeKey | None = None, *, since=None) -> list[BudgetAlert]:
        return list(self.alerts)


@pytest.mark.asyncio
async def test_global_cap_race_allows_two_concurrent_cap100_checks():
    scope = ScopeKey("global", "*")
    store = _RaceStore(BudgetCap(scope=scope, daily_limit_usd=1.00), spend_usd=0.98)
    guard = CostGuard(store=store)
    est = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=10_000,
        output_tokens=0,
    )

    left, right = await asyncio.gather(guard.check(est), guard.check(est))

    assert left.allowed and right.allowed
    assert [a.level for a in left.triggered_alerts] == ["cap_100"]
    assert [a.level for a in right.triggered_alerts] == ["cap_100"]
    assert len(store.alerts) == 2


@pytest.mark.asyncio
async def test_per_ticket_cap_mid_call_hit_blocks_next_check():
    guard = CostGuard()
    scope = ScopeKey("priority", "HD")
    await guard.configure_budget(scope, daily_limit_usd=1.00)
    est = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=10_000,
        output_tokens=0,
        priority="HD",
        call_id="op1065-mid-call",
    )

    pre_submit = await guard.check(est)
    await guard.record_estimate(est)
    await guard.record_actual(
        CostActual(
            call_id="op1065-mid-call",
            input_tokens=10_000,
            output_tokens=0,
            cost_usd=1.21,
        )
    )
    retry = await guard.check(est)

    assert pre_submit.allowed
    assert not retry.allowed
    assert any(alert.level == "over_120" for alert in retry.triggered_alerts)


@pytest.mark.asyncio
async def test_wait_60s_recovery_allows_after_daily_spend_window_clears():
    now = datetime(2026, 5, 14, 1, 0, tzinfo=timezone.utc)
    scope = ScopeKey("global", "*")
    store = _TimedRecoveryStore(
        BudgetCap(scope=scope, daily_limit_usd=1.00),
        recovery_at=now + timedelta(seconds=60),
    )
    guard = CostGuard(store=store)
    est = estimate_cost(
        model="claude-sonnet-4-6",
        input_tokens=10_000,
        output_tokens=0,
    )

    blocked = await guard.check(est, now=now)
    recovered = await guard.check(est, now=now + timedelta(seconds=60))

    assert not blocked.allowed
    assert recovered.allowed
    assert recovered.triggered_alerts == ()
