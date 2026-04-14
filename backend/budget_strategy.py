"""Phase 47C — Budget Strategy selector.

Four strategies adjust the system's spend / quality trade-off:

    quality      → high-tier models, generous retries (3), no downgrade
    balanced     → default — the existing behavior (2 retries, downgrade at 90%)
    cost_saver   → cheap models preferred, fewer retries (1), aggressive downgrade
    sprint       → parallel-heavy, moderate retries (2), upgrade model tier

The strategy resolves to concrete knobs consumed by `model_router` and
`nodes._handle_llm_error`:

    model_tier: "premium" | "default" | "budget"
    max_retries: int
    downgrade_at_usage_pct: int    # token-budget threshold to auto-downgrade
    prefer_parallel: bool

Callers that don't know about strategies keep using the global defaults;
strategy-aware callers read `get_tuning()` at decision time.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from enum import Enum


class BudgetStrategy(str, Enum):
    quality = "quality"
    balanced = "balanced"
    cost_saver = "cost_saver"
    sprint = "sprint"


@dataclass(frozen=True)
class Tuning:
    strategy: BudgetStrategy
    model_tier: str               # "premium" | "default" | "budget"
    max_retries: int
    downgrade_at_usage_pct: int   # 0-100
    freeze_at_usage_pct: int      # 0-100
    prefer_parallel: bool

    def to_dict(self) -> dict:
        d = asdict(self)
        d["strategy"] = self.strategy.value
        return d


_TUNINGS: dict[BudgetStrategy, Tuning] = {
    BudgetStrategy.quality: Tuning(
        strategy=BudgetStrategy.quality,
        model_tier="premium",
        max_retries=3,
        downgrade_at_usage_pct=100,  # never auto-downgrade
        freeze_at_usage_pct=100,
        prefer_parallel=False,
    ),
    BudgetStrategy.balanced: Tuning(
        strategy=BudgetStrategy.balanced,
        model_tier="default",
        max_retries=2,
        downgrade_at_usage_pct=90,
        freeze_at_usage_pct=100,
        prefer_parallel=False,
    ),
    BudgetStrategy.cost_saver: Tuning(
        strategy=BudgetStrategy.cost_saver,
        model_tier="budget",
        max_retries=1,
        downgrade_at_usage_pct=70,
        freeze_at_usage_pct=95,
        prefer_parallel=False,
    ),
    BudgetStrategy.sprint: Tuning(
        strategy=BudgetStrategy.sprint,
        model_tier="default",
        max_retries=2,
        downgrade_at_usage_pct=95,
        freeze_at_usage_pct=100,
        prefer_parallel=True,
    ),
}


# Fix-B B7: sync-only lock; awaits happen outside. See decision_engine.py.
_state_lock = threading.Lock()
_current: BudgetStrategy = BudgetStrategy.balanced


def list_strategies() -> list[dict]:
    return [_TUNINGS[s].to_dict() for s in BudgetStrategy]


def get_strategy() -> BudgetStrategy:
    with _state_lock:
        return _current


def get_tuning() -> Tuning:
    return _TUNINGS[get_strategy()]


def set_strategy(strategy: BudgetStrategy | str) -> Tuning:
    global _current
    if isinstance(strategy, str):
        try:
            strategy = BudgetStrategy(strategy)
        except ValueError as exc:
            raise ValueError(f"unknown strategy: {strategy}") from exc
    with _state_lock:
        prev = _current
        _current = strategy
    try:
        from backend.events import bus as _bus
        _bus.publish("budget_strategy_changed", {
            "strategy": strategy.value,
            "previous": prev.value,
            "tuning": _TUNINGS[strategy].to_dict(),
        })
    except Exception as exc:
        # Fix-B B2: SSE publish failure is non-critical; surface at debug level.
        import logging as _l
        _l.getLogger(__name__).debug("budget_strategy SSE publish failed: %s", exc)
    return _TUNINGS[strategy]


def _reset_for_tests() -> None:
    global _current
    with _state_lock:
        _current = BudgetStrategy.balanced
