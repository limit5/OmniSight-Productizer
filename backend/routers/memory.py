"""Phase 63-E — admin endpoint for restoring decayed memories.

Only exposes `restore` (not delete) — the locked design rule is that
rows never vanish. Listing happens via existing search paths.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from backend import auth as _au
from backend import memory_decay

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/memory", tags=["memory"])


@router.post("/{memory_id}/restore")
async def restore_memory(memory_id: str, _user=Depends(_au.require_admin)) -> dict:
    score = await memory_decay.restore(memory_id)
    if score is None:
        raise HTTPException(status_code=404, detail=f"memory {memory_id!r} not found")
    return {"id": memory_id, "decayed_score": score}
