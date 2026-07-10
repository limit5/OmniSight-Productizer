"""OP-2567 U4-B — publication-ledger ACTOR BOUNDARY.

Boundary contract: this is the ONLY sanctioned writer of the
publication ledger (migration 0258); U4-C composes the atomic
publish/revoke procedures ON this boundary; producers (U4-I) never
import it. Ships DORMANT until U4-C/J2 — no caller in this change.

The writer is deliberately THIN: no reduction, no snapshot write, no
head bump, no transition-legality enforcement. The 0259 DB trigger
backstops approval linkage; full state-machine enforcement is U4-C's
advisory-locked procedure calling ``is_legal_transition`` under the
lock (freeze G3 TOCTOU defense).

``conn`` is a parameter everywhere (pure seam) — this module never
imports backend.db_pool nor initialises a pool.
"""
from __future__ import annotations

from typing import Any

from backend.learned_item_publication import PUBLICATION_STATES


async def acquire_publish_lock(conn: Any, scope_key: str) -> None:
    """Take the per-scope xact-scoped advisory lock (freeze G3 TOCTOU
    defense). MUST be called inside an open transaction — the lock is
    released at commit/rollback, never explicitly.

    No sqlite equivalent: sqlite tests exercise the writer without the
    lock (single-writer test DB).
    """
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))", scope_key
    )


async def insert_publication_event(
    conn: Any,
    *,
    event_id: str,
    version_id: str,
    state: str,
    approval_id: str | None = None,
    published_at: Any = None,
    revoked_at: Any = None,
    revoked_by: str | None = None,
    revoke_reason: str | None = None,
) -> None:
    """Thin guarded INSERT of one publication event (``event_id`` maps
    to the table's ``id`` PK column). ``event_seq`` is not in the
    column list — PG identity assigns it (sqlite leaves it NULL)."""
    if state not in PUBLICATION_STATES:
        raise ValueError(
            f"insert_publication_event: state {state!r} is not one of "
            f"{PUBLICATION_STATES}"
        )
    await conn.execute(
        "INSERT INTO memory_publications "
        "(id, version_id, approval_id, state, published_at, revoked_at, "
        " revoked_by, revoke_reason) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        event_id,
        version_id,
        approval_id,
        state,
        published_at,
        revoked_at,
        revoked_by,
        revoke_reason,
    )
