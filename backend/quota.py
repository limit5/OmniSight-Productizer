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
# SP-8.1c (2026-04-21): per_user + per_tenant were calibrated for a
# much quieter dashboard than what actually ships. The real shape:
#   * ``hooks/use-engine.ts`` fetches 11 endpoints via
#     ``Promise.allSettled`` every 5s → 132 req/min per tab from this
#     hook alone.
#   * 4+ sidebar panels (ops_summary / orchestration / pipeline_timeline
#     / audit / run_history / arch_indicator / host_devices)
#     independently poll 5-15s → another ~30-60 req/min per tab.
#   * A single logged-in operator cold-loading one tab generates
#     150-200 req/min legitimately.
# Old ``per_user=120/min`` tripped before the first ``useEngine``
# round finished, producing 429 cascades on every panel whose most
# recent request lost the rate-limit race. Operator debug screenshot
# 2026-04-21 confirmed the 3 rightmost panels (OPS SUMMARY /
# ORCHESTRATION / PIPELINE TIMELINE) all showed "API 429: ..." after
# the SP-8.1b per-IP bump — per-IP was no longer the bottleneck;
# per-user was. Scale per_user + per_tenant to match the per_ip
# envelope so a single multi-tab operator never trips this.
#
# The long-term fix is dashboard-side: consolidate the 11
# ``useEngine`` endpoints into one ``/dashboard/summary`` aggregator
# and switch to SSE push for state that changes less than once per
# poll window. Tracked as follow-up, not this commit's scope.
#
# Phase-4 SP-4-5 (2026-04-24): the dashboard-side fix landed. 4-1
# added ``GET /api/v1/dashboard/summary`` (one request replaces the
# 11-endpoint ``useEngine`` fan-out); 4-2 rewrote ``useEngine`` to
# consume the aggregator; 4-3 widened the poll interval from 5s to
# 10s; 4-4 inventoried the remaining panel-local polls (absorb /
# SSE-first / keep-independent per
# ``docs/dashboard-polling-inventory.md``). The realistic free-tier
# per-tab budget is now ~6 aggregator calls/min + safety-net polls
# ≈ 10-15 req/min/tab. With 3 tabs open a single user uses ~45
# req/min — 300/60s leaves 6x headroom while restoring ``per_user``
# as a real defensive cap against compromised-credential abuse or
# runaway client bugs. ``per_tenant`` drops in lockstep to 600/60s
# so small teams still fit (3-4 users × 45 = ~180 req/min leaves
# ~3x headroom) while the tier ceiling reins in tenant-wide abuse.
# ``per_ip`` stays at 1200 because the CF-Free-Rate-Limiting
# rationale behind SP-8.1b is unchanged — edge has no useful gate
# below 1200/min, so the backend remains the sole per-IP cap and
# keeps the bursty headroom for legitimate shared-NAT traffic.
PLAN_QUOTAS: dict[str, PlanQuota] = {
    "free": PlanQuota(
        per_ip=RateLimitBudget(capacity=1200, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=300, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=600, window_seconds=60.0),
    ),
    "starter": PlanQuota(
        per_ip=RateLimitBudget(capacity=2400, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=2400, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=3000, window_seconds=60.0),
    ),
    "pro": PlanQuota(
        per_ip=RateLimitBudget(capacity=6000, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=6000, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=10000, window_seconds=60.0),
    ),
    "enterprise": PlanQuota(
        per_ip=RateLimitBudget(capacity=12000, window_seconds=60.0),
        per_user=RateLimitBudget(capacity=12000, window_seconds=60.0),
        per_tenant=RateLimitBudget(capacity=30000, window_seconds=60.0),
    ),
}

DEFAULT_PLAN = "free"


def quota_for_plan(plan: str) -> PlanQuota:
    return PLAN_QUOTAS.get(plan, PLAN_QUOTAS[DEFAULT_PLAN])
