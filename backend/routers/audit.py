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

router = APIRouter(prefix="/audit", tags=["audit"])


def _require_audit_token(authorization: str | None = Header(default=None)) -> None:
    """Audit reads can leak operator behaviour, so when the
    OMNISIGHT_DECISION_BEARER env is set we require it here too.
    Reuses the same env var to avoid a second secret to manage."""
    expected = os.environ.get("OMNISIGHT_DECISION_BEARER", "").strip()
    if not expected:
        return
    presented = (authorization or "")
    if presented.startswith("Bearer "):
        presented = presented[len("Bearer "):]
    if presented != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing audit bearer token")


@router.get("")
async def list_audit(
    since: float | None = None,
    actor: str | None = None,
    entity_kind: str | None = None,
    limit: int = 200,
    _auth: None = Depends(_require_audit_token),
    user: _au.User = Depends(_au.current_user),
) -> dict:
    # Phase 54 RBAC: non-admin callers can only read entries they
    # themselves authored. Admins see everything; the optional
    # `actor` query param narrows further.
    if not _au.role_at_least(user.role, "admin"):
        actor = user.email  # force-narrow to self
    rows = await audit.query(since=since, actor=actor, entity_kind=entity_kind, limit=limit)
    return {"items": rows, "count": len(rows), "filtered_to_self": user.role != "admin"}


@router.get("/verify")
async def verify_chain(_auth: None = Depends(_require_audit_token),
                       _user: _au.User = Depends(_au.require_admin)) -> dict:
    ok, bad = await audit.verify_chain()
    return {"ok": ok, "first_bad_id": bad}
