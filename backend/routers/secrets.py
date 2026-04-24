"""I4 — Tenant-scoped secrets REST API.

Endpoints for managing encrypted credentials scoped to the current
tenant. All endpoints require admin role and an active tenant context.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import optimistic_lock as _ol
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
    if_match: str | None = Header(None, alias="If-Match"),
):
    """Rotate a secret value and/or its metadata.

    Q.7 #301 — requires ``If-Match: <version>`` header. Two admins
    rotating the same provider key from laptop + phone concurrently
    produce exactly one winner (post-bump version echoed in the
    response) and one 409 (body carries ``current_version`` /
    ``your_version`` / ``hint`` for the frontend ``use409Conflict``
    hook).
    """
    _ensure_tenant(user)
    expected_version = _ol.parse_if_match(if_match)
    existing = await sec.get_secret_value(secret_id)
    if existing is None:
        raise HTTPException(404, "Secret not found")

    from backend.db_pool import get_pool
    import json
    updates: dict[str, object] = {}
    if body.value is not None:
        from backend.secret_store import encrypt
        updates["encrypted_value"] = encrypt(body.value)
        updates["updated_at"] = _pg_now_str()
    if body.metadata is not None:
        updates["metadata"] = json.dumps(body.metadata)
        if "updated_at" not in updates:
            updates["updated_at"] = _pg_now_str()

    async with get_pool().acquire() as conn:
        try:
            new_version = await _ol.bump_version_pg(
                conn,
                "tenant_secrets",
                pk_col="id",
                pk_value=secret_id,
                expected_version=expected_version,
                updates=updates,
            )
        except _ol.VersionConflict as conflict:
            if conflict.current_version is None:
                raise HTTPException(404, "Secret not found")
            _ol.raise_conflict(
                conflict.current_version,
                conflict.your_version,
                resource="tenant_secret",
            )

    return {"id": secret_id, "status": "updated", "version": new_version}


def _pg_now_str() -> str:
    """Mirror the ``to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS')``
    timestamp format the pre-Q.7 code used for ``updated_at``. Computing
    it application-side (vs. SQL) keeps ``_ol.bump_version_pg``'s
    column-agnostic signature usable here.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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
