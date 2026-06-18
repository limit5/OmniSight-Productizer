"""OP-2239 BI0b -- transcript retention helpers (window + sweep).

The BI0 transcript tables (alembic 0249) hold sensitive speech-derived
text. BI0b adds a deadline-based retention sweep: rows whose
``meetings.retention_until`` is in the past are purged together with
their child ``transcript_segments``. The deadline is stamped on the
meeting row at open time using the configured window (env knob below),
so each row carries its own expiry independent of any later config
rotation -- consistent with the BI0 "no audio, TEXT-only, predictable
lifecycle" contract.

Tuning knob
-----------
``OMNISIGHT_BI0_TRANSCRIPT_RETENTION_S`` (default ``7776000`` -- 90 days).
The retention window applied to a meeting at create time. A NULL
``retention_until`` row is treated as "no deadline" and is never swept.

Cross-tenant invariant
----------------------
:func:`sweep_expired_meetings` accepts an optional ``tenant_id`` to
constrain the sweep. With ``tenant_id`` set, the sweep deletes only
meetings whose ``tenant_id`` matches; the unconstrained call sweeps
every tenant (cron-style global sweep). The acceptance test exercises
both shapes -- per-tenant sweep does not bleed across tenant
boundaries.

Module-global audit (SOP Step 1, 2026-04-21 rule)
--------------------------------------------------
This module has NO mutable module-level state. ``RETENTION_S`` is an
immutable env-resolved constant identical in every worker. All
mutable state lives in PG and is serialised by the underlying
``conn.transaction()`` from the caller.

Order-of-deletion contract
--------------------------
Segments first, meetings second -- mirrors the ordering enforced by
``backend/routers/privacy.py::_ERASURE_TENANT_DELETE_STATEMENTS``.
There is no FK between the tables (BI0 chose composite-key dedup over
an FK), but interleaving the delete in parent->child order means a
concurrent reader walking the meeting envelope sees a consistent
intermediate state -- a meeting whose segments are gone but the row
still exists is harmless (segment_count = 0 is a valid envelope); a
segment whose meeting is gone is a dangling row our reads would skip
silently.
"""
from __future__ import annotations

import logging
import os
import time as _time
from typing import Optional

logger = logging.getLogger(__name__)


# 90 days -- the BI0 design doc references a 30-90 day window for
# meeting transcripts depending on tenant plan; we default to the upper
# bound and let the operator tighten via the env knob. Choosing 90 days
# (vs 30) at the default avoids surprising a tenant whose audit window
# is still being negotiated; the sweep is keyed off the stamp written
# at create time so a later default change does not retroactively wipe
# rows that already booked the longer window.
_DEFAULT_RETENTION_S = 90 * 24 * 3600


def _retention_window_s() -> float:
    """Return the configured retention window in seconds.

    Evaluated per-call (not at import time) so test fixtures can
    monkeypatch the env between tests without restarting the process.
    """
    raw = os.environ.get("OMNISIGHT_BI0_TRANSCRIPT_RETENTION_S")
    if raw is None or raw.strip() == "":
        return float(_DEFAULT_RETENTION_S)
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "OMNISIGHT_BI0_TRANSCRIPT_RETENTION_S=%r is not numeric -- "
            "falling back to default %ds",
            raw, _DEFAULT_RETENTION_S,
        )
        return float(_DEFAULT_RETENTION_S)
    if value <= 0:
        # A non-positive window would expire every freshly-written row
        # the moment it is created, which is almost certainly a config
        # mistake. Treat as "no deadline" rather than instant wipe.
        logger.warning(
            "OMNISIGHT_BI0_TRANSCRIPT_RETENTION_S=%s is non-positive -- "
            "treating as 'no deadline' (NULL retention_until)",
            value,
        )
        return 0.0
    return value


def compute_retention_until(created_at: float) -> Optional[float]:
    """Return the ``retention_until`` epoch for a meeting opened at
    ``created_at``, or ``None`` when the configured window is zero
    ("no deadline" -- see :func:`_retention_window_s`).

    Pure function; no PG, no env-cache. The router calls this at
    meeting open / auto-create and stamps the result onto the row.
    """
    window = _retention_window_s()
    if window <= 0:
        return None
    return float(created_at) + window


async def sweep_expired_meetings(
    conn,
    *,
    tenant_id: Optional[str] = None,
    now: Optional[float] = None,
) -> dict[str, int]:
    """Delete every meeting whose ``retention_until`` is strictly in the
    past, plus all of its transcript segments. Returns a count summary.

    Parameters
    ----------
    conn : asyncpg.Connection
        The caller's connection. Must run inside the caller's
        ``conn.transaction()`` if atomic semantics across the two
        DELETEs are required by the test (the test fixture does this);
        unwrapped, each DELETE is its own implicit transaction which
        still preserves per-row consistency.
    tenant_id : str or None
        If set, restrict the sweep to a single tenant (the ticket's AC
        invariant: "deletes only that tenant's"). If None, the sweep
        runs across every tenant (cron-style global sweep).
    now : float or None
        Wall-clock epoch to compare against ``retention_until``.
        Defaults to ``time.time()``; tests inject a frozen value.

    Returns
    -------
    {"meetings": int, "transcript_segments": int}
        Number of rows deleted from each table.
    """
    cutoff = float(now if now is not None else _time.time())

    # Order: segments first, then meetings. See module docstring for
    # why this ordering matters (parent->child consistency).
    seg_sql_parts = [
        "DELETE FROM transcript_segments WHERE meeting_id IN ("
        "SELECT id FROM meetings "
        "WHERE retention_until IS NOT NULL AND retention_until < $1"
    ]
    seg_params: list = [cutoff]
    if tenant_id is not None:
        seg_sql_parts.append(f"AND tenant_id = ${len(seg_params) + 1}")
        seg_params.append(tenant_id)
    seg_sql_parts.append(")")
    if tenant_id is not None:
        seg_sql_parts.append(f"AND tenant_id = ${len(seg_params) + 1}")
        seg_params.append(tenant_id)
    seg_status = await conn.execute(" ".join(seg_sql_parts), *seg_params)

    mtg_sql_parts = [
        "DELETE FROM meetings "
        "WHERE retention_until IS NOT NULL AND retention_until < $1"
    ]
    mtg_params: list = [cutoff]
    if tenant_id is not None:
        mtg_sql_parts.append(f"AND tenant_id = ${len(mtg_params) + 1}")
        mtg_params.append(tenant_id)
    mtg_status = await conn.execute(" ".join(mtg_sql_parts), *mtg_params)

    return {
        "transcript_segments": _execute_count(seg_status),
        "meetings": _execute_count(mtg_status),
    }


def _execute_count(status: str) -> int:
    """Parse the ``DELETE N`` row-count from an asyncpg execute() return.

    asyncpg ``Connection.execute`` returns a status string like
    ``"DELETE 3"`` -- the trailing integer is the affected row count.
    """
    if not status:
        return 0
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0
