"""K6 — API key management router.

Admin-only endpoints for creating, rotating, revoking, and listing
per-service bearer tokens that replace the old single-env bearer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend import api_keys as _ak
from backend import audit
from backend import auth as _au

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class CreateKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["*"])


class UpdateScopesRequest(BaseModel):
    scopes: list[str] = Field(..., min_length=1)


@router.get("")
async def list_keys(
    _user: _au.User = Depends(_au.require_admin),
) -> dict:
    keys = await _ak.list_keys()
    return {"items": [k.to_dict() for k in keys], "count": len(keys)}


@router.post("")
async def create_key(
    req: CreateKeyRequest,
    request: Request,
    user: _au.User = Depends(_au.require_admin),
) -> dict:
    key, raw_secret = await _ak.create_key(
        name=req.name, scopes=req.scopes, created_by=user.email,
    )
    await audit.write_audit(
        request, action="api_key_create", entity_kind="api_key",
        entity_id=key.id,
        after={"name": key.name, "scopes": key.scopes},
        actor=user.email,
    )
    return {"key": key.to_dict(), "secret": raw_secret}


@router.post("/{key_id}/rotate")
async def rotate_key(
    key_id: str,
    request: Request,
    user: _au.User = Depends(_au.require_admin),
) -> dict:
    key, raw_secret = await _ak.rotate_key(key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    await audit.write_audit(
        request, action="api_key_rotate", entity_kind="api_key",
        entity_id=key_id, actor=user.email,
    )
    return {"key": key.to_dict(), "secret": raw_secret}


@router.post("/{key_id}/revoke")
async def revoke_key(
    key_id: str,
    request: Request,
    user: _au.User = Depends(_au.require_admin),
) -> dict:
    ok = await _ak.revoke_key(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="API key not found")
    await audit.write_audit(
        request, action="api_key_revoke", entity_kind="api_key",
        entity_id=key_id, actor=user.email,
    )
    return {"revoked": True, "id": key_id}


@router.post("/{key_id}/enable")
async def enable_key(
    key_id: str,
    request: Request,
    user: _au.User = Depends(_au.require_admin),
) -> dict:
    ok = await _ak.enable_key(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="API key not found")
    await audit.write_audit(
        request, action="api_key_enable", entity_kind="api_key",
        entity_id=key_id, actor=user.email,
    )
    return {"enabled": True, "id": key_id}


@router.delete("/{key_id}")
async def delete_key(
    key_id: str,
    request: Request,
    user: _au.User = Depends(_au.require_admin),
) -> dict:
    ok = await _ak.delete_key(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="API key not found")
    await audit.write_audit(
        request, action="api_key_delete", entity_kind="api_key",
        entity_id=key_id, actor=user.email,
    )
    return {"deleted": True, "id": key_id}


@router.patch("/{key_id}/scopes")
async def update_scopes(
    key_id: str,
    req: UpdateScopesRequest,
    request: Request,
    user: _au.User = Depends(_au.require_admin),
) -> dict:
    key = await _ak.get_key(key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    old_scopes = key.scopes
    ok = await _ak.update_scopes(key_id, req.scopes)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to update scopes")
    await audit.write_audit(
        request, action="api_key_scopes_update", entity_kind="api_key",
        entity_id=key_id,
        before={"scopes": old_scopes},
        after={"scopes": req.scopes},
        actor=user.email,
    )
    return {"id": key_id, "scopes": req.scopes}
