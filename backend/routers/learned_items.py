"""β-3c (leg-2) — learned-items block endpoint for the JIRA runner.

The runner is a separate process with NO DB pool (HTTP-only context
reads); this thin wrapper exposes the loader's PURE-SYNC cache read.
Dual-flag gated for free: read-OFF ⇒ ("", "kill_switch_off"). Always
200 ``{block, result}`` — the runner idiom collapses any failure to
no-injection. Auth mirrors /api/v1/project-state (the runner's bearer).

HONEST CONTAINMENT (β-3c audit C2): the runner is OUTSIDE the memory
kernel — no provenance snapshot, no auto-auth downgrade. Its real
guards: the human approval gate (only human-approved published cards
exist), the u4r2 descriptive datamark render (injected VERBATIM —
never re-fenced), CapabilityNotPermitted at tool-dispatch, the
runner_sandbox jail + cred scrub, and the human +2 merge gate.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from backend import auth as _au

router = APIRouter(tags=["learned-items"])


@router.get("/learned-items/block")
async def learned_items_block(
    tenant: str | None = None,
    context: str = "",
    user: _au.User = Depends(_au.current_user),
) -> dict[str, Any]:
    from backend.learned_item_loader import get_learned_items_block

    block, result = get_learned_items_block(
        tenant_id=(tenant or None), context=context or "",
    )
    return {"block": block, "result": result}
