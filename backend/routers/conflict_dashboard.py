"""OP-746 -- /admin/conflict-trend backend.

One read-only endpoint that powers the operator-dashboard tile:

  GET /admin/conflict-trend
       Returns 24h + 7d roll-ups of the conflict-observations table
       plus the top-3 hotspot files. The tile in
       ``components/admin/conflict-trend-tile.tsx`` renders this as a
       small panel on the existing operator dashboard.

Auth model
----------
``auth.require_admin`` — same gate as the OP-735 batch-merge dashboard.
The data is operational hygiene, not customer data, but it is bot-
ownership-sensitive so we don't expose it to viewers.

Module-global state audit
-------------------------
None introduced. Reads go through the asyncpg pool, which is
process-global but shared across uvicorn workers via PG. The aggregate
is recomputed on every request — no caching layer added; if/when this
tile gets hot enough to matter we can cache by minute.

Read-after-write timing audit
-----------------------------
Read-only endpoint. The async reader uses asyncpg's per-statement
autocommit semantics so newly-inserted observations from the bridge
become visible on the next request.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend import auth
from backend.agents.conflict_observations import (
    fetch_recent_observations,
    summarise,
)
from backend.db_pool import get_conn


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/conflict-trend", tags=["admin", "conflict-trend"])


@router.get("")
async def get_conflict_trend(
    _user: auth.User = Depends(auth.require_admin),
    conn: asyncpg.Connection = Depends(get_conn),
) -> JSONResponse:
    """Return 24h + 7d snapshots for the operator dashboard tile.

    Shape:

    .. code-block:: json

       {
         "fetched_at": "...",
         "window_24h": {
           "window_start": "...", "window_end": "...",
           "total_events": 8,
           "by_cause": {"sibling_merged": 6, "verified_minus_one": 2},
           "hotspots": [{"file": "...", "events": 5}, ...],
           "distinct_changes": 7
         },
         "window_7d": { ... same shape ... }
       }
    """
    now = datetime.now(timezone.utc)
    start_24h = now - timedelta(hours=24)
    start_7d = now - timedelta(days=7)

    obs_7d = await fetch_recent_observations(conn, since=start_7d)
    obs_24h = [o for o in obs_7d if o.ts >= start_24h]

    summary_24h = summarise(
        obs_24h, window_start=start_24h, window_end=now, top_n_hotspots=3,
    )
    summary_7d = summarise(
        obs_7d, window_start=start_7d, window_end=now, top_n_hotspots=3,
    )

    return JSONResponse({
        "fetched_at": now.isoformat(),
        "window_24h": summary_24h.to_payload(),
        "window_7d": summary_7d.to_payload(),
    })
