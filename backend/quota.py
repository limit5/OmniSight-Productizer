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


# Phase-3 Epic 8 / SP-8.1 (task #81, 2026-04-21): free tier's per-IP
# budget was 60/60s — too tight for a single-user dashboard cold load
# (polling + SSE + asset fetch comfortably burst past 60 req/min) and
# operators reported 429 cascades on the first dashboard visit after
# login. Raised to 300/60s. The other tiers scale by the same 5x
# factor so the plan hierarchy stays intact
# (test_quota.test_plan_hierarchy).
PLAN_QUOTAS: dict[str, PlanQuota] = {
    "free": PlanQuota(
        per_ip=RateLimitBudget(capacity=300, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=120, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=300, window_seconds=60.0),
    ),
    "starter": PlanQuota(
        per_ip=RateLimitBudget(capacity=600, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=300, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=1000, window_seconds=60.0),
    ),
    "pro": PlanQuota(
        per_ip=RateLimitBudget(capacity=1500, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=600, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=3000, window_seconds=60.0),
    ),
    "enterprise": PlanQuota(
        per_ip=RateLimitBudget(capacity=3000, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=1200, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=10000, window_seconds=60.0),
    ),
}

DEFAULT_PLAN = "free"


def quota_for_plan(plan: str) -> PlanQuota:
    return PLAN_QUOTAS.get(plan, PLAN_QUOTAS[DEFAULT_PLAN])
