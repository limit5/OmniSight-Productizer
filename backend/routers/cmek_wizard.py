"""KS.2.1 -- CMEK tenant-settings wizard REST surface.

Routes are tenant-admin scoped and intentionally stateless. KS.2.11
will add durable ``cmek_configs`` / ``tier_assignments`` storage; until
then ``complete`` returns a Tier 2 draft summary for the settings UI.

Module-global state audit (SOP Step 1)
--------------------------------------
Only immutable route constants and Pydantic classes are module globals.
Provider metadata lives in ``backend.security.cmek_wizard`` as immutable
dataclasses. No in-memory cache or singleton is introduced.

Read-after-write timing audit (SOP Step 1)
------------------------------------------
No shared writes happen in this router. Every request computes its
response from request input, so there is no read-after-write race.
"""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from backend import auth
from backend.security import cmek_wizard as _cw


router = APIRouter(tags=["cmek-wizard"])

TENANT_ID_PATTERN = r"^t-[a-z0-9][a-z0-9-]{2,62}$"
_TENANT_ID_RE = re.compile(TENANT_ID_PATTERN)
_ALLOWED_MEMBERSHIP_ROLES = frozenset({"owner", "admin"})


def _is_valid_tenant_id(tid: str) -> bool:
    return bool(tid) and bool(_TENANT_ID_RE.match(tid))


async def _user_can_manage_cmek(user: auth.User, tenant_id: str) -> bool:
    if auth.role_at_least(user.role, "super_admin"):
        return True

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role, status FROM user_tenant_memberships "
            "WHERE user_id = $1 AND tenant_id = $2",
            user.id, tenant_id,
        )
    if row is None or row["status"] != "active":
        return False
    return row["role"] in _ALLOWED_MEMBERSHIP_ROLES


class GeneratePolicyRequest(BaseModel):
    provider: Literal["aws-kms", "gcp-kms", "vault-transit"]
    principal: str = Field(min_length=1, max_length=512)
    key_id: str | None = Field(default=None, max_length=512)


class VerifyCMEKRequest(BaseModel):
    provider: Literal["aws-kms", "gcp-kms", "vault-transit"]
    key_id: str = Field(min_length=1, max_length=512)


class CompleteCMEKRequest(BaseModel):
    provider: Literal["aws-kms", "gcp-kms", "vault-transit"]
    key_id: str = Field(min_length=1, max_length=512)
    verification_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _verification_id_shape(self) -> "CompleteCMEKRequest":
        if not self.verification_id.startswith("cmekv_"):
            raise ValueError("verification_id must come from the verify step")
        return self


async def _guard(tenant_id: str, actor: auth.User) -> JSONResponse | None:
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid tenant id: {tenant_id!r}; "
                    f"must match {TENANT_ID_PATTERN}"
                ),
            },
        )
    if not await _user_can_manage_cmek(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=f"requires tenant owner/admin or super_admin on {tenant_id!r}",
        )
    return None


@router.get("/tenants/{tenant_id}/cmek/wizard/providers")
async def list_cmek_wizard_providers(
    tenant_id: str,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    guarded = await _guard(tenant_id, actor)
    if guarded is not None:
        return guarded
    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "providers": _cw.list_provider_specs(),
        }
    )


@router.post("/tenants/{tenant_id}/cmek/wizard/policy")
async def generate_cmek_wizard_policy(
    tenant_id: str,
    req: GeneratePolicyRequest,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    guarded = await _guard(tenant_id, actor)
    if guarded is not None:
        return guarded
    try:
        provider = _cw.normalise_provider(req.provider)
        policy = _cw.generate_policy_json(
            provider,
            tenant_id=tenant_id,
            principal=req.principal,
            key_id=req.key_id,
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "provider": provider,
            "policy": policy,
            "policy_json": _cw.stable_policy_json(policy),
        }
    )


@router.post("/tenants/{tenant_id}/cmek/wizard/verify")
async def verify_cmek_wizard_connection(
    tenant_id: str,
    req: VerifyCMEKRequest,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    guarded = await _guard(tenant_id, actor)
    if guarded is not None:
        return guarded
    try:
        result = _cw.verify_connection_probe(
            _cw.normalise_provider(req.provider),
            tenant_id=tenant_id,
            key_id=req.key_id,
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    return JSONResponse({"tenant_id": tenant_id, **result})


@router.post("/tenants/{tenant_id}/cmek/wizard/complete")
async def complete_cmek_wizard(
    tenant_id: str,
    req: CompleteCMEKRequest,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    guarded = await _guard(tenant_id, actor)
    if guarded is not None:
        return guarded
    try:
        provider = _cw.normalise_provider(req.provider)
        key_id = _cw.validate_key_id(provider, req.key_id)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "security_tier": "tier-2",
            "provider": provider,
            "key_id": key_id,
            "verification_id": req.verification_id,
            "config_status": "draft",
            "persisted": False,
        }
    )
