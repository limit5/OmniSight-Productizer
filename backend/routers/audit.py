"""Phase 53 / I8 — audit query API with per-tenant hash chain.

GET  /audit?since=&actor=&entity_kind=&limit=
GET  /audit/verify?tenant_id=     per-tenant chain verify (admin only)
GET  /audit/verify-all            all tenants at once (admin only)

The mutator endpoints (decision_engine.set_mode/resolve/undo) write
to the audit log automatically; this router is read-only.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from backend import audit
from backend import auth as _au
from backend.db_context import current_tenant_id
from backend.routers import _pagination as _pg

async def _resolve_session_hint(user_id: str, token_hint: str) -> str | None:
    """Resolve a masked token_hint back to the row's lookup hash.

    FX.11.2: ``sessions.token`` is now KS-envelope JSON; the addressable
    identifier is ``token_lookup_index`` (sha256 hex of the cookie
    plaintext). Returning that hash keeps the audit-log session filter
    on the same surface as the listing API. Note that pre-FX.11.2
    ``audit_log.session_id`` rows hold plaintext tokens and will not
    match this hash — that lookup degradation is a known follow-up
    (see HANDOFF FX.11.2 known limitations)."""
    sessions = await _au.list_sessions(user_id)
    for s in sessions:
        if s["token_hint"] == token_hint:
            return s.get("token_lookup_index")
    return None

router = APIRouter(prefix="/audit", tags=["audit"])

_STAGING_AUDIT_VERIFY_TOKEN_FILE_ENV = "OMNISIGHT_STAGING_AUDIT_VERIFY_TOKEN_FILE"
_STAGING_AUDIT_VERIFY_TOKEN_FILE = Path(
    "~/.config/omnisight/staging-audit-verify-token"
).expanduser()


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


def _staging_audit_verify_token() -> str:
    """Read the staging-only audit verify token, if provisioned."""
    raw_path = os.environ.get(_STAGING_AUDIT_VERIFY_TOKEN_FILE_ENV, "").strip()
    path = Path(raw_path).expanduser() if raw_path else _STAGING_AUDIT_VERIFY_TOKEN_FILE
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def _require_audit_verify_access(
    request: Request,
    authorization: str | None = Header(default=None),
) -> _au.User:
    """Allow the staging audit-verify token or the existing admin auth path."""
    expected = _staging_audit_verify_token()
    presented = authorization or ""
    if presented.lower().startswith("bearer "):
        presented = presented[len("bearer "):].strip()

    if expected:
        if presented and secrets.compare_digest(presented, expected):
            return _au.User(
                id="staging-audit-verify",
                email="staging-audit-verify@local",
                name="staging audit verify",
                role="admin",
                enabled=True,
                tenant_id=current_tenant_id() or "t-default",
            )
        if _au.auth_mode() == "open":
            raise HTTPException(status_code=401, detail="Invalid audit verify token")
    else:
        _require_audit_token(authorization)

    user = await _au.current_user(request)
    if not _au.role_at_least(user.role, "admin"):
        raise HTTPException(
            status_code=403,
            detail=f"Requires role=admin or higher (you are {user.role})",
        )
    return user


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
    tid = current_tenant_id() or user.tenant_id
    return {"items": rows, "count": len(rows), "filtered_to_self": user.role != "admin",
            "tenant_id": tid}


@router.get("/verify")
async def verify_chain(
    tenant_id: str | None = Query(default=None, description="Tenant to verify (admin only, defaults to current tenant)"),
    user: _au.User = Depends(_require_audit_verify_access),
) -> dict:
    tid = tenant_id or current_tenant_id() or user.tenant_id
    ok, bad = await audit.verify_chain(tenant_id=tid)
    return {"ok": ok, "first_bad_id": bad, "tenant_id": tid}


@router.get("/verify-all")
async def verify_all_chains(
    _auth: None = Depends(_require_audit_token),
    _user: _au.User = Depends(_au.require_admin),
) -> dict:
    results = await audit.verify_all_chains()
    tenants = []
    all_ok = True
    for tid, (ok, bad) in sorted(results.items()):
        tenants.append({"tenant_id": tid, "ok": ok, "first_bad_id": bad})
        if not ok:
            all_ok = False
    return {"ok": all_ok, "tenants": tenants}
