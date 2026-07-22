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


_TICKET_RE = __import__("re").compile(r"^[A-Z][A-Z0-9_]*-\d+$")


@router.get("/learned-items/block")
async def learned_items_block(
    tenant: str | None = None,
    context: str = "",
    ticket: str | None = None,
    user: _au.User = Depends(_au.current_user),
) -> dict[str, Any]:
    from backend.learned_item_loader import get_learned_items_block

    served: list = []
    block, result = get_learned_items_block(
        tenant_id=(tenant or None), context=context or "", served_sink=served,
    )
    # β-4 (audit C1/F2): SERVER-observed citation — best-effort, NEVER fails
    # the 200 (a DB blip must not cost the runner its injection). Hit =
    # server-observed (cited_by = authenticated principal); ticket =
    # runner-supplied semi-trusted (shape-validated); OUTCOME stays fully
    # trusted (the join source is the Gerrit-verified ledger).
    if result == "non_empty" and served and ticket and _TICKET_RE.match(ticket):
        try:
            import uuid as _uuid

            from backend.db_pool import get_pool

            async with get_pool().acquire() as conn:
                for vid in served:
                    await conn.execute(
                        "INSERT INTO learned_item_citations "
                        "(id, version_id, ticket_key, cited_by) "
                        "VALUES ($1, $2, $3, $4) "
                        "ON CONFLICT (version_id, ticket_key) DO NOTHING",
                        str(_uuid.uuid4()), vid, ticket, str(user.id),
                    )
        except Exception:  # noqa: BLE001 — loud metric, silent to the caller
            from backend import metrics

            metrics.memory_failclosed_total.labels(
                reason="citation_write").inc()
    return {"block": block, "result": result}
