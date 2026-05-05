"""WP.7.8 -- operator feature flag registry UI API.

GET   /feature-flags                 -- all authenticated roles inspect
PATCH /feature-flags/{flag_name}     -- admin+ toggles global state

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
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth
from backend import feature_flags as _flags
from backend.db_pool import get_pool


router = APIRouter(prefix="/feature-flags", tags=["feature-flags"])


FeatureFlagStateLiteral = Literal["disabled", "enabled"]


class PatchFeatureFlagRequest(BaseModel):
    state: FeatureFlagStateLiteral = Field(
        description="New global state for the feature flag.",
    )


_LIST_FLAGS_SQL = """
SELECT
    flag_name,
    tier,
    state,
    expires_at,
    owner,
    created_at
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
    created_at
FROM feature_flags
WHERE flag_name = $1
FOR UPDATE
"""


_UPDATE_FLAG_SQL = """
UPDATE feature_flags
SET state = $2
WHERE flag_name = $1
RETURNING flag_name, tier, state, expires_at, owner, created_at
"""


def _row_to_payload(row: Any) -> dict[str, Any]:
    return {
        "flag_name": row["flag_name"],
        "tier": row["tier"],
        "state": row["state"],
        "expires_at": (
            None if row["expires_at"] is None else str(row["expires_at"])
        ),
        "owner": row["owner"] or "",
        "created_at": str(row["created_at"]),
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
    """Toggle one feature flag's global state and audit the mutation."""
    new_state = _flags.FeatureFlagState.parse(req.state).value

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            before_row = await conn.fetchrow(_GET_FLAG_FOR_UPDATE_SQL, flag_name)
            if before_row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"feature flag not found: {flag_name}",
                )

            if before_row["state"] == new_state:
                updated_row = before_row
            else:
                updated_row = await conn.fetchrow(
                    _UPDATE_FLAG_SQL,
                    flag_name,
                    new_state,
                )
                try:
                    from backend import audit as _audit
                    await _audit.log(
                        action="feature_flag.toggled",
                        entity_kind="feature_flag",
                        entity_id=flag_name,
                        before=_row_to_payload(before_row),
                        after=_row_to_payload(updated_row),
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
        content={"feature_flag": _row_to_payload(updated_row)},
    )
