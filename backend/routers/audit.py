"""Phase 53 — audit query API.

GET  /audit?since=&actor=&entity_kind=&limit=
GET  /audit/verify    walk the chain, return {ok, first_bad_id?}

The mutator endpoints (decision_engine.set_mode/resolve/undo) write
to the audit log automatically; this router is read-only.
"""

from __future__ import annotations

import os
from fastapi import APIRouter, Depends, Header, HTTPException

from backend import audit
from backend import auth as _au
from backend.routers import _pagination as _pg

async def _resolve_session_hint(user_id: str, token_hint: str) -> str | None:
    """Resolve a masked token_hint back to the full session token."""
    sessions = await _au.list_sessions(user_id)
    for s in sessions:
        if s["token_hint"] == token_hint:
            return s["token"]
    return None

router = APIRouter(prefix="/audit", tags=["audit"])


def _require_audit_token(authorization: str | None = Header(default=None)) -> None:
    """Audit reads can leak operator behaviour, so when bearer auth is
    configured (per-key api_keys table or legacy env) we require it.
    The actual validation happens in current_user(); this gate only
    checks that a bearer is present when the legacy env is set (for
    backwards compat). Per-key callers are validated by current_user."""
    expected = os.environ.get("OMNISIGHT_DECISION_BEARER", "").strip()
    if not expected:
        return
    presented = (authorization or "")
    if presented.startswith("Bearer "):
        presented = presented[len("Bearer "):]
    if not presented:
        raise HTTPException(status_code=401, detail="Bearer token required for audit access")


@router.get("")
async def list_audit(
    since: float | None = None,
    actor: str | None = None,
    entity_kind: str | None = None,
    session_id: str | None = None,
    limit: int = _pg.Limit(default=200, max_cap=500),
    _auth: None = Depends(_require_audit_token),
    user: _au.User = Depends(_au.current_user),
) -> dict:
    if not _au.role_at_least(user.role, "admin"):
        actor = user.email
    resolved_sid = session_id
    if session_id and len(session_id) < 20:
        full = await _resolve_session_hint(user.id, session_id)
        if full:
            resolved_sid = full
    rows = await audit.query(
        since=since, actor=actor, entity_kind=entity_kind,
        session_id=resolved_sid, limit=limit,
    )
    return {"items": rows, "count": len(rows), "filtered_to_self": user.role != "admin"}


@router.get("/verify")
async def verify_chain(_auth: None = Depends(_require_audit_token),
                       _user: _au.User = Depends(_au.require_admin)) -> dict:
    ok, bad = await audit.verify_chain()
    return {"ok": ok, "first_bad_id": bad}
