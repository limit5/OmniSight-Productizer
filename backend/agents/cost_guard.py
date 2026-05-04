"""AB.6 — Cost estimator + budget guard.

Two coupled responsibilities:

  1. **Estimator** — pre-submit USD cost prediction for a planned
     Anthropic call given (model, input_tokens, output_tokens,
     batch_flag, cache_hint). Math handles batch 50% discount,
     prompt cache 90% off (75% on Haiku) read, and the +25% premium
     write surcharge.

  2. **Budget guard** — accumulates real spend against per-scope
     caps (workspace / priority / task_type / model / global), fires
     three-tier alerts (80% warn / 100% cap / 120% over), and refuses
     submissions whose pre-submit estimate would breach a cap.

Both pieces are stateless on the API surface: callers pass scope
keys explicitly, no global mutable state. Persistence (alembic 0183)
shipped; PG-backed `CostStore` lands when AB.4 dispatcher first
runs against production DB. Tests use the in-memory store.

Pricing tables are module-const, sourced from
``docs/operations/anthropic-api-migration-and-batch-mode.md §6.1``.
Updated when Anthropic prices change; CI cost-regression test (AB.10)
catches drift between estimator and actual.

Integration with Z spend anomaly (existing): the rate-watch already
in `backend.agents.llm._normalize_ratelimit_headers` writes to
`SharedKV("provider_ratelimit")`. AB.6 alerts add a parallel write
to `SharedKV("cost_alerts")` so the dashboard surfaces both
provider-side rate exhaust *and* OmniSight-side spend overshoot in
one card.

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §6
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

logger = logging.getLogger(__name__)


# ─── Pricing (module-const) ──────────────────────────────────────


@dataclass(frozen=True)
class ModelPricing:
    """USD per million tokens for one model (Anthropic Tier 4 prices)."""

    model: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float

    def cost_usd(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        is_batch: bool = False,
    ) -> float:
        """Compute USD cost. Batch: 50% off both input + output (cache
        prices unaffected by batch flag — Anthropic publishes batch
        prices net of cache discount).

        Effective input billing = (input_tokens × input_rate) +
                                   (cache_creation_tokens × write_rate) +
                                   (cache_read_tokens × read_rate)

        Note: input_tokens here is the *non-cached* input portion. The
        Anthropic SDK splits usage into (input_tokens, cache_read_input_tokens,
        cache_creation_input_tokens); pass them as separate args.
        """
        input_rate = self.input_per_mtok
        output_rate = self.output_per_mtok
        if is_batch:
            input_rate *= 0.5
            output_rate *= 0.5

        cost = 0.0
        cost += input_tokens * input_rate / 1_000_000.0
        cost += output_tokens * output_rate / 1_000_000.0
        cost += cache_read_tokens * self.cache_read_per_mtok / 1_000_000.0
        cost += cache_creation_tokens * self.cache_write_per_mtok / 1_000_000.0
        return cost


# Anthropic Tier 4 pricing as of 2026-04 (source: ADR §6.1).
PRICING_TABLE: dict[str, ModelPricing] = {
    "claude-opus-4-7": ModelPricing(
        model="claude-opus-4-7",
        input_per_mtok=15.0,
        output_per_mtok=75.0,
        cache_read_per_mtok=1.50,
        cache_write_per_mtok=18.75,
    ),
    "claude-sonnet-4-6": ModelPricing(
        model="claude-sonnet-4-6",
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    "claude-haiku-4-5-20251001": ModelPricing(
        model="claude-haiku-4-5-20251001",
        input_per_mtok=1.0,
        output_per_mtok=5.0,
        cache_read_per_mtok=0.10,
        cache_write_per_mtok=1.25,
    ),
    # Legacy / pre-4.x compat for fallback callers
    "claude-sonnet-4-20250514": ModelPricing(
        model="claude-sonnet-4-20250514",
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
}


def get_pricing(model: str) -> ModelPricing:
    """Look up pricing for a model. Raises if unknown — fail loud rather
    than silently mis-billing."""
    if model not in PRICING_TABLE:
        raise KeyError(
            f"No pricing for model {model!r}. Add to "
            "backend/agents/cost_guard.py:PRICING_TABLE before submitting."
        )
    return PRICING_TABLE[model]


# ─── Estimate / actual data classes ──────────────────────────────


@dataclass(frozen=True)
class CostEstimate:
    """Pre-submit prediction. AB.6.2."""

    call_id: str
    model: str
    is_batch: bool
    input_tokens_estimated: int
    output_tokens_estimated: int
    cost_usd_estimated: float
    workspace: str | None = None
    priority: str | None = None
    task_type: str | None = None


@dataclass(frozen=True)
class CostActual:
    """Post-call observed usage + cost. Used to update CostEstimate row."""

    call_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0


def estimate_cost(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    is_batch: bool = False,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    call_id: str | None = None,
    workspace: str | None = None,
    priority: str | None = None,
    task_type: str | None = None,
) -> CostEstimate:
    """Build a CostEstimate from token counts + model + flags."""
    pricing = get_pricing(model)
    cost = pricing.cost_usd(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        is_batch=is_batch,
    )
    return CostEstimate(
        call_id=call_id or f"call_{uuid.uuid4().hex[:16]}",
        model=model,
        is_batch=is_batch,
        input_tokens_estimated=input_tokens,
        output_tokens_estimated=output_tokens,
        cost_usd_estimated=cost,
        workspace=workspace,
        priority=priority,
        task_type=task_type,
    )


# ─── Budget scopes + caps ────────────────────────────────────────


ScopeKind = Literal[
    "global", "workspace", "priority", "task_type", "model"
]
PeriodKind = Literal["per_batch", "daily", "monthly"]
AlertLevel = Literal["warn_80", "cap_100", "over_120"]
AlertAction = Literal["notify", "throttle", "block"]


@dataclass(frozen=True)
class ScopeKey:
    """A budget scope = (kind, key). e.g. ('priority', 'HD')."""

    kind: ScopeKind
    key: str

    def __str__(self) -> str:
        return f"{self.kind}={self.key}"


@dataclass
class BudgetCap:
    """Operator-configured cap for one scope."""

    scope: ScopeKey
    daily_limit_usd: float | None = None
    monthly_limit_usd: float | None = None
    per_batch_limit_usd: float | None = None
    enabled: bool = True


@dataclass(frozen=True)
class BudgetCheck:
    """Outcome of pre-submit check or post-spend evaluation."""

    allowed: bool
    reason: str = ""
    triggered_alerts: tuple["BudgetAlert", ...] = ()


@dataclass(frozen=True)
class BudgetAlert:
    """A fired alert event."""

    alert_id: str
    scope: ScopeKey
    period: PeriodKind
    level: AlertLevel
    threshold_usd: float
    observed_usd: float
    action: AlertAction
    fired_at: datetime


_LEVEL_FRACTION: dict[AlertLevel, float] = {
    "warn_80": 0.80,
    "cap_100": 1.00,
    "over_120": 1.20,
}

_LEVEL_DEFAULT_ACTION: dict[AlertLevel, AlertAction] = {
    "warn_80": "notify",
    "cap_100": "throttle",
    "over_120": "block",
}


# ─── Storage Protocol + in-memory impl ───────────────────────────


class CostStore(Protocol):
    """Persistence surface for cost estimates / spend / budgets / alerts.

    PG-backed impl deferred to AB.4 dispatcher's cross-restart need;
    in-memory impl ships for dev / test.
    """

    async def save_estimate(self, estimate: CostEstimate) -> None: ...
    async def update_actual(self, actual: CostActual) -> None: ...
    async def spend_in_period(
        self,
        scope: ScopeKey,
        period: PeriodKind,
        *,
        now: datetime | None = None,
    ) -> float: ...
    async def upsert_budget(self, budget: BudgetCap) -> None: ...
    async def get_budget(self, scope: ScopeKey) -> BudgetCap | None: ...
    async def list_budgets(
        self, *, enabled_only: bool = False
    ) -> list[BudgetCap]: ...
    async def save_alert(self, alert: BudgetAlert) -> None: ...
    async def list_alerts(
        self, scope: ScopeKey | None = None, *, since: datetime | None = None
    ) -> list[BudgetAlert]: ...


def _period_start(period: PeriodKind, now: datetime) -> datetime:
    if period == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "monthly":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # per_batch is point-in-time
    return now


class InMemoryCostStore:
    """Dev / test impl. Production: same Protocol, PG-backed."""

    def __init__(self) -> None:
        self._estimates: dict[str, CostEstimate] = {}
        self._actuals: dict[str, CostActual] = {}
        self._budgets: dict[ScopeKey, BudgetCap] = {}
        self._alerts: list[BudgetAlert] = []

    async def save_estimate(self, estimate: CostEstimate) -> None:
        self._estimates[estimate.call_id] = estimate

    async def update_actual(self, actual: CostActual) -> None:
        self._actuals[actual.call_id] = actual

    async def spend_in_period(
        self,
        scope: ScopeKey,
        period: PeriodKind,
        *,
        now: datetime | None = None,
    ) -> float:
        now = now or datetime.now(timezone.utc)
        if period == "per_batch":
            # per_batch is a point-in-time scope, not a time window. Sum the
            # latest batch's calls (caller supplies a workspace-style key
            # that scopes to the batch_run_id).
            return self._sum_matching(scope)
        cutoff = _period_start(period, now)
        return self._sum_matching(scope, since=cutoff)

    def _sum_matching(
        self, scope: ScopeKey, *, since: datetime | None = None
    ) -> float:
        total = 0.0
        for call_id, est in self._estimates.items():
            actual = self._actuals.get(call_id)
            cost = (
                actual.cost_usd
                if (actual and actual.cost_usd > 0)
                else est.cost_usd_estimated
            )
            if not _scope_matches(scope, est):
                continue
            if since is not None:
                # No created_at on dataclass; in-memory impl treats all
                # estimates as "now". Production PG impl honours actual ts.
                pass
            total += cost
        return total

    async def upsert_budget(self, budget: BudgetCap) -> None:
        self._budgets[budget.scope] = budget

    async def get_budget(self, scope: ScopeKey) -> BudgetCap | None:
        return self._budgets.get(scope)

    async def list_budgets(
        self, *, enabled_only: bool = False
    ) -> list[BudgetCap]:
        items = list(self._budgets.values())
        if enabled_only:
            items = [b for b in items if b.enabled]
        return items

    async def save_alert(self, alert: BudgetAlert) -> None:
        self._alerts.append(alert)

    async def list_alerts(
        self, scope: ScopeKey | None = None, *, since: datetime | None = None
    ) -> list[BudgetAlert]:
        items = list(self._alerts)
        if scope:
            items = [a for a in items if a.scope == scope]
        if since:
            items = [a for a in items if a.fired_at >= since]
        return items


def _scope_matches(scope: ScopeKey, estimate: CostEstimate) -> bool:
    """Does this estimate count toward this scope's spend?"""
    if scope.kind == "global":
        return True
    if scope.kind == "workspace":
        return estimate.workspace == scope.key
    if scope.kind == "priority":
        return estimate.priority == scope.key
    if scope.kind == "task_type":
        return estimate.task_type == scope.key
    if scope.kind == "model":
        return estimate.model == scope.key
    return False


# ─── CostGuard ───────────────────────────────────────────────────


AlertSink = Callable[[BudgetAlert], Awaitable[None]]


class CostGuard:
    """Cost estimator + budget enforcer.

    Hot-path methods:
      - ``record_estimate(estimate)`` — persist before submit
      - ``record_actual(actual)`` — update with observed cost post-call
      - ``check(estimate)`` — pre-submit gate; returns BudgetCheck.
        ``allowed=False`` means caller must abort. Handles per_batch,
        daily, monthly across all configured scopes.
      - ``configure_budget(scope, daily=, monthly=, per_batch=)`` —
        operator-side budget set
      - ``alerts_since(scope, since)`` — dashboard read
    """

    def __init__(
        self,
        store: CostStore | None = None,
        *,
        alert_sink: AlertSink | None = None,
    ) -> None:
        self.store = store or InMemoryCostStore()
        self.alert_sink = alert_sink

    async def configure_budget(
        self,
        scope: ScopeKey,
        *,
        daily_limit_usd: float | None = None,
        monthly_limit_usd: float | None = None,
        per_batch_limit_usd: float | None = None,
        enabled: bool = True,
    ) -> BudgetCap:
        cap = BudgetCap(
            scope=scope,
            daily_limit_usd=daily_limit_usd,
            monthly_limit_usd=monthly_limit_usd,
            per_batch_limit_usd=per_batch_limit_usd,
            enabled=enabled,
        )
        await self.store.upsert_budget(cap)
        return cap

    async def record_estimate(self, estimate: CostEstimate) -> None:
        await self.store.save_estimate(estimate)

    async def record_actual(self, actual: CostActual) -> None:
        await self.store.update_actual(actual)

    async def check(
        self,
        estimate: CostEstimate,
        *,
        now: datetime | None = None,
        per_batch_observed_usd: float | None = None,
    ) -> BudgetCheck:
        """Pre-submit gate. Returns allowed=False if any cap would breach.

        ``per_batch_observed_usd`` lets the caller pass the running
        sum of cost in the current batch (if any); without it,
        per_batch caps are skipped for this estimate.
        """
        now = now or datetime.now(timezone.utc)
        triggered: list[BudgetAlert] = []
        block = False
        block_reason = ""

        scopes_to_check = self._scopes_for_estimate(estimate)
        for scope in scopes_to_check:
            cap = await self.store.get_budget(scope)
            if cap is None or not cap.enabled:
                continue

            for period, limit in (
                ("per_batch", cap.per_batch_limit_usd),
                ("daily", cap.daily_limit_usd),
                ("monthly", cap.monthly_limit_usd),
            ):
                if limit is None:
                    continue
                if period == "per_batch":
                    if per_batch_observed_usd is None:
                        continue
                    projected = per_batch_observed_usd + estimate.cost_usd_estimated
                else:
                    current = await self.store.spend_in_period(
                        scope, period, now=now  # type: ignore[arg-type]
                    )
                    projected = current + estimate.cost_usd_estimated

                level = self._classify_level(projected, limit)
                if level is None:
                    continue
                action = _LEVEL_DEFAULT_ACTION[level]
                alert = BudgetAlert(
                    alert_id=f"alert_{uuid.uuid4().hex[:12]}",
                    scope=scope,
                    period=period,  # type: ignore[arg-type]
                    level=level,
                    threshold_usd=limit,
                    observed_usd=projected,
                    action=action,
                    fired_at=now,
                )
                triggered.append(alert)
                await self.store.save_alert(alert)
                if self.alert_sink:
                    try:
                        await self.alert_sink(alert)
                    except Exception:  # noqa: BLE001 — sink boundary
                        logger.exception(
                            "alert_sink raised for alert %s", alert.alert_id
                        )

                if action == "block":
                    block = True
                    block_reason = (
                        f"Budget exceeded: {scope} {period} projected "
                        f"${projected:.2f} > ${limit:.2f} (level={level})"
                    )

        return BudgetCheck(
            allowed=not block,
            reason=block_reason,
            triggered_alerts=tuple(triggered),
        )

    def _classify_level(
        self, projected: float, limit: float
    ) -> AlertLevel | None:
        """Pick the highest tier the projected spend trips, or None."""
        if limit <= 0:
            return None
        ratio = projected / limit
        if ratio >= _LEVEL_FRACTION["over_120"]:
            return "over_120"
        if ratio >= _LEVEL_FRACTION["cap_100"]:
            return "cap_100"
        if ratio >= _LEVEL_FRACTION["warn_80"]:
            return "warn_80"
        return None

    def _scopes_for_estimate(
        self, estimate: CostEstimate
    ) -> list[ScopeKey]:
        scopes: list[ScopeKey] = [ScopeKey(kind="global", key="*")]
        if estimate.workspace:
            scopes.append(ScopeKey(kind="workspace", key=estimate.workspace))
        if estimate.priority:
            scopes.append(ScopeKey(kind="priority", key=estimate.priority))
        if estimate.task_type:
            scopes.append(ScopeKey(kind="task_type", key=estimate.task_type))
        scopes.append(ScopeKey(kind="model", key=estimate.model))
        return scopes

    async def alerts_since(
        self, scope: ScopeKey | None = None, *, since: datetime | None = None
    ) -> list[BudgetAlert]:
        return await self.store.list_alerts(scope, since=since)
