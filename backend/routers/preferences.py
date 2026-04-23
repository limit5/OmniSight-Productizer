"""J4 — User preferences API.

GET  /user-preferences         all prefs for current user
GET  /user-preferences/{key}   single pref
PUT  /user-preferences/{key}   upsert a pref

SP-5.8 (2026-04-21): ported to asyncpg pool. 3 compat calls → pool
acquire + $N. ``tenant_where`` → ``tenant_where_pg``. ON CONFLICT
UPSERT already atomic; no new concurrency exposure.

Q.3-SUB-4 (#297, 2026-04-24): PUT emits ``preferences.updated`` on
the event bus so a second device owned by the same user patches its
cached prefs without waiting for the next poll. Emit is best-effort
(``broadcast_scope='user'``, advisory until Q.4 #298) and failures
never fail the HTTP mutation — the PG row is the source of truth.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth
from backend.db_context import tenant_insert_value, tenant_where_pg

logger = logging.getLogger(__name__)

router = APIRouter(tags=["preferences"])


class PrefBody(BaseModel):
    value: str = Field(max_length=65536)


@router.get("/user-preferences")
async def list_preferences(
    user: auth.User = Depends(auth.current_user),
) -> dict:
    from backend.db_pool import get_pool
    params: list = [user.id]
    conditions = ["user_id = $1"]
    tenant_where_pg(conditions, params)
    sql = (
        "SELECT pref_key, value, updated_at FROM user_preferences "
        "WHERE " + " AND ".join(conditions)
    )
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"items": {r["pref_key"]: r["value"] for r in rows}}


@router.get("/user-preferences/{key}")
async def get_preference(
    key: str,
    user: auth.User = Depends(auth.current_user),
) -> dict:
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM user_preferences "
            "WHERE user_id = $1 AND pref_key = $2",
            user.id, key,
        )
    if not row:
        raise HTTPException(status_code=404, detail="preference not found")
    return {"key": key, "value": row["value"]}


@router.put("/user-preferences/{key}")
async def set_preference(
    key: str,
    body: PrefBody,
    user: auth.User = Depends(auth.current_user),
) -> dict:
    from backend.db_pool import get_pool
    now = time.time()
    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO user_preferences "
            "(user_id, pref_key, value, updated_at, tenant_id) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (user_id, pref_key) DO UPDATE SET "
            "  value = EXCLUDED.value, "
            "  updated_at = EXCLUDED.updated_at",
            user.id, key, body.value, now, tenant_insert_value(),
        )
    # Q.3-SUB-4 (#297): cross-device sync push. Best-effort — a flaky
    # bus / Redis outage must not fail the mutation (PG is source of
    # truth, the emit is latency-optimisation only).
    try:
        from backend.events import emit_preferences_updated
        emit_preferences_updated(key, body.value, user.id)
    except Exception as exc:
        logger.debug(
            "emit_preferences_updated failed for key=%s user=%s: %s",
            key, user.id, exc,
        )
    return {"key": key, "value": body.value}
