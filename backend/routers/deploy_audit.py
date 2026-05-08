"""OP-779 D18 -- ``deploy_audit`` query + CSV export router.

Compliance surface for the change-management audit log. Two
endpoints:

* ``GET /admin/deploy-audit`` -- JSON list of audit rows in the
  ``[since, until)`` window. Defaults to the last 1y per the ticket
  spec. Optional ``kind`` filter narrows to one of the four event
  classes.
* ``GET /admin/deploy-audit.csv`` -- the same window as a CSV
  attachment for compliance auditors.
* ``GET /admin/deploy-audit/verify`` -- re-walks the hash chain and
  reports tampering. Read-only.

All three require admin auth (same gate as the release-approval
router) so the audit surface is not exposed to non-operators.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from backend import auth, deploy_audit


router = APIRouter(prefix="/admin/deploy-audit", tags=["deploy-audit"])


def _parse_iso(value: str | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be ISO-8601 timestamp",
        ) from exc


def _validated_kind(kind: str | None) -> str | None:
    if kind is None:
        return None
    if kind not in deploy_audit.ALLOWED_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {sorted(deploy_audit.ALLOWED_KINDS)}",
        )
    return kind


@router.get("")
async def list_deploy_audit(
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=10_000),
    _user: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    rows = deploy_audit.query(
        since=_parse_iso(since, field="since"),
        until=_parse_iso(until, field="until"),
        kind=_validated_kind(kind),
        limit=limit,
    )
    return JSONResponse({"rows": rows, "count": len(rows)})


@router.get(".csv")
async def export_deploy_audit_csv(
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    _user: auth.User = Depends(auth.require_admin),
) -> Response:
    csv_text = deploy_audit.export_csv(
        since=_parse_iso(since, field="since"),
        until=_parse_iso(until, field="until"),
        kind=_validated_kind(kind),
    )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="deploy_audit.csv"',
        },
    )


@router.get("/verify")
async def verify_deploy_audit_chain(
    _user: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    result: dict[str, Any] = deploy_audit.verify_chain()
    status = 200 if result.get("ok") else 409
    return JSONResponse(result, status_code=status)
