"""I9 — Tenant plan quota configuration.

Maps tenant plan tiers to rate-limit budgets across three dimensions:
  per-IP, per-user, per-tenant.

Each dimension specifies (capacity, window_seconds).  The middleware
consumes one token per request from each applicable bucket.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitBudget:
    capacity: int
    window_seconds: float


@dataclass(frozen=True)
class PlanQuota:
    per_ip: RateLimitBudget
    per_user: RateLimitBudget
    per_tenant: RateLimitBudget


PLAN_QUOTAS: dict[str, PlanQuota] = {
    "free": PlanQuota(
        per_ip=RateLimitBudget(capacity=60, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=120, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=300, window_seconds=60.0),
    ),
    "starter": PlanQuota(
        per_ip=RateLimitBudget(capacity=120, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=300, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=1000, window_seconds=60.0),
    ),
    "pro": PlanQuota(
        per_ip=RateLimitBudget(capacity=300, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=600, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=3000, window_seconds=60.0),
    ),
    "enterprise": PlanQuota(
        per_ip=RateLimitBudget(capacity=600, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=1200, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=10000, window_seconds=60.0),
    ),
}

DEFAULT_PLAN = "free"


def quota_for_plan(plan: str) -> PlanQuota:
    return PLAN_QUOTAS.get(plan, PLAN_QUOTAS[DEFAULT_PLAN])
