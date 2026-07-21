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
import time
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


def _evidence_for(candidate: dict, *, jira_labels: "list[str] | None" = None) -> tuple:
    """Rebuild the evidence tuple from the ledger's VERIFIED snapshot. β-F wrote
    the row only after an independent non-bot +2, so a single Code-Review=+2 by
    the stored non-bot ``plus2_reviewer`` lets ``derive_ground_truths`` re-derive
    merged + review_plus2. change_ref MUST be the bare number. ``jira_labels``
    (β-1) MUST be the SAME snapshot the hard-gate judged — a stoploss label then
    surfaces as a ``stoploss`` ground-truth row (audit BLOCKER-1: no TOCTOU
    between gate and evidence)."""
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
            "jira_labels": jira_labels,
        },
    )


async def _try_llm_draft(
    conn, candidate: dict, llm_state: dict,
) -> "tuple[dict | None, str | None, list[str] | None]":
    """β-1: (payload, llm_label, jira_labels_snapshot). Cheap arms first (no
    network): revert/struggle checks on ledger fields; only a candidate that
    passes them spends the artifact fetch, whose LABEL SNAPSHOT then feeds the
    authoritative stoploss gate AND the evidence (audit BLOCKER-1). Any failure
    → (None, label, snapshot) and the caller falls back to the minimal record."""
    from backend.agents import worker_loop_distiller as distiller

    ticket_key = (candidate.get("ticket_key") or "").strip()
    incidents = await distiller.count_incidents(conn, ticket_key) if ticket_key else 0
    # Cheap arms (ledger-only, no network): revert evidence + struggle signal.
    ok, reason = distiller.hard_gate(candidate, incidents_count=incidents)
    if not ok:
        return None, f"gate_skip_{reason}", None
    # Same-ticket revert join (audit MAJOR-1 / leak L5): a SEPARATE revert
    # change for this ticket disqualifies the original fix as a "success".
    if ticket_key and await distiller.reverted_later(conn, ticket_key):
        return None, "gate_skip_reverted_later", None
    if llm_state["left"] <= 0 or time.monotonic() > llm_state.get("deadline", 0.0):
        # Tick cap / tick deadline hit (audit MAJOR-4: bound the conn+lock
        # hold across network). DEFER — leave the row UNDISTILLED so a later
        # tick's fresh budget distills it richly; minimal-downgrading a
        # gate-passer would be permanent (audit MAJOR-5).
        return None, "llm_deferred", None
    artifacts = await distiller.fetch_artifacts(candidate)
    if artifacts is None:
        return None, "llm_error", None
    labels = artifacts.get("jira_labels")
    if ticket_key and labels is None:
        # The ticket EXISTS but its labels are unavailable (JIRA blip) ⇒ the
        # evidence snapshot would be incomplete ⇒ fail CLOSED for the LLM
        # lane (correctness-audit MAJOR-3); the minimal record still ships.
        return None, "gate_skip_labels_unknown", None
    # Authoritative gate re-run with the fetched commit message (catches the
    # "This reverts commit" body mark a webhook-seeded subject missed).
    ok, reason = distiller.hard_gate(
        candidate, incidents_count=incidents,
        commit_message=artifacts.get("commit_message"),
    )
    if not ok:
        return None, f"gate_skip_{reason}", labels
    llm_state["left"] -= 1
    payload, label = await distiller.draft_record(
        candidate, artifacts, llm=llm_state.get("llm"),
    )
    if payload is None and label == "budget_exhausted":
        # Daily budget: same deferral semantics as the tick cap — the window
        # rolls and a later tick distills richly.
        return None, "llm_deferred", labels
    return payload, label, labels


async def _distill_candidate(
    conn, candidate: dict, *, now: str, llm_state: "dict | None" = None,
) -> "tuple[str, str | None]":
    """Submit ONE candidate as a quarantined record, then mark the ledger row
    distilled. Returns (submit_label: submitted|dup, llm_label|None). β-0
    behavior is byte-identical when the LLM lane is disabled."""
    ticket = (candidate.get("ticket_key") or "").strip() or f"change-{candidate['gerrit_change']}"
    payload = None
    llm_label = None
    jira_labels = None
    if llm_state is not None and llm_state.get("enabled"):
        payload, llm_label, jira_labels = await _try_llm_draft(
            conn, candidate, llm_state,
        )
        if llm_label == "llm_deferred":
            # Audit MAJOR-5: an over-budget gate-passer stays UNDISTILLED —
            # no submit, no mark; a later tick's fresh budget distills it.
            return "deferred", llm_label
    is_llm_draft = payload is not None
    if payload is None:
        payload = _build_minimal_record(candidate)

    async def _submit(p: dict, llm_draft: bool):
        return await submit_quarantined_version(
            conn,
            payload=p,
            kind="playbook",
            audience="tenant",
            tenant_id=_SELF_TENANT,
            # Provenance tier (audit MAJOR-4): β-2/β-3 reviewers weight LLM
            # drafts apart from deterministic pointers.
            created_by=(
                "ground_truth_curator_llm" if llm_draft else "ground_truth_curator"
            ),
            name=f"ground-truth:{ticket}",
            description=(
                f"Merged Gerrit change {candidate['gerrit_change']} ({ticket})"
                + (" — LLM draft" if llm_draft else "")
            ),
            delivery_mode="retrieved",
            evidence=_evidence_for(candidate, jira_labels=jira_labels),
            now=now,
        )

    try:
        result = await _submit(payload, is_llm_draft)
    except Exception:
        if not is_llm_draft:
            raise
        # An LLM draft whose submit raises must not re-spend LLM budget every
        # tick (a nondeterministic draft never dedups): one-shot terminal
        # fallback to the minimal record (audit MINOR-7 retry ceiling).
        _log.warning(
            "worker_curator: LLM-draft submit failed for %s — falling back "
            "to the minimal record", candidate.get("id"), exc_info=True,
        )
        llm_label = "llm_reject"
        is_llm_draft = False
        result = await _submit(_build_minimal_record(candidate), False)
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
    each as a quarantined record (β-1 LLM draft when the lane is enabled + the
    hard gate passes; else the β-0 minimal record). Never raises (except
    CancelledError); a single bad candidate lands in ``error`` and the rest of
    the batch runs."""
    from backend.agents import worker_loop_distiller as distiller

    now = datetime.now(timezone.utc).isoformat()
    batch = _env_int(_BATCH_ENV, _DEFAULT_BATCH, minimum=1)
    # "deferred" = over-budget gate-passers left UNDISTILLED for a later
    # tick's fresh LLM budget (never minimal-downgraded — audit MAJOR-5).
    counts = {"submitted": 0, "dup": 0, "error": 0, "deferred": 0}
    llm_counts: dict[str, int] = {}
    scanned = 0
    # β-1 lane state for this tick: per-tick LLM cap + tick deadline (audit
    # MAJOR-4) + one factory walk.
    llm_state: dict = {"enabled": distiller.llm_distill_enabled(), "left": 0, "llm": None}
    if llm_state["enabled"]:
        llm_state["left"] = distiller.per_tick_cap()
        llm_state["deadline"] = time.monotonic() + _env_float(
            "OMNISIGHT_WORKER_CURATOR_LLM_TICK_BUDGET_S", 120.0, minimum=10.0,
        )
        try:
            from backend.agents.llm import get_cheapest_model

            llm_state["llm"] = get_cheapest_model()
        except Exception:  # noqa: BLE001 — no provider ⇒ draft_record labels llm_fallback
            llm_state["llm"] = None
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
                "canonical_subject, plus2_reviewer, revert_state, patchset_count "
                "FROM curator_merge_candidates "
                "WHERE NOT distilled ORDER BY merged_at ASC LIMIT $1",
                batch,
            )
            scanned = len(rows)
            for row in rows:
                candidate = dict(row)
                try:
                    submit_label, llm_label = await _distill_candidate(
                        conn, candidate, now=now, llm_state=llm_state,
                    )
                    counts[submit_label] += 1
                    if llm_label:
                        llm_counts[llm_label] = llm_counts.get(llm_label, 0) + 1
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
    for label, n in llm_counts.items():
        metrics.worker_curator_candidates_total.labels(result=label).inc(n)
    if scanned == 0:
        # Anti-hollow: an empty scan is EXPECTED only when the ledger genuinely
        # has no undistilled rows. A ledger starved because β-F's verify is
        # degraded shows up as merge_verify_total{degraded} on the write side —
        # cross-read that gauge here when β-1 adds the LLM cost worth gating.
        _log.info("worker_curator tick: no undistilled candidates (empty_expected)")
    else:
        _log.info(
            "worker_curator tick scanned=%d submitted=%d dup=%d error=%d "
            "deferred=%d llm=%s",
            scanned, counts["submitted"], counts["dup"], counts["error"],
            counts["deferred"], llm_counts or "-",
        )
    return {"leader": True, "scanned": scanned, "llm": llm_counts, **counts}


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
        # Anti-hollow (β-1 audit MINOR-1): an operator who sets ONLY the LLM
        # flag gets a LOUD hint, not a silent no-op.
        from backend.agents import worker_loop_distiller as _d

        if _d.llm_distill_enabled():
            _log.warning(
                "OMNISIGHT_WORKER_CURATOR_LLM is set but OMNISIGHT_WORKER_CURATOR "
                "is OFF — the curator loop is disabled and the LLM flag is ignored"
            )
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
