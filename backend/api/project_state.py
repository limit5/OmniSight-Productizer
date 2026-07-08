"""OP-904 (F6) — ``/api/v1/project-state`` cross-task awareness aggregator.

Reads JIRA / Cognee / Graphiti / failure-graph in parallel, assembles
the three-axis payload (structural / temporal / causal), and serves it
behind the standard backend auth chain so the runner prompt-builder can
hydrate fresh-pickup context in a single round-trip.

Contract reminders (per the AC):

* hard 2 s total budget, per-axis budgets of 800/600/600 ms
* per-axis timeout → ``null`` for that axis; the response is still 200
* 5-minute TTL cache keyed on ``(ticket, develop_sha)`` with invalidation
  on JIRA webhook (see :func:`invalidate_for_webhook`)
* every call logs axis-by-axis latency + cache hit/miss to the operator
  metrics surface (``/api/v1/project-state/metrics``)

The router is mounted from ``backend.main`` via
``_include_versioned_router`` so the canonical URL is
``/api/v1/project-state``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend import auth
from backend import api_versioning as versioning
from backend import metrics
from backend.agents import project_state_aggregator as agg
from backend.agents import project_state_cache as cache_mod

log = logging.getLogger(__name__)


router = APIRouter(prefix="/project-state", tags=["project-state"])


# OP-904 AC #1 — the JIRA project we serve. Mirrors the conventions
# enforced by docs/sop/jira-ticket-conventions.md §2 (ticket keys look
# like ``OP-<N>`` with an optional alphanumeric suffix).
_TICKET_RE = re.compile(r"^OP-[A-Z0-9]+$", re.IGNORECASE)
_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


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


def _clean_git_sha(raw: object) -> str | None:
    candidate = str(raw or "").strip()
    if _FULL_GIT_SHA_RE.fullmatch(candidate):
        return candidate.lower()
    return None


def _resolve_develop_sha() -> str:
    """Return the deployed build SHA, dev checkout SHA, or ``unknown``.

    The cache is keyed on this string so a fresh merge to ``develop``
    invalidates every cached entry naturally — the next call computes
    against the new SHA and writes a new slot. Deployed containers read
    the deploy overlay first, then the build env var. Only dev checkouts
    fall back to forking ``git``; CI/deploy paths without any SHA source
    return ``unknown`` so the cache still works.
    """
    overlay = versioning.get_deploy_overlay() or {}
    overlay_sha = _clean_git_sha(overlay.get("build_git_sha"))
    if overlay_sha:
        return overlay_sha

    env_sha = _clean_git_sha(os.environ.get("OMNISIGHT_BUILD_GIT_SHA"))
    if env_sha:
        return env_sha

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return _clean_git_sha(proc.stdout) or "unknown"


def _emit_axis_metrics(payload: dict[str, Any]) -> None:
    """OP-2557 — axis-health counters, emitted before every served 200.

    Sits at the router boundary (NOT in ``record_trace``, which carries
    no axis payload on cache hits) so cache-hit responses count too.
    Content classification reuses the aggregator's classifier so the
    counter enum can never drift from the trace surface. Best-effort:
    never raises, O(1), no I/O.
    """
    try:
        for axis in agg.ALL_AXES:
            content = agg._classify_payload(payload.get(axis))
            metrics.project_state_axis_total.labels(axis=axis, content=content).inc()

        structural = payload.get("structural")
        if isinstance(structural, dict):
            jira_useful = bool(
                structural.get("blockers") or structural.get("parent_meta")
            )
            kg_useful = bool(structural.get("kg_neighbours"))
            metrics.project_state_structural_half_total.labels(
                half="jira", useful="true" if jira_useful else "false"
            ).inc()
            metrics.project_state_structural_half_total.labels(
                half="kg", useful="true" if kg_useful else "false"
            ).inc()
        # structural is None (e.g. all-axes-failed): skip the half
        # counters — axis_total above already recorded the degraded axis.
    except Exception:  # noqa: BLE001 — metrics must never break serving
        log.debug("project_state.metrics_emit_failed", exc_info=True)


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
        _emit_axis_metrics(cached)
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
        fallback = agg.assemble_response(
            ticket_key=ticket_key,
            develop_sha=develop_sha,
            results={
                axis: agg.AxisResult(
                    name=axis, payload=None, latency_sec=0.0, error=exc.errors.get(axis)
                )
                for axis in agg.ALL_AXES
            },
        )
        _emit_axis_metrics(fallback)
        return fallback

    if _structural_cacheable(payload):
        cache_mod.default_cache.set(cache_key, payload)
        log.info(
            "project_state.cache_write ticket=%s develop_sha=%s total_latency_sec=%.3f",
            ticket_key,
            develop_sha[:12],
            time.monotonic() - started,
        )
    else:
        structural = payload.get("structural") or {}
        log.info(
            "project_state.cache_skip_degraded_structural ticket=%s "
            "develop_sha=%s jira_source=%s kg_source=%s",
            ticket_key,
            develop_sha[:12],
            structural.get("jira_source") if isinstance(structural, dict) else None,
            structural.get("kg_source") if isinstance(structural, dict) else None,
        )
    _emit_axis_metrics(payload)
    return payload


def _structural_cacheable(payload: dict[str, Any]) -> bool:
    """OP-2555 (audit F4) — never latch a degraded structural into the TTL cache.

    A cached degraded/null structural pins ``kg_source=degraded`` (or a
    NULL axis) for the full 5-minute TTL even though the background KG
    refresh typically completes within seconds — the next uncached call
    would already serve ``live``. Stable markers
    (``live/disabled/unavailable``) stay cacheable; only the transient
    ``degraded`` marker and a missing axis skip the write.
    """
    structural = payload.get("structural")
    if not isinstance(structural, dict):
        return False
    return "degraded" not in (
        structural.get("jira_source"),
        structural.get("kg_source"),
    )


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
                "axis_content": dict(t.axis_content),
                "source_markers": dict(t.source_markers),
                "jira_negative_cache_hits": t.jira_negative_cache_hits,
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


__all__ = [
    "get_project_state",
    "invalidate_for_webhook",
    "project_state_metrics",
    "router",
]
