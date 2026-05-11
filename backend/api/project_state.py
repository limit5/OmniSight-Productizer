"""OP-904 (F6) — ``/api/v1/project-state`` cross-task awareness aggregator.

Reads JIRA / Cognee / Graphiti / failure-graph in parallel, assembles
the three-axis payload (structural / temporal / causal), and serves it
behind the standard backend auth chain so the runner prompt-builder can
hydrate fresh-pickup context in a single round-trip.

Contract reminders (per the AC):

* hard 2 s total budget, per-axis budgets of 800/600/600 ms
* per-axis timeout → ``null`` for that axis; the response is still 200
* 5-minute TTL cache keyed on ``(ticket, develop_sha)`` with invalidation
  on JIRA webhook + develop merge (see :func:`invalidate_for_webhook`)
* every call logs axis-by-axis latency + cache hit/miss to the operator
  metrics surface (``/api/v1/project-state/metrics``)

The router is mounted from ``backend.main`` via
``_include_versioned_router`` so the canonical URL is
``/api/v1/project-state``.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend import auth
from backend.agents import project_state_aggregator as agg
from backend.agents import project_state_cache as cache_mod

log = logging.getLogger(__name__)


router = APIRouter(prefix="/project-state", tags=["project-state"])


# OP-904 AC #1 — the JIRA project we serve. Mirrors the conventions
# enforced by docs/sop/jira-ticket-conventions.md §2 (ticket keys look
# like ``OP-<N>`` with an optional alphanumeric suffix).
_TICKET_RE = re.compile(r"^OP-[A-Z0-9]+$", re.IGNORECASE)


def _normalise_ticket(raw: str | None) -> str:
    """Reject anything that doesn't look like an OP project key.

    Returning a 400 here is cheaper than letting the aggregator fan out
    three coroutines that all immediately discover the key is bogus.
    """
    candidate = (raw or "").strip().upper()
    if not candidate:
        raise HTTPException(status_code=400, detail="ticket is required")
    if not _TICKET_RE.match(candidate):
        raise HTTPException(
            status_code=400,
            detail="ticket must look like OP-<N> (uppercase, alphanumeric suffix)",
        )
    return candidate


def _resolve_develop_sha() -> str:
    """Return the current ``origin/develop`` SHA, or ``unknown``.

    The cache is keyed on this string so a fresh merge to ``develop``
    invalidates every cached entry naturally — the next call computes
    against the new SHA and writes a new slot. When the repo is not
    available (CI containers without a git tree, tests) we fall back
    to ``unknown`` so the cache still works but with no SHA-based
    invalidation.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "origin/develop"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return proc.stdout.strip() or "unknown"


@router.get("", response_model=None)
async def get_project_state(
    ticket: str = Query(..., description="JIRA ticket key (e.g. OP-904)"),
    _user: auth.User = Depends(auth.current_user),
) -> dict[str, Any]:
    """Return the three-axis ``project-state`` payload for ``ticket``.

    See module docstring for the budget / degrade / cache contract.
    """

    ticket_key = _normalise_ticket(ticket)
    develop_sha = _resolve_develop_sha()
    cache_key = (ticket_key, develop_sha)

    cached = cache_mod.default_cache.get(cache_key)
    if cached is not None:
        await agg.record_trace(
            agg.ProjectStateTrace(
                ticket=ticket_key,
                develop_sha=develop_sha,
                cache_hit=True,
                total_latency_sec=0.0,
                axis_latency_sec={},
                axis_error={},
                budget_exceeded=False,
            )
        )
        log.info(
            "project_state.cache_hit ticket=%s develop_sha=%s",
            ticket_key,
            develop_sha[:12],
        )
        return cached

    started = time.monotonic()
    try:
        payload = await agg.aggregate_project_state(
            ticket_key,
            develop_sha=develop_sha,
            cache_hit=False,
        )
    except agg.ProjectStateAllAxesFailed as exc:
        log.warning(
            "project_state.all_axes_failed ticket=%s errors=%s",
            ticket_key,
            exc.errors,
        )
        # Per AC error catalog: return 200 with all-null axes so the
        # runner prompt-builder degrades silently rather than blocking
        # pickup on a backing-store outage.
        return agg.assemble_response(
            ticket_key=ticket_key,
            develop_sha=develop_sha,
            results={
                axis: agg.AxisResult(
                    name=axis, payload=None, latency_sec=0.0, error=exc.errors.get(axis)
                )
                for axis in agg.ALL_AXES
            },
        )

    cache_mod.default_cache.set(cache_key, payload)
    log.info(
        "project_state.cache_write ticket=%s develop_sha=%s total_latency_sec=%.3f",
        ticket_key,
        develop_sha[:12],
        time.monotonic() - started,
    )
    return payload


@router.get("/metrics", response_model=None)
async def project_state_metrics(
    limit: int = Query(50, ge=1, le=200),
    _user: auth.User = Depends(auth.require_admin),
) -> dict[str, Any]:
    """Operator-facing metrics surface (AC #6).

    Returns the last ``limit`` aggregator traces (axis-by-axis latency,
    cache hit/miss, budget overrun flag) plus current cache stats. Used
    by the operator dashboard to verify the 2 s budget contract under
    real traffic — a sustained tail of ``budget_exceeded=true`` is a
    paging signal.
    """
    traces = await agg.tail_traces(limit=limit)
    return {
        "cache": cache_mod.default_cache.stats(),
        "traces": [
            {
                "ticket": t.ticket,
                "develop_sha": t.develop_sha,
                "cache_hit": t.cache_hit,
                "total_latency_sec": round(t.total_latency_sec, 4),
                "axis_latency_sec": {
                    k: round(v, 4) for k, v in t.axis_latency_sec.items()
                },
                "axis_error": dict(t.axis_error),
                "budget_exceeded": t.budget_exceeded,
                "captured_at": t.captured_at.isoformat(),
            }
            for t in traces
        ],
    }


def invalidate_for_webhook(ticket_key: str) -> int:
    """Drop every cache entry for ``ticket_key`` regardless of SHA.

    Called from the JIRA webhook handler when a ticket transitions or
    its description changes — the cached structural axis would
    otherwise serve stale parent/blockers/siblings until the 5-minute
    TTL expired.
    """
    return cache_mod.default_cache.invalidate_ticket(ticket_key.strip().upper())


def invalidate_for_develop_merge() -> None:
    """Drop the entire cache on a develop-merge event (AC #4).

    The cache key embeds the develop SHA, so the next request after a
    merge would have already missed; the explicit clear shaves the
    cold-tail of in-flight callers still holding the old SHA so they
    get the new view on their next call.
    """
    cache_mod.default_cache.clear()


__all__ = [
    "get_project_state",
    "invalidate_for_develop_merge",
    "invalidate_for_webhook",
    "project_state_metrics",
    "router",
]
