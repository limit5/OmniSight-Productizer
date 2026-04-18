"""R1 (#307) — ChatOps router.

Exposes:

* ``POST /chatops/webhook/discord`` — Discord Interaction endpoint.
* ``POST /chatops/webhook/teams``   — Teams Bot / outgoing webhook.
* ``POST /chatops/webhook/line``    — Line messaging webhook.
* ``GET  /chatops/mirror``          — recent ChatOps messages (ring).
* ``GET  /chatops/status``          — adapter connection status.
* ``POST /chatops/inject``          — dashboard-side inject hint surface
  (same sanitize + rate-limit as the Discord ``/omnisight inject`` route).
* ``POST /chatops/send``            — dashboard-side send-interactive
  (used for E2E integration tests + manual on-call pokes).
* ``POST /pep/decision/{pep_id}``   — thin alias resolving a PEP held
  id to its decision engine id and forwarding approve/reject (spec line
  item from R1).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import chatops_bridge as bridge
from backend import agent_hints
from backend import chatops_handlers as _chatops_handlers  # noqa: F401 — side-effect registration

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chatops"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Webhooks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/chatops/webhook/discord")
async def discord_webhook(request: Request) -> dict[str, Any]:
    raw = await request.body()
    import json as _json
    try:
        payload = _json.loads(raw.decode("utf-8") or "{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"bad json: {exc}")

    # Discord PING (type=1) must be answered with {"type":1}. Do this
    # before signature verification? No — Discord signs PING too.
    mod = bridge.get_adapter("discord")
    try:
        mod.verify(dict(request.headers), raw)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    if payload.get("type") == 1:  # PING
        return {"type": 1}

    inbound = mod.parse_inbound(payload)
    result = await bridge.dispatch_inbound(inbound)
    # Discord expects an Interaction response (type=4 = channel message).
    return {
        "type": 4,
        "data": {"content": result.get("reply") or "✓"},
    }


@router.post("/chatops/webhook/teams")
async def teams_webhook(request: Request) -> dict[str, Any]:
    raw = await request.body()
    import json as _json
    try:
        payload = _json.loads(raw.decode("utf-8") or "{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"bad json: {exc}")

    mod = bridge.get_adapter("teams")
    try:
        mod.verify(dict(request.headers), raw)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    inbound = mod.parse_inbound(payload)
    result = await bridge.dispatch_inbound(inbound)
    # Teams expects {"type": "message", "text": "..."}.
    return {"type": "message", "text": result.get("reply") or "✓"}


@router.post("/chatops/webhook/line")
async def line_webhook(request: Request) -> dict[str, Any]:
    raw = await request.body()
    import json as _json
    try:
        payload = _json.loads(raw.decode("utf-8") or "{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"bad json: {exc}")

    mod = bridge.get_adapter("line")
    try:
        mod.verify(dict(request.headers), raw)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    inbound = mod.parse_inbound(payload)
    await bridge.dispatch_inbound(inbound)
    # Line webhook is fire-and-forget; 200 OK is enough.
    return {"ok": True}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dashboard surfaces
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/chatops/mirror")
async def chatops_mirror(limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    return {
        "items": bridge.mirror_snapshot(limit=limit),
        "status": bridge.adapter_status(),
    }


@router.get("/chatops/status")
async def chatops_status() -> dict[str, Any]:
    return {
        "adapters": bridge.adapter_status(),
        "buttons": bridge.list_buttons(),
        "commands": bridge.list_commands(),
        "pending_hints": agent_hints.snapshot(),
    }


class InjectRequest(BaseModel):
    agent_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    author: str = Field(default="dashboard")


@router.post("/chatops/inject")
async def chatops_inject(
    req: InjectRequest,
    _user=Depends(_au.require_operator),
) -> dict[str, Any]:
    try:
        hint = agent_hints.inject(
            req.agent_id,
            req.text,
            author=req.author or getattr(_user, "email", "dashboard"),
            channel="dashboard",
        )
    except agent_hints.HintRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True, "hint": hint.to_dict()}


class SendRequest(BaseModel):
    channel: str = Field(..., description="discord | teams | line | *")
    title: str = Field(default="OmniSight")
    body: str
    buttons: list[dict] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)


@router.post("/chatops/send")
async def chatops_send(
    req: SendRequest,
    _user=Depends(_au.require_operator),
) -> dict[str, Any]:
    btns = [
        bridge.Button(
            id=str(b.get("id") or ""),
            label=str(b.get("label") or b.get("id") or ""),
            style=str(b.get("style") or "primary"),
            value=str(b.get("value") or ""),
        )
        for b in (req.buttons or [])
        if b.get("id")
    ]
    out = await bridge.send_interactive(
        req.channel, req.body, title=req.title, buttons=btns, meta=req.meta or {},
    )
    return {"ok": True, "message": out.to_dict()}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PEP decision alias (ChatOps approve/reject button round-trip)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class PepDecisionRequest(BaseModel):
    decision: str = Field(..., description="approve | reject")


@router.post("/pep/decision/{pep_id}")
async def pep_decision(
    pep_id: str,
    req: PepDecisionRequest,
    _user=Depends(_au.require_operator),
) -> dict[str, Any]:
    """Resolve a PEP held id directly (alias for the R1 button round-trip).

    Looks up the PEP HELD registry, maps to its ``decision_id``, then
    delegates to the decision engine just like the /decisions router
    approve/reject does. This exists so ChatOps buttons can POST to a
    stable ``/pep/decision/{pep_id}`` URL without knowing the DE id
    (which only the backend has).
    """
    from backend import pep_gateway as _pep
    from backend import decision_engine as de

    decision = (req.decision or "").lower().strip()
    if decision not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="decision must be 'approve' or 'reject'")

    # Find the PEP dec in the held snapshot (safer than mutating registry here).
    held = {d["id"]: d for d in _pep.held_snapshot()}
    entry = held.get(pep_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"pep id {pep_id!r} not held")
    de_id = entry.get("decision_id")
    if not de_id:
        raise HTTPException(status_code=409, detail="no decision_engine id linked to pep held entry")

    existing = de.get(de_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="decision not found in engine")
    if existing.status != de.DecisionStatus.pending:
        raise HTTPException(status_code=409, detail=f"not pending (status={existing.status.value})")

    if decision == "approve":
        out = de.resolve(de_id, "approve", resolver=f"chatops:{getattr(_user, 'email', 'operator')}",
                         status=de.DecisionStatus.approved)
    else:
        out = de.resolve(de_id, "__rejected__", resolver=f"chatops:{getattr(_user, 'email', 'operator')}",
                         status=de.DecisionStatus.rejected)
    return {"ok": True, "decision": out.to_dict() if out else {}, "pep_id": pep_id}
