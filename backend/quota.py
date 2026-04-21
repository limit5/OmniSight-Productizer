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
# — raised to 300/60s with the other tiers scaled 5x to preserve the
# plan hierarchy.
#
# SP-8.1b (2026-04-21, same day): operator-side follow-up. CF Free's
# Rate Limiting Rules are 10 req/10s only — no other thresholds
# available on that tier. Enabling CF's rate limit at that level
# would trip legitimate dashboard traffic before our backend ever
# sees it, so CF rate limiting stays disabled and the backend cap is
# the only per-IP gate. To keep legitimate burst (polling + SSE +
# asset prefetch + 2-3 tabs) comfortable while still providing a
# sane ceiling against single-IP abuse, every tier's per_ip is
# bumped another 4x:
#
#   free:       300 → 1200 (20/sec avg)
#   starter:    600 → 2400
#   pro:       1500 → 6000
#   enterprise: 3000 → 12000
#
# The pool ``max_size=20`` per worker × 2 workers × 2 replicas = 80
# concurrent DB connections remains the physical ceiling under
# sustained abuse — rate_limit is the soft cap, pool saturation is
# the hard cap (which produces 503 via pool-timeout before real DB
# damage). See docs/phase-3-runtime-v2/02-sub-phases.md for the
# full rationale and the "CF free rate limiting off" decision.
PLAN_QUOTAS: dict[str, PlanQuota] = {
    "free": PlanQuota(
        per_ip=RateLimitBudget(capacity=1200, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=120, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=300, window_seconds=60.0),
    ),
    "starter": PlanQuota(
        per_ip=RateLimitBudget(capacity=2400, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=300, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=1000, window_seconds=60.0),
    ),
    "pro": PlanQuota(
        per_ip=RateLimitBudget(capacity=6000, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=600, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=3000, window_seconds=60.0),
    ),
    "enterprise": PlanQuota(
        per_ip=RateLimitBudget(capacity=12000, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=1200, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=10000, window_seconds=60.0),
    ),
}

DEFAULT_PLAN = "free"


def quota_for_plan(plan: str) -> PlanQuota:
    return PLAN_QUOTAS.get(plan, PLAN_QUOTAS[DEFAULT_PLAN])
