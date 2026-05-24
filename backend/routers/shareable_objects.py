"""WP.1.4 / WP.9 -- ``/shareable-objects`` permalink endpoints.

The WP.1 ``<Block />`` primitive's share dialog calls
``POST /api/v1/shareable-objects`` to mint a WP.9 permalink slug for the
right-clicked Block.  This module wires the FE call through to the
existing :mod:`backend.shareable_objects` helpers and returns the
permalink URL the dialog renders back to the operator.

Module-global state audit (SOP Step 1)
--------------------------------------
The router introduces no new module-level mutable state.  Slug minting
and the ``INSERT ... ON CONFLICT DO NOTHING`` collision arbitration are
owned by the existing WP.9.2 helper; every worker reads / writes the
same ``shareable_objects`` table.

Read-after-write timing audit (SOP Step 1)
------------------------------------------
The endpoint runs one ``create_shareable_object`` insert inside a
single ``get_pool().acquire()`` connection.  The same response is built
from the row returned by that insert, so the caller sees its own write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from backend import auth
from backend import shareable_objects as _so
from backend.agents import runbook_loader
from backend.block_redaction import (
    BLOCK_SHARE_REGIONS,
    REDACTION_REASONS,
    normalise_share_regions,
)
from backend.db_pool import get_pool


router = APIRouter(prefix="/shareable-objects", tags=["shareable-objects"])


ShareableObjectVisibility = Literal["private", "team", "tenant", "public"]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_FETCH_ACTIVE_SHAREABLE_OBJECT_SQL = """
SELECT share_id, object_kind, object_id, tenant_id, owner_user_id,
       visibility, expires_at, redaction_applied, created_at
FROM shareable_objects
WHERE share_id = $1
  AND (expires_at IS NULL OR expires_at > now())
"""

_FETCH_BLOCK_SHARE_PAYLOAD_SQL = """
SELECT block_id, parent_id, tenant_id, user_id, project_id, session_id,
       kind, status, title, payload, metadata, started_at, completed_at,
       created_at
FROM blocks
WHERE block_id = $1
  AND tenant_id = $2
"""


class CreateShareableObjectRequest(BaseModel):
    object_kind: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Generic object kind (block / runbook / ...).",
    )
    object_id: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="ID of the underlying object being shared.",
    )
    tenant_id: str | None = Field(
        default=None,
        description="Override tenant for the share row. Defaults to the caller's tenant.",
    )
    visibility: ShareableObjectVisibility = Field(
        default="private",
        description="Stored ACL tier; WP.9.3 enforces it at read time.",
    )
    regions: list[str] | None = Field(
        default=None,
        description="WP.1.5 share regions (block kind only).",
    )
    redaction_mask: dict[str, Any] | None = Field(
        default=None,
        description="Operator-marked redaction mask (path -> reason).",
    )
    base_url: str | None = Field(
        default=None,
        max_length=512,
        description="Optional base URL the FE uses to build the permalink.",
    )
    expires_at: str | None = Field(
        default=None,
        description="Reserved for WP.9.4 expiry sweeps; unused here.",
    )

    @field_validator("object_kind")
    @classmethod
    def _strip_object_kind(cls, value: str) -> str:
        return value.strip()

    @field_validator("object_id")
    @classmethod
    def _strip_object_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("tenant_id")
    @classmethod
    def _strip_tenant_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


def _validate_redaction_mask(mask: dict[str, Any]) -> dict[str, Any]:
    """Reject malformed mask payloads so they cannot land in the DB.

    The mask shape is ``{path: reason}`` or ``{path: [reason, ...]}``.
    Unknown reasons are coerced out -- the durable row must only carry
    the four WP.1.5 reasons (``secret`` / ``pii`` / ``customer_ip`` /
    ``ks_envelope``) so WP.9.5 enforcement is unambiguous.
    """
    cleaned: dict[str, Any] = {}
    for path, reason in mask.items():
        if not isinstance(path, str) or not path.strip():
            raise HTTPException(
                status_code=400,
                detail="redaction_mask keys must be non-empty path strings",
            )
        if isinstance(reason, (list, tuple)):
            reasons = [str(item).strip() for item in reason if str(item).strip()]
            allowed = [item for item in reasons if item in REDACTION_REASONS]
            if not allowed:
                raise HTTPException(
                    status_code=400,
                    detail=f"redaction_mask[{path}] has no recognised reasons",
                )
            cleaned[path.strip()] = allowed if len(allowed) > 1 else allowed[0]
        else:
            text = str(reason).strip()
            if text not in REDACTION_REASONS:
                raise HTTPException(
                    status_code=400,
                    detail=f"redaction_mask[{path}] reason '{text}' not recognised",
                )
            cleaned[path.strip()] = text
    return cleaned


def _validate_block_regions(regions: list[str] | None) -> list[str]:
    """For block shares, regions must be a non-empty subset of WP.1.5."""
    if regions is None:
        return list(BLOCK_SHARE_REGIONS)
    requested = {str(item).strip() for item in regions if str(item).strip()}
    unknown = requested - set(BLOCK_SHARE_REGIONS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"regions contain unknown values: {sorted(unknown)}",
        )
    selected = list(normalise_share_regions(regions))
    if not selected:
        raise HTTPException(
            status_code=400,
            detail="regions must include at least one block share region",
        )
    return selected


def _safe_base_url(base_url: str | None) -> str | None:
    if not base_url:
        return None
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail="base_url must be an absolute http(s) URL",
        )
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _build_redaction_applied(
    object_kind: str,
    regions: list[str] | None,
    mask: dict[str, Any],
) -> dict[str, Any]:
    """Pack the durable ``redaction_applied`` audit payload.

    Keeping both ``regions`` and ``mask`` makes the WP.9.5 read path
    unambiguous about which Block sub-regions were exposed and which
    paths were masked, without needing to refetch the Block.
    """
    if object_kind == "block":
        return {
            "regions": list(regions) if regions is not None else [],
            "mask": dict(mask),
        }
    return {"mask": dict(mask)} if mask else {}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("share payload JSON fields must be objects")


def _json_scalar(value: Any) -> Any:
    return None if value is None else str(value)


def _project_root() -> Path:
    """Resolution hook so tests can monkey-patch the runbook root."""
    return _PROJECT_ROOT


def _runbook_to_share_payload(rb: runbook_loader.Runbook) -> dict[str, Any]:
    return {
        "name": rb.name,
        "description": rb.description,
        "tags": list(rb.tags),
        "source_url": rb.source_url,
        "scope": rb.scope,
        "source_path": str(rb.source_path) if rb.source_path else None,
        "params": [
            {
                "name": p.name,
                "type": p.type,
                "default": p.default,
                "description": p.description,
                "required": p.required,
            }
            for p in rb.params
        ],
        "steps": [
            {"kind": s.kind, "title": s.title, "payload": dict(s.payload)}
            for s in rb.steps
        ],
    }


async def _load_shared_object_payload(
    conn,
    share: _so.ShareableObject,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the payload/metadata pair for object kinds share UI mints."""
    if share.object_kind == "block":
        row = await conn.fetchrow(
            _FETCH_BLOCK_SHARE_PAYLOAD_SQL,
            share.object_id,
            share.tenant_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="shared object not found")
        payload = _json_object(row["payload"])
        metadata = _json_object(row["metadata"])
        metadata.update({
            "block_id": row["block_id"],
            "parent_id": row["parent_id"],
            "tenant_id": row["tenant_id"],
            "user_id": row["user_id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "kind": row["kind"],
            "status": row["status"],
            "title": row["title"],
            "started_at": _json_scalar(row["started_at"]),
            "completed_at": _json_scalar(row["completed_at"]),
            "created_at": _json_scalar(row["created_at"]),
        })
        return payload, metadata

    if share.object_kind == "runbook":
        registry = runbook_loader.load_default_scopes(_project_root())
        rb = registry.get(share.object_id)
        if rb is None:
            raise HTTPException(status_code=404, detail="shared object not found")
        return _runbook_to_share_payload(rb), {}

    return {"object_id": share.object_id}, {}


def _redaction_mask_for_share(share: _so.ShareableObject) -> Mapping[str, Any]:
    redaction = share.redaction_applied
    if not isinstance(redaction, Mapping):
        return {}
    mask = redaction.get("mask")
    if isinstance(mask, Mapping):
        return mask
    return redaction


@router.post("")
async def create_shareable_object(
    req: CreateShareableObjectRequest,
    actor: auth.User = Depends(auth.require_viewer),
) -> JSONResponse:
    """Mint a WP.9 permalink slug for the requested object.

    The Block share dialog (``components/omnisight/block.tsx``) calls
    this with ``object_kind="block"`` plus the operator's selected
    regions and operator-marked redaction mask.  The router pins the
    durable shape stored in ``shareable_objects.redaction_applied`` and
    returns the absolute permalink URL the dialog renders.
    """
    object_kind = req.object_kind
    try:
        _so._validate_object_kind(object_kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        _so.validate_visibility(req.visibility)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    mask = _validate_redaction_mask(dict(req.redaction_mask or {}))

    regions: list[str] | None = None
    if object_kind == "block":
        regions = _validate_block_regions(req.regions)
    elif req.regions:
        raise HTTPException(
            status_code=400,
            detail="regions are only meaningful for object_kind='block'",
        )

    base_url = _safe_base_url(req.base_url)
    tenant_id = req.tenant_id or actor.tenant_id
    if not tenant_id:
        raise HTTPException(
            status_code=400,
            detail="tenant_id could not be resolved from request or caller",
        )

    redaction_applied = _build_redaction_applied(object_kind, regions, mask)

    try:
        async with get_pool().acquire() as conn:
            share = await _so.create_shareable_object(
                conn,
                object_kind=object_kind,
                object_id=req.object_id,
                tenant_id=tenant_id,
                owner_user_id=actor.id,
                visibility=req.visibility,
                redaction_applied=redaction_applied,
            )
    except _so.ShareSlugCollisionError as exc:
        raise HTTPException(
            status_code=503,
            detail="share slug collision retry budget exhausted; please retry",
        ) from exc

    permalink_url = (
        f"{base_url}/share/{share.share_id}" if base_url else None
    )

    body: dict[str, Any] = {
        "share_id": share.share_id,
        "object_kind": share.object_kind,
        "object_id": share.object_id,
        "visibility": share.visibility,
        "expires_at": (
            None if share.expires_at is None else str(share.expires_at)
        ),
    }
    if permalink_url:
        body["permalink_url"] = permalink_url

    return JSONResponse(status_code=201, content=body)


@router.get("/{share_id}")
async def resolve_shareable_object(
    share_id: str,
    actor: auth.User = Depends(auth.require_viewer),
) -> JSONResponse:
    """Resolve a minted permalink through WP.9 ACL + redaction policy."""
    if not _so.is_valid_share_slug(share_id):
        raise HTTPException(status_code=404, detail="share not found")

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(_FETCH_ACTIVE_SHAREABLE_OBJECT_SQL, share_id)
        if row is None:
            raise HTTPException(status_code=404, detail="share not found")

        share = _so._row_to_shareable_object(row)
        allowed = await _so.user_can_access_shareable_object(
            conn,
            share,
            caller_user_id=actor.id,
            caller_role=actor.role,
        )
        if not allowed:
            raise HTTPException(status_code=404, detail="share not found")

        payload, metadata = await _load_shared_object_payload(conn, share)
        body = {
            "share_id": share.share_id,
            "object_kind": share.object_kind,
            "object_id": share.object_id,
            "visibility": share.visibility,
            "expires_at": (
                None if share.expires_at is None else str(share.expires_at)
            ),
            "payload": payload,
            "metadata": metadata,
        }

        redaction_share = {
            **share.to_dict(),
            "redaction_applied": _redaction_mask_for_share(share),
        }
        try:
            redacted = _so.build_share_payload(redaction_share, body)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="share redaction mask could not be applied",
            ) from exc

    return JSONResponse(status_code=200, content=redacted)
