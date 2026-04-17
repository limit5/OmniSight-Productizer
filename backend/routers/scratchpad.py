"""R3 (#309) — Scratchpad REST endpoints.

Read-only endpoints the Agent Matrix Wall Scratchpad Progress
Indicator (and the optional preview popover) use to render
per-agent scratchpad state. The full markdown preview is decrypted
server-side so the ciphertext never leaves the host.

All endpoints return JSON. The markdown preview is returned as a
string field rather than as a raw body so the UI can co-locate it
with meta (turn, age, subtask).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend import scratchpad as sp

router = APIRouter(prefix="/scratchpad", tags=["scratchpad"])


@router.get("/agents")
async def list_agents() -> dict:
    """Compact summary for every agent that has a scratchpad.

    Empty list if nothing's been flushed yet. The UI treats that as
    "the agent either hasn't been started or crashed before the first
    save" and falls back to the old card layout.
    """
    return {"agents": sp.ui_summary_all()}


@router.get("/agents/{agent_id}")
async def get_summary(agent_id: str) -> dict:
    try:
        summary = sp.ui_summary(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if summary is None:
        raise HTTPException(status_code=404, detail="No scratchpad for agent")
    return summary


@router.get("/agents/{agent_id}/preview")
async def get_preview(agent_id: str, max_chars: int = 8000) -> dict:
    """Decrypted markdown body, capped so the UI doesn't choke on a
    100 KB paste. ``max_chars`` default aligns with the UI modal's
    max height of ~120 lines.
    """
    try:
        text = sp.preview_markdown(agent_id, max_chars=max(256, min(max_chars, 64000)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if text is None:
        raise HTTPException(status_code=404, detail="No scratchpad for agent")
    return {
        "agent_id": agent_id,
        "markdown": text,
        "chars": len(text),
    }


@router.get("/agents/{agent_id}/archive")
async def get_archive(agent_id: str) -> dict:
    try:
        items = sp.list_archive(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"agent_id": agent_id, "items": items}
