"""W10 #284 — FastAPI router for Web Observability ingest + dashboard.

Endpoints (mounted under ``settings.api_prefix``):

  POST /rum/vitals           Browser sendBeacon — Core Web Vital sample.
  POST /rum/errors           Browser uncaught error — feeds the
                             ErrorToIntentRouter for JIRA ticket creation.
  GET  /rum/dashboard        Per-metric × per-page CWV snapshot for the
                             operator dashboard. ``?page=`` to filter to
                             one page; ``?metric=`` to filter to one
                             vital. ``?reset=true`` to wipe the in-memory
                             aggregator (operator panic-button).
  GET  /rum/errors/recent    Recent fingerprints + JIRA tickets
                             routed by the error router.
  GET  /rum/health           Quick health probe — exposes router metrics
                             (routed / deduped / adapter-unavailable counts).

The vitals & errors endpoints are intentionally **unauthenticated** —
browser ``navigator.sendBeacon`` cannot attach an auth header on
unload, and CSRF doesn't apply (read-only sink that aggregates
publicly-observable performance numbers). Hard payload size cap
mitigates DoS.

Operators that want the dashboard surface gated must wire the route
through the existing FastAPI dependency stack (see auth.py); this
module ships the raw read endpoints and lets ops wire ACLs in main.py.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from backend.observability import (
    ErrorEvent,
    WebVital,
    get_default_aggregator,
    get_default_router,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rum", tags=["web-observability"])


# ── Hard limits ──────────────────────────────────────────────────

MAX_BEACON_BYTES = 16 * 1024  # 16 KiB — generous for one CWV blob
MAX_ERROR_BYTES = 64 * 1024   # 64 KiB — stack trace headroom


async def _read_body(request: Request, *, limit: int) -> bytes:
    body = await request.body()
    if len(body) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"payload exceeds {limit} bytes (got {len(body)})",
        )
    return body


def _parse_json(body: bytes) -> dict[str, Any]:
    import json
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail=f"body is not JSON: {exc}")
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400,
                            detail="body must be a JSON object")
    return parsed


# ── /rum/vitals ──────────────────────────────────────────────────


@router.post("/vitals")
async def ingest_vital(request: Request) -> dict:
    """Ingest one Core Web Vital sample.

    Expected payload (web-vitals JS lib output passed through sendBeacon):

        {
          "name": "LCP",          // required — LCP / INP / CLS / TTFB / FCP
          "value": 2400.5,        // required — ms (or unitless for CLS)
          "page": "/blog",        // optional — defaults to "/"
          "rating": "good",       // optional — re-derived if absent
          "navType": "navigate",  // optional
          "sessionId": "...",     // optional
          "userAgent": "...",     // optional (navigator.userAgent)
          "locale": "en-US"       // optional (navigator.language)
        }

    Returns ``{"accepted": True, "rating": "..."}`` so test harnesses
    and curl users can verify the classification.
    """
    body = await _read_body(request, limit=MAX_BEACON_BYTES)
    data = _parse_json(body)

    name = str(data.get("name") or "").upper()
    if not name:
        raise HTTPException(400, detail="'name' is required")
    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        raise HTTPException(400, detail="'value' must be a number")

    user_agent = data.get("userAgent") or request.headers.get("user-agent", "")
    vital = WebVital(
        name=name,
        value=value,
        page=str(data.get("page") or "/"),
        session_id=str(data.get("sessionId") or ""),
        rating=str(data.get("rating") or ""),
        nav_type=str(data.get("navType") or "navigate"),
        user_agent=str(user_agent or ""),
        locale=str(data.get("locale") or ""),
        raw=data,
    )

    try:
        get_default_aggregator().record(vital)
    except Exception as exc:
        # Aggregator failures should never bubble up — beacon endpoints
        # must always 200 so the browser doesn't retry.
        logger.warning("rum.vitals aggregator error: %r", exc)

    return {
        "accepted": True,
        "name": vital.name,
        "value": vital.value,
        "rating": vital.rating,
    }


# ── /rum/errors ──────────────────────────────────────────────────


@router.post("/errors")
async def ingest_error(request: Request) -> dict:
    """Ingest one browser error event.

    Expected payload:

        {
          "message": "TypeError: x is undefined",  // required
          "stack": "at app.js:1:2\\n...",
          "page": "/blog",
          "level": "error",          // error / warning / fatal
          "release": "1.42.0",
          "environment": "production",
          "fingerprint": "...",      // optional — derived if absent
          "sessionId": "...",
          "userAgent": "..."
        }

    Routes the event through the ``ErrorToIntentRouter`` (JIRA / GitHub
    Issues / GitLab Issues) and returns the resulting ticket ref (if any).
    """
    body = await _read_body(request, limit=MAX_ERROR_BYTES)
    data = _parse_json(body)

    message = str(data.get("message") or "").strip()
    if not message:
        raise HTTPException(400, detail="'message' is required")

    user_agent = data.get("userAgent") or request.headers.get("user-agent", "")
    event = ErrorEvent(
        message=message,
        page=str(data.get("page") or "/"),
        session_id=str(data.get("sessionId") or ""),
        level=str(data.get("level") or "error"),
        stack=str(data.get("stack") or ""),
        fingerprint=str(data.get("fingerprint") or ""),
        release=str(data.get("release") or ""),
        environment=str(data.get("environment") or "production"),
        user_agent=str(user_agent or ""),
        raw=data,
    )

    try:
        ref = await get_default_router().route(event)
    except Exception as exc:
        # Router exceptions must NOT break ingest — log + 202.
        logger.warning("rum.errors router error: %r", exc)
        return {"accepted": True, "routed": False, "error": str(exc)}

    return {
        "accepted": True,
        "routed": ref is not None,
        "ticket": ref.ticket if ref else "",
        "ticket_url": ref.url if ref else "",
        "fingerprint": event.fingerprint,
    }


# ── /rum/dashboard ───────────────────────────────────────────────


@router.get("/dashboard")
async def dashboard(
    page: Optional[str] = Query(default=None,
                                description="Filter to one page bucket "
                                            "(use '*' for site-wide rollup)."),
    metric: Optional[str] = Query(default=None,
                                  description="Filter to one CWV name "
                                              "(LCP / INP / CLS / TTFB / FCP)."),
    reset: bool = Query(default=False,
                        description="Wipe in-memory aggregator (operator only)."),
) -> dict:
    agg = get_default_aggregator()
    if reset:
        agg.reset()
        return {"reset": True, "metrics": [], "total_samples": 0,
                "window_seconds": agg.window_seconds, "generated_at": 0}
    snap = agg.snapshot(page=page, metric=metric)
    return snap.to_dict()


# ── /rum/errors/recent ───────────────────────────────────────────


@router.get("/errors/recent")
async def errors_recent(
    limit: int = Query(default=50, ge=1, le=500,
                       description="Max number of fingerprints."),
) -> dict:
    router_inst = get_default_router()
    return {
        "items": router_inst.list_recent(limit=limit),
        "metrics": router_inst.metrics(),
    }


# ── /rum/health ──────────────────────────────────────────────────


@router.get("/health")
async def rum_health() -> dict:
    agg = get_default_aggregator()
    rt = get_default_router()
    snap = agg.snapshot()
    return {
        "ok": True,
        "vitals": {
            "total_samples": snap.total_samples,
            "active_buckets": len(snap.metrics),
            "window_seconds": snap.window_seconds,
        },
        "errors": rt.metrics(),
    }
