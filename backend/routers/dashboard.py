"""Dashboard aggregator endpoint (Phase 4-1).

Collapses the 11 ``/runtime/*`` fan-out calls that the frontend
``useEngine`` hook makes every 5 seconds into a single composite
response. Sub-queries run concurrently via ``asyncio.gather`` and are
individually wrapped in an ``{ok, data|error}`` envelope so a single
failure does not take down the rest of the payload (matches the
frontend's existing ``Promise.allSettled`` fault-tolerance, pushed to
the server side).

Rationale: TODO.md Phase 4 preamble. Reduces 11 req/poll × 5 s tick =
132 req/min per tab down to 1 req/poll, freeing the per-user rate-limit
budget for the defensive cap it was designed to be.

Module-global state audit (SOP Step 1): this router owns no module-
global state; every call is a pure fan-out to other routers that own
their own state (``SharedTokenUsage`` via Redis, ``_log_buffer_shared``
across workers, PG via pool). Per-worker consistency concerns are
inherited from the underlying endpoints — this aggregator does not
introduce a new coordination point.

PG-connection concurrency note: asyncpg forbids pipelining two
operations on the same connection. The two sub-queries that need PG
(``unread_count`` + ``list_simulations``) therefore each acquire their
own pool conn inside ``_run_with_conn`` rather than sharing an
aggregator-scoped conn from ``Depends(get_conn)``. This keeps the
aggregator response time the max of the slowest sub-query (parallel)
instead of their sum (serialised).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, Callable

from fastapi import APIRouter, Depends

from backend import auth as _auth
from backend.db_pool import get_pool
from backend.routers import simulations as _simulations_router
from backend.routers import system as _system_router

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(_auth.current_user)],
)


async def _wrap(coro: Awaitable[Any]) -> dict[str, Any]:
    """Run ``coro`` and wrap its result in the ok/error envelope.

    A raised exception becomes ``{"ok": False, "error": "<type>: <msg>"}``
    so the overall aggregator response stays 200 even when one
    sub-query fails. Matches the frontend's per-key
    ``Promise.allSettled`` fallback semantics — each panel decides
    independently whether to render stale data or an error state.
    """
    try:
        data = await coro
        return {"ok": True, "data": data}
    except Exception as exc:  # noqa: BLE001 — deliberate broad catch: this
        # is the outermost boundary before we flip failures into a
        # structured JSON envelope for the client.
        logger.warning(
            "dashboard.summary subquery failed: %s: %s",
            type(exc).__name__, exc,
        )
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def _run_with_conn(
    fn: Callable[..., Awaitable[Any]],
    /,
    **kwargs: Any,
) -> Any:
    """Borrow a fresh asyncpg connection from the pool and call ``fn``.

    Each PG-bound sub-query (``unread_count``, ``list_simulations``)
    gets its own connection so they can run concurrently under
    ``asyncio.gather`` — asyncpg does not support pipelining two
    operations on one connection.
    """
    async with get_pool().acquire() as conn:
        return await fn(conn=conn, **kwargs)


@router.get("/summary")
async def get_dashboard_summary() -> dict[str, Any]:
    """Aggregate the 11 dashboard fan-out endpoints into one response.

    Replaces the frontend's per-5-second ``Promise.allSettled`` loop
    over 11 ``/runtime/*`` endpoints (see
    ``hooks/use-engine.ts::fetchSystemData``). Concurrency via
    ``asyncio.gather`` + per-subquery ``_wrap`` means one slow or
    failing sub-query does not serialise or take down the happy path.

    Response shape: each of the 11 keys carries an
    ``{"ok": bool, "data": ...}`` or ``{"ok": false, "error": "..."}``
    envelope. Partial failures stay HTTP 200 so the frontend can
    render stale UI for the failed panel while keeping the rest live.
    """
    (
        system_status,
        system_info,
        devices,
        spec,
        repos,
        logs,
        token_usage,
        token_budget,
        notifications_unread,
        compression,
        simulations,
        ollama_tool_failures,
    ) = await asyncio.gather(
        _wrap(_system_router.get_system_status()),
        _wrap(_system_router.get_system_info()),
        _wrap(_system_router.get_devices()),
        _wrap(_system_router.get_spec()),
        _wrap(_system_router.get_repos()),
        _wrap(_system_router.get_logs(limit=50)),
        _wrap(_system_router.get_token_usage()),
        _wrap(_system_router.get_token_budget()),
        _wrap(_run_with_conn(_system_router.unread_count)),
        _wrap(_system_router.get_compression_stats()),
        _wrap(_run_with_conn(
            _simulations_router.list_simulations,
            task_id="",
            agent_id="",
            status="",
            limit=50,
        )),
        # Z.6.5: Ollama tool-call failure counters for dashboard warning.
        _wrap(_system_router.get_ollama_tool_failures()),
    )

    return {
        "systemStatus": system_status,
        "systemInfo": system_info,
        "devices": devices,
        "spec": spec,
        "repos": repos,
        "logs": logs,
        "tokenUsage": token_usage,
        "tokenBudget": token_budget,
        "notificationsUnread": notifications_unread,
        "compression": compression,
        "simulations": simulations,
        "ollamaToolFailures": ollama_tool_failures,
    }
