"""Phase 63-E — admin endpoint for restoring decayed memories.

Only exposes `restore` (not delete) — the locked design rule is that
rows never vanish. Listing happens via existing search paths.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import memory_decay

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/memory", tags=["memory"])


class CogneeDecisionNodeWriteRequest(BaseModel):
    """Decision-node payload posted by the coordinator learning loop."""

    kind: str = Field(min_length=1)
    identifier: str = Field(min_length=1)
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/{memory_id}/restore")
async def restore_memory(memory_id: str, _user=Depends(_au.require_admin)) -> dict:
    score = await memory_decay.restore(memory_id)
    if score is None:
        raise HTTPException(status_code=404, detail=f"memory {memory_id!r} not found")
    return {"id": memory_id, "decayed_score": score}


@router.post("/cognee/decision-nodes")
async def write_cognee_decision_node(
    request: CogneeDecisionNodeWriteRequest,
    _user=Depends(_au.require_admin),
) -> dict:
    """Ingest one coordinator decision node through the backend Cognee stack."""
    try:
        from backend.agents.cognee_integration import CogneeAdapter, IngestSource

        report = await CogneeAdapter.from_env().ingest([
            IngestSource(
                kind=request.kind,
                identifier=request.identifier,
                content=request.content,
                metadata=request.metadata,
            )
        ])
    except Exception as exc:  # noqa: BLE001 — API caller degrades fail-open
        logger.info("memory.cognee_decision_write_unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="cognee write unavailable") from exc
    return {"written": report.sources_ingested >= 1}
