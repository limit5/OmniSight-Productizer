"""Q.6 #300 (2026-04-24, checkboxes 1-2) — per-user draft composer slots.

Backs the 500 ms debounce write from the INVOKE command bar
(``components/omnisight/invoke-core.tsx``) and the workspace chat
composer (``components/omnisight/workspace-chat.tsx``). Two slot
keys land first (``invoke:main`` / ``chat:main``); future per-thread
chat (``chat:<thread_id>``) reuses the same endpoint with a different
``slot_key`` path component.

Routes
──────
PUT /user/drafts/{slot_key}
    Upsert the draft text for the current user + slot. Returns
    ``{slot_key, content, updated_at}`` so the client can echo the
    server-side timestamp into local storage for the Q.6 conflict
    check on restore (checkbox 4).
GET /user/drafts/{slot_key}
    Restore-on-new-device read (Q.6 checkbox 2). Returns the current
    server-stored ``{slot_key, content, updated_at}`` or an empty
    draft shape (``content=""``, ``updated_at=None``) when the row
    is absent so the frontend can treat the "never typed here"
    case branch-free. Called once on mount by both composers.

The DELETE counterpart arrives with Q.6 checkbox 3 (retention GC is
currently opportunistic on every PUT, so explicit delete is not yet
required for correctness).

Conflict policy (Q.6 spec, last-writer-wins)
────────────────────────────────────────────
Two devices typing into the same slot at the same time produces a
trivial INSERT … ON CONFLICT DO UPDATE race; whoever's UPSERT lands
second wins. Drafts are ephemeral (24 h retention, dropped on
submit) so we deliberately skip the optimistic-lock dance the
``workflow_runs`` family uses. The conflict gets caught instead at
*restore* time when the new device compares the server-side
``updated_at`` against its local-storage cache.

Slot-key validation
───────────────────
Accept ``[a-z0-9_-]+:[a-z0-9_-]+`` only — that covers ``invoke:main``,
``chat:main``, and the future ``chat:<thread_id>`` form (thread ids
are uuid-like). Anything else gets a 400; the validator runs in the
handler to keep the FastAPI path-parameter parsing dumb (nothing
about ``slot_key`` should be inferred from the URL beyond "string").
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth
from backend import db as db_helpers

logger = logging.getLogger(__name__)

router = APIRouter(tags=["drafts"])


# Slot-key shape pin: ``namespace:scope`` where each side is a small
# kebab/snake-case identifier. Leaves room for ``chat:<uuid>`` (uuids
# fit ``[a-z0-9-]+``) without taking new shapes that complicate
# server-side parsing.
_SLOT_KEY_RE = re.compile(r"^[a-z0-9_-]{1,64}:[a-z0-9_-]{1,128}$")

# Keep parity with frontend Q.6 limits (`MAX_DRAFT_BYTES` in
# ``lib/api.ts``). 64 KiB is dramatically more than any realistic
# draft and small enough that the row stays cheap to upsert.
MAX_DRAFT_BYTES = 64 * 1024


class DraftBody(BaseModel):
    content: str = Field(default="", max_length=MAX_DRAFT_BYTES)


def _validate_slot_key(slot_key: str) -> None:
    if not _SLOT_KEY_RE.match(slot_key):
        raise HTTPException(
            status_code=400,
            detail=(
                "slot_key must match '<namespace>:<scope>' with "
                "[a-z0-9_-] characters on each side; got "
                f"{slot_key!r}"
            ),
        )


@router.put("/user/drafts/{slot_key}")
async def put_user_draft(
    slot_key: str,
    body: DraftBody,
    user: auth.User = Depends(auth.current_user),
) -> dict:
    """Upsert ``content`` into the (user_id, slot_key) row. Always
    returns 200 with the server-committed ``updated_at`` timestamp.

    Best-effort opportunistic GC: after the upsert lands, sweep rows
    older than 24 h. Failures in the GC do NOT fail the PUT — the
    draft itself is the only correctness-critical write here, and the
    sweep is purely a table-bound housekeeping nicety.
    """
    _validate_slot_key(slot_key)
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        committed_at = await db_helpers.upsert_user_draft(
            conn, user.id, slot_key, body.content,
        )
        # Opportunistic GC — never let it propagate.
        try:
            await db_helpers.prune_user_drafts(conn)
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "prune_user_drafts opportunistic sweep failed: %s", exc,
            )
    return {
        "slot_key": slot_key,
        "content": body.content,
        "updated_at": committed_at,
    }


@router.get("/user/drafts/{slot_key}")
async def get_user_draft(
    slot_key: str,
    user: auth.User = Depends(auth.current_user),
) -> dict:
    """Q.6 #300 checkbox 2 — restore on new device.

    Fetch the server-stored draft for ``(current_user, slot_key)``.
    Returns ``{slot_key, content, updated_at}`` when the row exists,
    or a shaped empty response (``content=""``, ``updated_at=None``)
    when no row has ever been written so the caller can skip a
    "is it null?" branch in JS.

    Returning 200 + empty over 404 is deliberate: the frontend calls
    this on every page mount and a 404 on a fresh slot would just
    pollute the DevTools network tab. The empty-shape contract also
    keeps the TS type a single ``DraftResponse`` on the happy path
    instead of forcing a ``DraftResponse | null`` union.
    """
    _validate_slot_key(slot_key)
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await db_helpers.get_user_draft(conn, user.id, slot_key)
    if row is None:
        return {"slot_key": slot_key, "content": "", "updated_at": None}
    return row
