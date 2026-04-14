"""Phase 52 — Observability endpoints.

GET  /metrics    Prometheus exposition format
GET  /healthz    Deep health check (DB ping + version + watchdog age)

Both endpoints are intentionally outside the authenticated surface so
Prometheus / load-balancer health probes don't need credentials.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import Response, JSONResponse

from backend import metrics as _metrics

logger = logging.getLogger(__name__)
router = APIRouter(tags=["observability"])

_HEALTHZ_TIMEOUT_S = 1.0
_VERSION = "0.1.0"  # mirrors backend/main.py FastAPI version


@router.get("/metrics")
async def get_metrics() -> Response:
    body, ctype = _metrics.render_exposition()
    return Response(content=body, media_type=ctype)


async def _probe_db() -> dict:
    """Try a 1-statement query against the active DB."""
    started = time.perf_counter()
    try:
        from backend import db
        async with db._conn().execute("SELECT 1") as cur:
            row = await cur.fetchone()
        ok = bool(row and (row[0] if not hasattr(row, "keys") else row[0]) == 1)
    except Exception as exc:
        return {"ok": False, "latency_ms": int((time.perf_counter() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc!s}"[:200]}
    return {"ok": ok, "latency_ms": int((time.perf_counter() - started) * 1000)}


def _watchdog_age_s() -> float | None:
    """Best-effort age (seconds) since the watchdog last did a pass.
    Returns None if the watchdog module hasn't started yet."""
    try:
        from backend.routers import invoke as _inv
        last = getattr(_inv, "_watchdog_last_tick", None)
        if not last:
            return None
        return time.time() - float(last)
    except Exception as exc:
        # Fix-B B5: not fatal, but shouldn't be completely silent.
        logger.debug("_watchdog_age_s lookup failed: %s", exc)
        return None


@router.get("/healthz")
async def healthz() -> JSONResponse:
    """Deep health check.

    Returns:
      ok           overall — every component must report ok
      version      backend semver
      uptime_s     seconds since process start
      auth_mode    open|session|strict
      profile      currently active decision profile
      db           {ok, latency_ms, error?}
      watchdog     {age_s | null}
      sse          {subscribers, dropped}
    """
    db_probe_task = asyncio.create_task(_probe_db())
    try:
        db_probe = await asyncio.wait_for(db_probe_task, timeout=_HEALTHZ_TIMEOUT_S)
    except asyncio.TimeoutError:
        db_probe = {"ok": False, "latency_ms": int(_HEALTHZ_TIMEOUT_S * 1000),
                    "error": "timeout"}

    # SSE telemetry (non-blocking)
    sse_info: dict[str, Any] = {"subscribers": 0, "dropped": 0}
    try:
        from backend.events import bus
        sse_info = {"subscribers": bus.subscriber_count,
                    "dropped": bus.subscriber_dropped}
    except Exception:
        pass

    # Profile + auth
    profile_id = "STRICT"
    try:
        from backend import decision_profiles as _dp
        profile_id = _dp.get_current_id()
    except Exception:
        pass
    try:
        from backend import auth as _au
        auth_mode = _au.auth_mode()
    except Exception:
        auth_mode = "open"

    # Process uptime via prometheus gauge
    uptime_s: float = 0.0
    if _metrics.is_available():
        try:
            # We set process_start_time in metrics import; just diff.
            from backend import metrics as _m
            sample = next(
                iter(_m.process_start_time.collect()[0].samples), None,
            )
            if sample:
                uptime_s = max(0.0, time.time() - float(sample.value))
        except Exception:
            pass

    # Phase 64-D D4: surface sandbox counters so /healthz alone tells
    # the operator whether containers are launching, getting killed by
    # the lifetime cap, or having their output truncated.
    sandbox_info: dict[str, Any] = {
        "launched": 0, "errors": 0,
        "lifetime_killed": 0, "image_rejected": 0,
        "output_truncated": 0,
    }
    if _metrics.is_available():
        try:
            from backend import metrics as _m
            def _sum(metric, **filt):
                total = 0.0
                for s in metric.collect()[0].samples:
                    if not s.name.endswith("_total"):
                        continue
                    if all(s.labels.get(k) == v for k, v in filt.items()):
                        total += s.value
                return int(total)
            sandbox_info["launched"] = _sum(_m.sandbox_launch_total, result="success")
            sandbox_info["errors"] = _sum(_m.sandbox_launch_total, result="error")
            sandbox_info["image_rejected"] = _sum(_m.sandbox_launch_total, result="image_rejected")
            sandbox_info["lifetime_killed"] = _sum(_m.sandbox_lifetime_killed_total)
            sandbox_info["output_truncated"] = _sum(_m.sandbox_output_truncated_total)
        except Exception as exc:
            logger.debug("sandbox counters lookup failed: %s", exc)

    overall_ok = bool(db_probe.get("ok"))
    body = {
        "ok": overall_ok,
        "version": _VERSION,
        "uptime_s": round(uptime_s, 1),
        "auth_mode": auth_mode,
        "profile": profile_id,
        "db": db_probe,
        "watchdog": {"age_s": _watchdog_age_s()},
        "sse": sse_info,
        "sandbox": sandbox_info,
        "checked_at": time.time(),
    }
    return JSONResponse(content=body, status_code=200 if overall_ok else 503)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L1-04: compact ops summary for the in-app dashboard panel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# /healthz is great for probes; it's heavy to poll. This endpoint
# returns the handful of numbers an operator actually glances at
# (spend, freeze state, pending decisions, watchdog, subscribers)
# so the frontend's OpsSummary panel can poll every 10 s cheaply.

@router.get("/ops/summary")
async def ops_summary() -> dict:
    from backend.routers import system as _sys
    from backend import decision_engine as _de

    try:
        pending = len(_de.list_pending())
    except Exception as exc:
        logger.debug("ops_summary: decision engine lookup failed: %s", exc)
        pending = 0

    try:
        sse_subs = 0
        from backend.events import bus as _bus
        sse_subs = len(_bus._subscribers)
    except Exception as exc:
        logger.debug("ops_summary: sse bus lookup failed: %s", exc)

    # Uptime derives from the Prometheus process_start_time gauge
    # that metrics.py sets at import time — avoids needing a second
    # anchor variable and stays accurate across reloads.
    uptime = None
    try:
        from backend import metrics as _m
        if hasattr(_m.process_start_time, "_value"):
            started = float(_m.process_start_time._value.get())
            if started > 0:
                uptime = round(time.time() - started, 1)
    except Exception:
        pass

    return {
        "checked_at": time.time(),
        "uptime_s": uptime,
        # Spend
        "daily_cost_usd": _sys.get_daily_cost(),
        "hourly_cost_usd": _sys.get_hourly_cost()
            if hasattr(_sys, "get_hourly_cost") else 0.0,
        "token_frozen": bool(getattr(_sys, "token_frozen", False)),
        "budget_level": getattr(_sys, "_last_budget_level", "") or "normal",
        # DE load
        "decisions_pending": pending,
        # Event bus pressure
        "sse_subscribers": sse_subs,
        # Watchdog liveness
        "watchdog_age_s": _watchdog_age_s(),
    }
