"""I4 — Tenant-scoped secrets REST API.

Endpoints for managing encrypted credentials scoped to the current
tenant. All endpoints require admin role and an active tenant context.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import tenant_secrets as sec
from backend.db_context import require_current_tenant, set_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/secrets", tags=["secrets"])


class SecretCreate(BaseModel):
    key_name: str = Field(..., min_length=1, max_length=128)
    value: str = Field(..., min_length=1)
    secret_type: str = Field("custom", pattern=r"^(git_credential|provider_key|cloudflare_token|webhook_secret|custom)$")
    metadata: dict[str, Any] = Field(default_factory=dict)


class SecretUpdate(BaseModel):
    value: str | None = None
    metadata: dict[str, Any] | None = None


def _ensure_tenant(user: dict) -> str:
    tid = user.get("tenant_id", "t-default")
    set_tenant_id(tid)
    return require_current_tenant()


@router.get("")
async def list_tenant_secrets(
    secret_type: str | None = None,
    user: dict = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    return await sec.list_secrets(secret_type=secret_type)


@router.post("", status_code=201)
async def create_secret(
    body: SecretCreate,
    user: dict = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    sid = await sec.upsert_secret(
        key_name=body.key_name,
        plaintext=body.value,
        secret_type=body.secret_type,
        metadata=body.metadata,
    )
    return {"id": sid, "status": "created"}


@router.put("/{secret_id}")
async def update_secret(
    secret_id: str,
    body: SecretUpdate,
    user: dict = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    existing = await sec.get_secret_value(secret_id)
    if existing is None:
        raise HTTPException(404, "Secret not found")

    if body.value is not None:
        from backend.secret_store import encrypt
        from backend.db_pool import get_pool
        import json
        enc = encrypt(body.value)
        if body.metadata is not None:
            sql = (
                "UPDATE tenant_secrets SET encrypted_value = $1, "
                "updated_at = to_char(clock_timestamp(), "
                "                       'YYYY-MM-DD HH24:MI:SS'), "
                "metadata = $2 WHERE id = $3"
            )
            params = (enc, json.dumps(body.metadata), secret_id)
        else:
            sql = (
                "UPDATE tenant_secrets SET encrypted_value = $1, "
                "updated_at = to_char(clock_timestamp(), "
                "                       'YYYY-MM-DD HH24:MI:SS') "
                "WHERE id = $2"
            )
            params = (enc, secret_id)
        async with get_pool().acquire() as conn:
            await conn.execute(sql, *params)
    elif body.metadata is not None:
        import json
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE tenant_secrets SET metadata = $1, "
                "updated_at = to_char(clock_timestamp(), "
                "                       'YYYY-MM-DD HH24:MI:SS') "
                "WHERE id = $2",
                json.dumps(body.metadata), secret_id,
            )

    return {"id": secret_id, "status": "updated"}


@router.delete("/{secret_id}")
async def delete_secret_endpoint(
    secret_id: str,
    user: dict = Depends(_au.require_admin),
):
    _ensure_tenant(user)
    deleted = await sec.delete_secret(secret_id)
    if not deleted:
        raise HTTPException(404, "Secret not found")
    return {"status": "deleted"}
