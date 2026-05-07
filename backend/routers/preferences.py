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

import json
import logging
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from backend import auth
from backend.db_context import tenant_insert_value, tenant_where_pg

logger = logging.getLogger(__name__)

router = APIRouter(tags=["preferences"])

TOUR_SEEN_PREF_KEY = "tour_seen"
SEEN_MP_TOUR_PREF_KEY = "seen_mp_tour"
MP_WAR_ROOM_PANEL_LAYOUT_PREF_KEY = "mp_war_room_panel_layout"
PREF_TRUE_VALUE = "1"


class PrefBody(BaseModel):
    value: str = Field(max_length=65536)


class PreferenceResponse(BaseModel):
    key: str
    value: str


class WarRoomPanel(BaseModel):
    id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    x: int = Field(ge=0, le=100000)
    y: int = Field(ge=0, le=100000)
    width: int = Field(ge=120, le=100000)
    height: int = Field(ge=80, le=100000)
    state: Literal["normal", "minimized", "maximized"] = "normal"


class WarRoomPanelConnection(BaseModel):
    source: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    target: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    kind: str = Field(
        default="related",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )


class WarRoomPanelLayoutBody(BaseModel):
    panels: list[WarRoomPanel] = Field(default_factory=list, max_length=64)
    connections: list[WarRoomPanelConnection] = Field(
        default_factory=list,
        max_length=256,
    )
    version: int = Field(default=1, ge=1, le=1)

    @model_validator(mode="after")
    def validate_layout(self) -> "WarRoomPanelLayoutBody":
        panel_ids = [panel.id for panel in self.panels]
        if len(panel_ids) != len(set(panel_ids)):
            raise ValueError("panel ids must be unique")

        known_panels = set(panel_ids)
        connection_keys: set[tuple[str, str, str]] = set()
        for connection in self.connections:
            if connection.source == connection.target:
                raise ValueError("panel connections must reference two distinct panels")
            if (
                connection.source not in known_panels
                or connection.target not in known_panels
            ):
                raise ValueError("panel connections must reference known panel ids")
            key = (connection.source, connection.target, connection.kind)
            if key in connection_keys:
                raise ValueError("panel connections must be unique")
            connection_keys.add(key)
        return self


class WarRoomPanelLayoutResponse(BaseModel):
    key: str
    value: WarRoomPanelLayoutBody


async def _upsert_preference(user_id: str, key: str, value: str) -> None:
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
            user_id, key, value, now, tenant_insert_value(),
        )


async def _get_preference_value(user_id: str, key: str) -> str | None:
    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM user_preferences "
            "WHERE user_id = $1 AND pref_key = $2",
            user_id, key,
        )
    if not row:
        return None
    return row["value"]


def _emit_preference_updated(key: str, value: str, user_id: str) -> None:
    # Q.3-SUB-4 (#297): cross-device sync push. Best-effort — a flaky
    # bus / Redis outage must not fail the mutation (PG is source of
    # truth, the emit is latency-optimisation only).
    try:
        from backend.events import emit_preferences_updated
        emit_preferences_updated(key, value, user_id)
    except Exception as exc:
        logger.debug(
            "emit_preferences_updated failed for key=%s user=%s: %s",
            key, user_id, exc,
        )


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
    value = await _get_preference_value(user.id, key)
    if value is None:
        raise HTTPException(status_code=404, detail="preference not found")
    return {"key": key, "value": value}


@router.put("/user-preferences/{key}")
async def set_preference(
    key: str,
    body: PrefBody,
    user: auth.User = Depends(auth.current_user),
) -> PreferenceResponse:
    await _upsert_preference(user.id, key, body.value)
    _emit_preference_updated(key, body.value, user.id)
    return {"key": key, "value": body.value}


@router.post("/user-preferences/tour_seen/skip")
async def skip_tour(
    user: auth.User = Depends(auth.current_user),
) -> PreferenceResponse:
    await _upsert_preference(user.id, TOUR_SEEN_PREF_KEY, PREF_TRUE_VALUE)
    _emit_preference_updated(TOUR_SEEN_PREF_KEY, PREF_TRUE_VALUE, user.id)
    return {"key": TOUR_SEEN_PREF_KEY, "value": PREF_TRUE_VALUE}


@router.post("/multi-provider/onboarding-tour/complete")
async def complete_multi_provider_onboarding_tour(
    user: auth.User = Depends(auth.current_user),
) -> PreferenceResponse:
    await _upsert_preference(user.id, SEEN_MP_TOUR_PREF_KEY, PREF_TRUE_VALUE)
    _emit_preference_updated(SEEN_MP_TOUR_PREF_KEY, PREF_TRUE_VALUE, user.id)
    return {"key": SEEN_MP_TOUR_PREF_KEY, "value": PREF_TRUE_VALUE}


@router.get("/multi-provider/war-room/panel-layout")
async def get_multi_provider_war_room_panel_layout(
    user: auth.User = Depends(auth.current_user),
) -> WarRoomPanelLayoutResponse:
    value = await _get_preference_value(user.id, MP_WAR_ROOM_PANEL_LAYOUT_PREF_KEY)
    if value is None:
        layout = WarRoomPanelLayoutBody()
    else:
        try:
            layout = WarRoomPanelLayoutBody.model_validate_json(value)
        except ValueError:
            layout = WarRoomPanelLayoutBody()
    return {"key": MP_WAR_ROOM_PANEL_LAYOUT_PREF_KEY, "value": layout}


@router.put("/multi-provider/war-room/panel-layout")
async def set_multi_provider_war_room_panel_layout(
    body: WarRoomPanelLayoutBody,
    user: auth.User = Depends(auth.current_user),
) -> WarRoomPanelLayoutResponse:
    value = json.dumps(body.model_dump(), separators=(",", ":"), sort_keys=True)
    await _upsert_preference(user.id, MP_WAR_ROOM_PANEL_LAYOUT_PREF_KEY, value)
    _emit_preference_updated(MP_WAR_ROOM_PANEL_LAYOUT_PREF_KEY, value, user.id)
    return {"key": MP_WAR_ROOM_PANEL_LAYOUT_PREF_KEY, "value": body}
