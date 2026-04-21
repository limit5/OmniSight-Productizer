"""J4 — User preferences API.

GET  /user-preferences         all prefs for current user
GET  /user-preferences/{key}   single pref
PUT  /user-preferences/{key}   upsert a pref

SP-5.8 (2026-04-21): ported to asyncpg pool. 3 compat calls → pool
acquire + $N. ``tenant_where`` → ``tenant_where_pg``. ON CONFLICT
UPSERT already atomic; no new concurrency exposure.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth
from backend.db_context import tenant_insert_value, tenant_where_pg

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
    return {"key": key, "value": body.value}
