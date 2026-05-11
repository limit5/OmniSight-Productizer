"""WP.7.8 + OP-884 -- operator feature flag registry UI API.

GET   /feature-flags                 -- all authenticated roles inspect
PATCH /feature-flags/{flag_name}     -- admin+ toggles state / rollout

Module-global state audit
-------------------------
No new mutable module-global state is introduced. Reads and writes go
through PG via ``db_pool.get_pool()``. Writer cache coherence uses the
existing WP.7.4 ``publish_feature_flags_invalidate()`` Redis fan-out,
so cross-worker readers reload from the same durable ``feature_flags``
table after a toggle.

Read-after-write timing audit
-----------------------------
The toggle path updates one row inside a DB transaction, emits the N10
``audit_log`` row with ``entity_kind="feature_flag"``, then publishes
cache invalidation after commit. The response is built from the updated
row returned by the same transaction, so the caller sees its write.

OP-884 extension
----------------
The PATCH payload now accepts ``rollout_pct`` and ``allowed_tenants``
alongside ``state``. All three fields are independently optional; the
caller may flip ``state`` while leaving rollout alone, dial up
``rollout_pct`` without changing ``state``, or set an explicit
allow-list. The audit row captures the full before / after row so the
N10 chain reconstructs the operator's intent on either side of the
change.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from backend import auth
from backend import feature_flags as _flags
from backend.db_pool import get_pool


router = APIRouter(prefix="/feature-flags", tags=["feature-flags"])


FeatureFlagStateLiteral = Literal["disabled", "enabled"]


class PatchFeatureFlagRequest(BaseModel):
    state: FeatureFlagStateLiteral | None = Field(
        default=None,
        description="New global state for the feature flag.",
    )
    rollout_pct: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Per-tenant rollout percentage (OP-884).",
    )
    allowed_tenants: list[str] | None = Field(
        default=None,
        description=(
            "Explicit tenant allow-list (OP-884). When non-empty, only "
            "listed tenants resolve enabled; the rollout_pct gate is "
            "short-circuited."
        ),
    )

    @field_validator("allowed_tenants")
    @classmethod
    def _strip_tenant_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [t.strip() for t in value if str(t).strip()]


_LIST_FLAGS_SQL = """
SELECT
    flag_name,
    tier,
    state,
    expires_at,
    owner,
    rollout_pct,
    allowed_tenants,
    created_at,
    updated_at
FROM feature_flags
ORDER BY
    CASE tier
        WHEN 'debug' THEN 0
        WHEN 'dogfood' THEN 1
        WHEN 'preview' THEN 2
        WHEN 'release' THEN 3
        WHEN 'runtime' THEN 4
        ELSE 99
    END,
    flag_name ASC
"""


_GET_FLAG_FOR_UPDATE_SQL = """
SELECT
    flag_name,
    tier,
    state,
    expires_at,
    owner,
    rollout_pct,
    allowed_tenants,
    created_at,
    updated_at
FROM feature_flags
WHERE flag_name = $1
FOR UPDATE
"""


_UPDATE_FLAG_SQL = """
UPDATE feature_flags
SET state = $2,
    rollout_pct = $3,
    allowed_tenants = $4::jsonb,
    updated_at = NOW()
WHERE flag_name = $1
RETURNING flag_name, tier, state, expires_at, owner,
          rollout_pct, allowed_tenants, created_at, updated_at
"""


def _decode_allowed(raw: Any) -> list[str]:
    """Coerce ``allowed_tenants`` from JSONB / text into a list[str].

    asyncpg returns JSONB as ``str`` (unless ``conn.set_type_codec`` is
    used) and the SQLite path returns plain TEXT; both shapes must
    round-trip through the operator UI without surprise quoting.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    return []


def _row_to_payload(row: Any) -> dict[str, Any]:
    # asyncpg.Record and plain dict both round-trip through ``dict(...)``,
    # so the helper stays compatible with the in-memory test fakes.
    r = dict(row)
    rollout_pct = r.get("rollout_pct", 100)
    updated_at = r.get("updated_at")
    return {
        "flag_name": r["flag_name"],
        "tier": r["tier"],
        "state": r["state"],
        "expires_at": (
            None if r.get("expires_at") is None else str(r["expires_at"])
        ),
        "owner": r.get("owner") or "",
        "rollout_pct": 100 if rollout_pct is None else int(rollout_pct),
        "allowed_tenants": _decode_allowed(r.get("allowed_tenants", "[]")),
        "created_at": str(r.get("created_at") or ""),
        "updated_at": None if updated_at is None else str(updated_at),
    }


@router.get("")
async def list_feature_flags(
    _request: Request,
    actor: auth.User = Depends(auth.require_viewer),
) -> JSONResponse:
    """Return the feature flag registry for operator inspection.

    Viewer / operator roles get the same read-only payload as admins.
    The ``can_toggle`` bit lets the UI render disabled controls without
    duplicating the backend's role ranking.
    """
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(_LIST_FLAGS_SQL)
    return JSONResponse(
        status_code=200,
        content={
            "feature_flags": [_row_to_payload(row) for row in rows],
            "can_toggle": auth.role_at_least(actor.role, "admin"),
        },
    )


@router.patch("/{flag_name:path}")
async def patch_feature_flag(
    flag_name: str,
    req: PatchFeatureFlagRequest,
    _request: Request,
    actor: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    """Toggle one feature flag's state / rollout and audit the mutation.

    OP-884: ``state``, ``rollout_pct``, and ``allowed_tenants`` are all
    optional; absent fields preserve the existing row value. Any change
    bumps ``updated_at`` and writes the N10 audit row.
    """
    if (
        req.state is None
        and req.rollout_pct is None
        and req.allowed_tenants is None
    ):
        raise HTTPException(
            status_code=400,
            detail="at least one of state / rollout_pct / allowed_tenants is required",
        )

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            before_row = await conn.fetchrow(_GET_FLAG_FOR_UPDATE_SQL, flag_name)
            if before_row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"feature flag not found: {flag_name}",
                )

            before_payload = _row_to_payload(before_row)
            new_state = (
                _flags.FeatureFlagState.parse(req.state).value
                if req.state is not None
                else before_payload["state"]
            )
            new_rollout = (
                int(req.rollout_pct)
                if req.rollout_pct is not None
                else before_payload["rollout_pct"]
            )
            new_allowed = (
                list(req.allowed_tenants)
                if req.allowed_tenants is not None
                else before_payload["allowed_tenants"]
            )

            unchanged = (
                new_state == before_payload["state"]
                and new_rollout == before_payload["rollout_pct"]
                and new_allowed == before_payload["allowed_tenants"]
            )
            if unchanged:
                updated_row = before_row
                after_payload = before_payload
            else:
                updated_row = await conn.fetchrow(
                    _UPDATE_FLAG_SQL,
                    flag_name,
                    new_state,
                    new_rollout,
                    json.dumps(new_allowed),
                )
                after_payload = _row_to_payload(updated_row)
                try:
                    from backend import audit as _audit
                    await _audit.log(
                        action="feature_flag.toggled",
                        entity_kind="feature_flag",
                        entity_id=flag_name,
                        before=before_payload,
                        after=after_payload,
                        actor=actor.email,
                        conn=conn,
                    )
                except Exception:
                    # ``audit.log`` already swallows internally; this is
                    # a defensive belt so the toggle path keeps the same
                    # best-effort audit posture as existing admin routers.
                    pass

    _flags.publish_feature_flags_invalidate(
        flag_name=flag_name,
        origin_worker="operator-ui",
    )
    return JSONResponse(
        status_code=200,
        content={"feature_flag": after_payload},
    )
