"""WP.8 — Runbook surface endpoints.

Three operator-facing routes mirror the WP.2 ``/skills`` surface:

* ``GET /runbooks/effective`` — list effective runbooks across the 3
  scopes (project > home > bundled) so the dashboard can render a
  catalog.
* ``POST /runbooks/save-from-block`` — synthesise a Runbook YAML from
  one :class:`backend.models.Block` and persist it into the project
  scope (``.omnisight/runbooks/<name>.yaml``). Returns the resulting
  YAML body so the UI can render a confirmation preview.
* ``POST /runbooks/{name}/execute`` — run an effective runbook with
  operator-supplied params and return the produced :class:`Block`
  chain. The first block's payload pins ``runbook.params`` so the
  output chain is self-describing.

Auth is the same operator-tier dependency the WP.2 skill router uses.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend.agents import runbook_loader
from backend.models import Block

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/runbooks", tags=["runbooks"])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _project_root() -> Path:
    """Resolution hook so tests can monkey-patch the project root."""
    return _PROJECT_ROOT


def _runbook_to_dict(rb: runbook_loader.Runbook) -> dict[str, Any]:
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


# ─── Models ──────────────────────────────────────────────────────


class BlockInput(BaseModel):
    """Block fields the synthesizer cares about.

    Accepting the relevant subset (rather than the full Block model) lets
    the FE post a slimmed payload without re-validating fields the
    synthesizer doesn't read.
    """

    block_id: str
    tenant_id: str
    user_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    kind: str
    status: str = "completed"
    title: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SaveFromBlockRequest(BaseModel):
    block: BlockInput
    name: str | None = Field(
        default=None,
        max_length=64,
        description="Optional name override; defaults to slugified block title.",
    )
    description: str | None = Field(default=None, max_length=512)
    tags: list[str] = Field(default_factory=list)
    overwrite: bool = False


class ExecuteRunbookRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str
    user_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    parent_block_id: str | None = None


# ─── Endpoints ───────────────────────────────────────────────────


@router.get("/effective")
async def list_effective_runbooks(_user=Depends(_au.require_operator)) -> dict:
    """List runbooks effective at the current 3-scope resolution.

    Module-global state audit: this endpoint keeps no process-global
    registry; every call re-derives the same effective catalog from the
    shared filesystem using the WP.8 precedence rules (mirror of WP.2).
    """
    registry = runbook_loader.load_default_scopes(_project_root())
    items = [_runbook_to_dict(rb) for rb in registry.list_all()]
    return {"items": items, "count": len(items)}


@router.post("/save-from-block")
async def save_from_block(
    req: SaveFromBlockRequest,
    _user=Depends(_au.require_operator),
) -> dict:
    """Synthesise + persist a Runbook stub from one Block.

    The Block context-menu "Save as Runbook" entry in
    ``components/omnisight/block.tsx`` posts here. The endpoint slugs
    the runbook name, derives params from ``{{ var }}`` placeholders in
    the block payload, writes the YAML to the project scope, and
    returns the synthesised runbook + on-disk path so the UI can render
    a confirmation.
    """
    block = Block(
        block_id=req.block.block_id,
        tenant_id=req.block.tenant_id,
        user_id=req.block.user_id,
        project_id=req.block.project_id,
        session_id=req.block.session_id,
        kind=req.block.kind,
        status=req.block.status,
        title=req.block.title,
        payload=dict(req.block.payload),
        metadata=dict(req.block.metadata),
    )
    runbook = runbook_loader.synthesize_runbook_from_block(
        block,
        name=req.name,
        description=req.description,
        tags=req.tags,
    )
    try:
        target_path = runbook_loader.write_project_runbook(
            runbook,
            project_root=_project_root(),
            overwrite=req.overwrite,
        )
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    yaml_body = runbook_loader.runbook_to_yaml(runbook)
    logger.info(
        "runbooks: synthesised %r from block %s → %s",
        runbook.name,
        block.block_id,
        target_path,
    )
    return {
        "runbook": _runbook_to_dict(runbook),
        "path": str(target_path),
        "yaml": yaml_body,
    }


@router.post("/{name}/execute")
async def execute_runbook(
    name: str,
    req: ExecuteRunbookRequest,
    _user=Depends(_au.require_operator),
) -> dict:
    """Execute a runbook against operator-supplied params.

    Returns the produced Block chain (one block per step, ``parent_id``
    chained back through ``req.parent_block_id``). If required params
    are missing the response is 422 with the list of missing names so
    the UI can re-prompt instead of dispatching a half-resolved run.
    """
    registry = runbook_loader.load_default_scopes(_project_root())
    runbook = registry.get(name)
    if runbook is None:
        raise HTTPException(status_code=404, detail=f"runbook {name!r} not found")

    _resolved, missing = runbook_loader.resolve_params(runbook, req.params)
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"missing_params": sorted(missing)},
        )

    try:
        chain = runbook_loader.execute_runbook(
            runbook,
            req.params,
            tenant_id=req.tenant_id,
            user_id=req.user_id,
            project_id=req.project_id,
            session_id=req.session_id,
            parent_block_id=req.parent_block_id,
        )
    except runbook_loader.RunbookExecutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "runbook": _runbook_to_dict(runbook),
        "blocks": [b.model_dump() for b in chain],
    }
