"""β-0 (leg-2) — worker-loop curator skeleton: ground-truth candidate → quarantine.

DORMANT by default (``OMNISIGHT_WORKER_CURATOR``). Reuses the U6-8 memory-scheduler
contract VERBATIM: a single gated loop in the API process, lazy pool per tick,
a per-tick ``pg_try_advisory_lock`` leader gate (N workers → 1 scan/interval),
inert-when-disabled (0 ticks, NO pool lookup on a SQLite/no-DSN startup).

ZERO model surface in β-0: each ground-truth candidate (a runner ticket whose
Gerrit change MERGED with an independent non-bot +2 — written to
``curator_merge_candidates`` by the β-F verified-merge path) becomes a MINIMAL,
DETERMINISTIC ``LearnedItemRecord`` — scope + ONE actionable leaf that POINTS at
the merged change — submitted QUARANTINED through the U4 governed write
(``submit_quarantined_version``). This proves the source→quarantine plumbing
end-to-end with no LLM; the cheap-LLM distiller is β-1, injection is β-3 (both
behind their own flags + audits). Nothing here ever reaches a prompt: the loader
kill-switch (``OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED``) stays OFF.

Evidence: β-F only wrote a candidate row AFTER an independent non-bot +2, so the
curator rebuilds the evidence's ``gerrit_change`` dict from the ledger's VERIFIED
snapshot (the stored ``plus2_reviewer``) — ``derive_ground_truths`` re-confirms
merged + review_plus2 without another Gerrit round-trip. Evidence is best-effort
at submit (a missing ``plus2_reviewer`` just drops the review_plus2 row); real
evidence is DEMANDED later at promotion (β-2/β-3).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from backend import db_pool, metrics
from backend.learned_item_producer import submit_quarantined_version

_log = logging.getLogger(__name__)

_ENABLE_ENV = "OMNISIGHT_WORKER_CURATOR"
_INTERVAL_ENV = "OMNISIGHT_WORKER_CURATOR_INTERVAL_S"
_DEFAULT_INTERVAL_S = 300.0
_BATCH_ENV = "OMNISIGHT_WORKER_CURATOR_BATCH"
_DEFAULT_BATCH = 20
_LEADER_LOCK_KEY = "worker-loop-curator-tick"
_SELF_TENANT = "omnisight-self"

_TRUTHY = {"1", "true", "yes", "on"}


def curator_enabled() -> bool:
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in _TRUTHY


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(float(raw), minimum)
    except ValueError:
        return default


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), minimum)
    except ValueError:
        return default


def _build_minimal_record(candidate: dict) -> dict:
    """Deterministic MINIMAL LearnedItemRecord payload from a ledger row — NO
    LLM. ``scope`` names the ticket (or bare change); ONE actionable leaf points
    at the merged, human-+2 change; the owning OP-key is the card's
    evidence_reference. Satisfies ``validate_learned_item`` (scope + ≥1
    actionable leaf) without inventing any content the server can't attest."""
    ticket = (candidate.get("ticket_key") or "").strip()
    change = candidate["gerrit_change"]
    scope = f"Tickets like {ticket}" if ticket else f"Gerrit change {change}"
    step = (
        f"See merged Gerrit change {change} for the applied, human-reviewed "
        f"(+2) solution" + (f" to {ticket}." if ticket else ".")
    )
    payload: dict = {"scope": scope, "procedure_steps": [step]}
    if ticket:
        payload["evidence_references"] = [ticket]
    return payload


def _evidence_for(candidate: dict) -> tuple:
    """Rebuild the evidence tuple from the ledger's VERIFIED snapshot. β-F wrote
    the row only after an independent non-bot +2, so a single Code-Review=+2 by
    the stored non-bot ``plus2_reviewer`` lets ``derive_ground_truths`` re-derive
    merged + review_plus2. change_ref MUST be the bare number."""
    reviewer = (candidate.get("plus2_reviewer") or "").strip()
    gerrit_change = {
        "status": "MERGED",
        "currentPatchSet": {
            "approvals": [
                {"type": "Code-Review", "value": "2", "by": {"username": reviewer}}
            ]
        },
    }
    return (
        {
            "change_ref": str(candidate["gerrit_change"]),
            "gerrit_change": gerrit_change,
            "jira_labels": None,
        },
    )


async def _distill_candidate(conn, candidate: dict, *, now: str) -> str:
    """Submit ONE candidate as a quarantined minimal record, then mark the
    ledger row distilled. Returns the metric label (submitted | dup)."""
    ticket = (candidate.get("ticket_key") or "").strip() or f"change-{candidate['gerrit_change']}"
    result = await submit_quarantined_version(
        conn,
        payload=_build_minimal_record(candidate),
        kind="playbook",
        audience="tenant",
        tenant_id=_SELF_TENANT,
        created_by="ground_truth_curator",
        name=f"ground-truth:{ticket}",
        description=f"Merged Gerrit change {candidate['gerrit_change']} ({ticket})",
        delivery_mode="retrieved",
        evidence=_evidence_for(candidate),
        now=now,
    )
    # Mark distilled whether it was a real insert OR a content-dup (an identical
    # record already exists) — either way this candidate is accounted for and
    # must not be re-scanned. A submit that RAISED never reaches here, so its
    # row stays undistilled and is retried next tick.
    await conn.execute(
        "UPDATE curator_merge_candidates "
        "SET distilled = TRUE, distilled_at = clock_timestamp() WHERE id = $1",
        candidate["id"],
    )
    return "submitted" if result.created else "dup"


async def run_worker_curator_once(pool) -> dict:
    """One tick: leader-gate, scan the ledger for undistilled candidates, submit
    each as a quarantined minimal record. Never raises (except CancelledError);
    a single bad candidate lands in ``error`` and the rest of the batch runs."""
    now = datetime.now(timezone.utc).isoformat()
    batch = _env_int(_BATCH_ENV, _DEFAULT_BATCH, minimum=1)
    counts = {"submitted": 0, "dup": 0, "error": 0}
    scanned = 0
    async with pool.acquire() as conn:
        # Leader gate (session-level try-lock; released in finally / on drop).
        got = await conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", _LEADER_LOCK_KEY
        )
        if not got:
            return {"leader": False, "scanned": 0, **counts}
        try:
            rows = await conn.fetch(
                "SELECT id, ticket_key, gerrit_change, change_id, "
                "canonical_subject, plus2_reviewer, revert_state "
                "FROM curator_merge_candidates "
                "WHERE NOT distilled ORDER BY merged_at ASC LIMIT $1",
                batch,
            )
            scanned = len(rows)
            for row in rows:
                candidate = dict(row)
                try:
                    counts[await _distill_candidate(conn, candidate, now=now)] += 1
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — one bad row never starves the batch
                    counts["error"] += 1
                    _log.warning(
                        "worker_curator: candidate %s failed",
                        candidate.get("id"), exc_info=True,
                    )
        finally:
            try:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))", _LEADER_LOCK_KEY
                )
            except Exception:  # noqa: BLE001 — conn teardown releases it anyway
                pass

    for label, n in counts.items():
        if n:
            metrics.worker_curator_candidates_total.labels(result=label).inc(n)
    if scanned == 0:
        # Anti-hollow: an empty scan is EXPECTED only when the ledger genuinely
        # has no undistilled rows. A ledger starved because β-F's verify is
        # degraded shows up as merge_verify_total{degraded} on the write side —
        # cross-read that gauge here when β-1 adds the LLM cost worth gating.
        _log.info("worker_curator tick: no undistilled candidates (empty_expected)")
    else:
        _log.info(
            "worker_curator tick scanned=%d submitted=%d dup=%d error=%d",
            scanned, counts["submitted"], counts["dup"], counts["error"],
        )
    return {"leader": True, "scanned": scanned, **counts}


async def run_worker_curator_loop(
    *,
    get_pool: Callable[[], object] = db_pool.get_pool,
    interval_s: float | None = None,
    should_continue: Callable[[], bool] = lambda: True,
    max_ticks: "int | None" = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Run the curator loop IFF ``OMNISIGHT_WORKER_CURATOR`` is set; else an
    immediate inert return (0 ticks, NO pool lookup — the lifespan create_task is
    safe on a SQLite/no-DSN startup). Mirrors the U6-8 scheduler-loop contract:
    ticks immediately then sleeps; ``get_pool`` is called LAZILY per tick;
    cancellable at shutdown; returns the tick count."""
    if not curator_enabled():
        return 0
    if interval_s is None:
        interval_s = _env_float(_INTERVAL_ENV, _DEFAULT_INTERVAL_S, minimum=5.0)
    if interval_s <= 0:
        raise ValueError("interval_s must be > 0")
    if max_ticks is not None and max_ticks < 0:
        raise ValueError("max_ticks must be >= 0")

    ticks = 0
    while should_continue() and (max_ticks is None or ticks < max_ticks):
        await asyncio.sleep(0)  # real yield -> always cancellable
        try:
            pool = get_pool()
            result = await run_worker_curator_once(pool)
            if not result["leader"]:
                outcome = "skipped"
            else:
                outcome = "error" if result["error"] else "ok"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — no pool yet / unexpected: keep looping
            outcome = "error"
            _log.warning("worker_curator tick failed", exc_info=True)
        metrics.worker_curator_ticks_total.labels(outcome=outcome).inc()
        ticks += 1
        await sleep(interval_s)
    return ticks
