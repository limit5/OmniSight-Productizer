"""J4 — User preferences API.

GET  /user-preferences         all prefs for current user
GET  /user-preferences/{key}   single pref
PUT  /user-preferences/{key}   upsert a pref
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth, db
from backend.db_context import tenant_insert_value, tenant_where

router = APIRouter(tags=["preferences"])


class PrefBody(BaseModel):
    value: str = Field(max_length=65536)


@router.get("/user-preferences")
async def list_preferences(
    user: auth.User = Depends(auth.current_user),
) -> dict:
    conn = db._conn()
    conditions = ["user_id=?"]
    params: list = [user.id]
    tenant_where(conditions, params)
    sql = "SELECT pref_key, value, updated_at FROM user_preferences WHERE " + " AND ".join(conditions)
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return {
        "items": {r["pref_key"]: r["value"] for r in rows},
    }


@router.get("/user-preferences/{key}")
async def get_preference(
    key: str,
    user: auth.User = Depends(auth.current_user),
) -> dict:
    conn = db._conn()
    async with conn.execute(
        "SELECT value FROM user_preferences WHERE user_id=? AND pref_key=?",
        (user.id, key),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="preference not found")
    return {"key": key, "value": row["value"]}


@router.put("/user-preferences/{key}")
async def set_preference(
    key: str,
    body: PrefBody,
    user: auth.User = Depends(auth.current_user),
) -> dict:
    conn = db._conn()
    now = time.time()
    await conn.execute(
        "INSERT INTO user_preferences (user_id, pref_key, value, updated_at, tenant_id) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, pref_key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (user.id, key, body.value, now, tenant_insert_value()),
    )
    await conn.commit()
    return {"key": key, "value": body.value}
