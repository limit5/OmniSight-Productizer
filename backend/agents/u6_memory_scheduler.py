"""U6-8 — memory scheduler: session-end summarize + decay (DORMANT by default).

The LAST leg-1 increment (frozen design §8 "schedulers + monitors"): a single
gated loop in the API process that drives the memory tier's LIFECYCLE — the
parts that must happen when no request is in flight. Two arms per tick, each
independently fault-guarded (one arm's failure never starves the other):

  A. **Session-end summarize** — finds chat sessions that went INACTIVE
     (no message for ``OMNISIGHT_U6_SESSION_INACTIVITY_S``, bounded by a
     lookback window + a per-tick cap), loads each session's messages in the
     STABLE canonical order (``timestamp, id`` — the order-sensitivity DoD the
     U6-2b watermark documents) and calls the already-merged, already-gated
     ``write_session_summary`` with ``SessionEndReason.INACTIVITY_TIMEOUT``.
     Exactly-once safety is the WRITER's (watermark dedup + advisory lock); the
     scheduler is allowed to be dumb and re-offer candidates. LAYERED kill
     switches (§7): this arm additionally requires ``OMNISIGHT_U6_L2_WRITE`` —
     scheduler-ON + L2-write-OFF runs decay only.

     v1 outcome is STRUCTURAL, not distilled: ``OutcomeKind.NO_OUTCOME`` +
     server-counted ``turn_count`` + ``resolved=False`` + no tasks/topics — the
     closed U6-2a schema filled ONLY from facts the server can count without
     reading meaning into content. The LLM distiller that adjudicates
     kind/resolved/tasks is a LATER increment that upgrades ``_build_outcome``
     behind the U6-3 eval gate; shipping the lifecycle first is deliberate
     (the plumbing is provable now; the smart payload arrives gated).

  B. **Decay** — two content-free, state-only lifecycle transitions on
     ``l3_facts`` (values stay sealed; nothing reads content), batched per
     tick so a backlog can't outlive the statement timeout:
       - a PROMOTED fact past its ``valid_until`` date → ``superseded``.
         Decay is HYGIENE here, not the enforcement: the U6-7 READER excludes
         expired facts at read time regardless (the injectable-set validity
         window is enforced at the boundary; this sweep just tidies state).
       - a QUARANTINED candidate older than
         ``OMNISIGHT_U6_L3_QUARANTINE_TTL_DAYS`` → ``rejected`` AND its
         ``dek_ref`` destroyed (§2.F "pending items expire"): the user never
         consented to this content, so retaining a live key is pure
         liability — mirror the ``erase_user`` crypto-shred posture (destroys,
         never reads).
     NOTE(scope/RLS): decay is a GLOBAL server-side maintenance sweep (a
     uniform lifecycle rule, not a per-user read), so its UPDATEs carry state
     predicates rather than per-scope predicates. Like ``u6_l3_store`` it runs
     under today's superuser app role where FORCED RLS does not bite; at
     de-superuser time this sweep needs an explicit maintenance role
     (BYPASSRLS or per-scope iteration) — until then a non-bypass role yields
     a SILENT 0-row sweep, which the ``u6_l3_expired_promoted_backlog`` gauge
     + ``u6_l3_decay_total`` counter make visible (not silent).

LEADER GATE: prod runs several uvicorn workers, each with its own lifespan
loop — a per-tick ``pg_try_advisory_lock`` elects one worker per interval and
the rest skip (outcome ``skipped``), so the fleet does ONE candidate scan per
interval, not N (the writer's advisory lock + watermark dedup would keep N
loops CORRECT — the gate removes the redundant work + counter noise).

DEFAULT-OFF: unless ``OMNISIGHT_SORA_MEMORY_SCHEDULER`` (§7's scheduler kill
switch) is truthy the loop returns immediately WITHOUT any pool lookup, so the
lifespan ``create_task`` is inert on SQLite/no-DSN startups. The liveness
MONITOR arm of U6-8 lives in ``u6_metrics_refresh`` (DB-derived gauges:
summaries count/age + l3 facts by state + expired-promoted backlog) — external
to this producer, so a flag-ON-but-hollow scheduler is VISIBLE (frozen count +
growing age), the 3D-memory lesson. ⚠ That visibility itself requires
``OMNISIGHT_U6_METRICS_ENABLED`` — the enable runbook must flip BOTH (a
scheduler enabled without its monitor is the hollow-enable trap again).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from backend import db_pool, metrics
from backend.agents.u6_l2_outcome import OutcomeKind, SessionOutcome
from backend.agents.u6_l2_writer import (
    SessionEndReason,
    l2_write_enabled,
    write_session_summary,
)
from backend.agents.u6_memory_scope import MemoryScope

_log = logging.getLogger(__name__)

_ENABLE_ENV = "OMNISIGHT_SORA_MEMORY_SCHEDULER"
_INACTIVITY_ENV = "OMNISIGHT_U6_SESSION_INACTIVITY_S"
_LOOKBACK_ENV = "OMNISIGHT_U6_SESSION_LOOKBACK_S"
_INTERVAL_ENV = "OMNISIGHT_U6_SCHEDULER_INTERVAL_S"
_QUARANTINE_TTL_ENV = "OMNISIGHT_U6_L3_QUARANTINE_TTL_DAYS"

_DEFAULT_INACTIVITY_S = 1800.0  # 30 min without a message = session end
_DEFAULT_LOOKBACK_S = 7 * 86400.0  # don't rescan sessions dead longer than this
_DEFAULT_INTERVAL_S = 300.0
_DEFAULT_QUARANTINE_TTL_DAYS = 14.0
_MAX_SESSIONS_PER_TICK = 50  # bound one tick's work; the next tick continues
_MAX_MESSAGES_PER_SESSION = 2000  # summarize the NEWEST-N window (WARN at cap)
_MAX_SESSION_BYTES = 2_000_000  # skip a session whose content sum exceeds this
_DECAY_BATCH = 1000  # per-tick decay batch; a backlog drains across ticks
_CHARS_PER_TOKEN = 3  # the established rough estimate (nodes.py L2 gate)
_MAX_TURNS = 100_000  # mirror u6_l2_outcome._MAX_TURNS (clamp, never raise)
_LEADER_LOCK_KEY = "u6-memory-scheduler-tick"

#: The structural distiller's fingerprint — no model is involved in v1.
STRUCTURAL_FINGERPRINT = "u6-8-structural-v1"


def memory_scheduler_enabled() -> bool:
    """§7 scheduler kill switch (default OFF; same predicate as the other U6 flags)."""
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _env_float(env: str, default: float, *, minimum: float) -> float:
    """A tunable numeric knob: malformed/out-of-range values fall back LOUDLY
    (never crash the loop over a typo'd env). Every return path honors
    ``minimum`` — including the fallbacks — so a caller-supplied dynamic
    minimum (e.g. lookback ≥ inactivity) can never be silently violated."""
    raw = os.environ.get(env, "").strip()
    if not raw:
        return max(default, minimum)
    try:
        value = float(raw)
    except ValueError:
        _log.warning("u6_memory_scheduler bad %s=%r; using default %s", env, raw, default)
        return max(default, minimum)
    if value < minimum:
        _log.warning("u6_memory_scheduler %s=%s below minimum %s; using default", env, value, default)
        return max(default, minimum)
    return value


@dataclass(frozen=True, slots=True)
class TickResult:
    """One tick's outcome (returned for tests; logged for operators)."""

    summarized: int = 0
    duplicates: int = 0
    summarize_errors: int = 0
    skipped_oversize: int = 0
    decayed_expired: int = 0
    decayed_quarantine: int = 0
    arm_errors: tuple[str, ...] = ()
    leader: bool = True  # False = another worker held the tick lock; no work ran

    @property
    def ok(self) -> bool:
        return not self.arm_errors and self.summarize_errors == 0


# ── Arm A: session-end summarize ─────────────────────────────────────────────

# The NOT EXISTS is a WORK BOUND, not the correctness gate (the writer's
# watermark dedup is): a session whose newest summary is COMFORTABLY newer
# than its last message is already covered — skip it. The 30s grace guards
# the cross-worker clock-skew window (a late message stamped just below the
# summary's created_at must NOT be excluded forever) while staying strictly
# below the 60s inactivity floor (else a min-inactivity session would
# re-offer every tick); inside the grace the session is merely re-offered
# and the writer answers duplicate_watermark. ``bytes`` feeds the oversize
# skip — a pathological session is refused BEFORE any content is fetched.
_CANDIDATE_SQL = """
SELECT m.tenant_id, m.user_id, m.session_id,
       max(m.timestamp) AS last_ts, count(*) AS n,
       sum(length(m.content)) AS bytes
FROM chat_messages m
WHERE m.session_id <> ''
GROUP BY m.tenant_id, m.user_id, m.session_id
HAVING max(m.timestamp) < $1
   AND max(m.timestamp) > $2
   AND NOT EXISTS (
        SELECT 1 FROM chat_session_summaries s
        WHERE s.tenant_id = m.tenant_id
          AND s.user_id = m.user_id
          AND s.session_id = m.session_id
          AND s.created_at > to_timestamp(max(m.timestamp)) + interval '30 seconds'
   )
ORDER BY last_ts DESC
LIMIT $3
"""

# NEWEST-N window (then reversed to chronological): for an over-cap session
# the watermark then always ADVANCES on a late turn — a new revision is
# written and the NOT EXISTS re-excludes it (an oldest-N window would freeze
# the watermark: permanent duplicate_watermark re-offers + a stale summary —
# audit F2). The summary honestly covers the recent tail; hitting the cap is
# WARN-logged (no silent truncation).
_MESSAGES_SQL = """
SELECT id, role, content
FROM chat_messages
WHERE tenant_id = $1 AND user_id = $2 AND session_id = $3
ORDER BY timestamp DESC, id DESC
LIMIT $4
"""


def _build_outcome(rows: list) -> SessionOutcome:
    """The v1 STRUCTURAL outcome: only server-countable facts, no content
    interpretation (see module docstring — the LLM distiller is later)."""
    user_turns = sum(1 for r in rows if r["role"] in ("user", "operator", "human"))
    return SessionOutcome(
        outcome_kind=OutcomeKind.NO_OUTCOME,
        turn_count=min(user_turns, _MAX_TURNS),
        resolved=False,
    )


async def _summarize_inactive_sessions(conn, *, now_ts: float) -> tuple[int, int, int, int]:
    """Find inactive sessions and offer each to the (gated) L2 writer.

    Returns ``(written, duplicates, errors, skipped_oversize)``. Per-candidate
    faults are counted and skipped — one broken session never blocks the rest.
    A session whose total content exceeds ``_MAX_SESSION_BYTES`` is REFUSED
    before any content is fetched (audit F1: an unbounded fetch would balloon
    the API process + sha256 the lot on the event loop); the skip is loud
    (WARN + counter), bounded per tick by the candidate cap."""
    inactivity = _env_float(_INACTIVITY_ENV, _DEFAULT_INACTIVITY_S, minimum=60.0)
    lookback = _env_float(_LOOKBACK_ENV, _DEFAULT_LOOKBACK_S, minimum=inactivity)
    cutoff = now_ts - inactivity
    floor = now_ts - lookback
    candidates = await conn.fetch(_CANDIDATE_SQL, cutoff, floor, _MAX_SESSIONS_PER_TICK)
    written = duplicates = errors = skipped = 0
    for cand in candidates:
        try:
            if (cand["bytes"] or 0) > _MAX_SESSION_BYTES:
                skipped += 1
                metrics.u6_l2_summaries_written_total.labels(result="skipped_oversize").inc()
                _log.warning(
                    "u6_memory_scheduler skipping oversize session=%s bytes=%s (cap=%d)",
                    cand["session_id"], cand["bytes"], _MAX_SESSION_BYTES,
                )
                continue
            rows = await conn.fetch(
                _MESSAGES_SQL,
                cand["tenant_id"], cand["user_id"], cand["session_id"],
                _MAX_MESSAGES_PER_SESSION,
            )
            if not rows:
                continue
            if len(rows) == _MAX_MESSAGES_PER_SESSION:
                _log.warning(
                    "u6_memory_scheduler session=%s hit the %d-message window; "
                    "summary covers the newest tail only",
                    cand["session_id"], _MAX_MESSAGES_PER_SESSION,
                )
            rows = list(rows)[::-1]  # newest-N window → chronological order
            messages = [(r["id"], r["content"]) for r in rows]
            token_count = sum(len(c) for _, c in messages) // _CHARS_PER_TOKEN
            result = await write_session_summary(
                conn,
                scope=MemoryScope(tenant_id=cand["tenant_id"], user_id=cand["user_id"]),
                session_id=cand["session_id"],
                reason=SessionEndReason.INACTIVITY_TIMEOUT,
                outcome=_build_outcome(rows),
                messages=messages,
                token_count=token_count,
                model_fingerprint=STRUCTURAL_FINGERPRINT,
            )
            if result.reason == "written":
                written += 1
                metrics.u6_l2_summaries_written_total.labels(result="written").inc()
            elif result.reason == "duplicate_watermark":
                duplicates += 1
                metrics.u6_l2_summaries_written_total.labels(result="duplicate").inc()
            # reason == "disabled" is unreachable here (arm gated on the flag),
            # but if it happens it is simply not counted as progress.
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one candidate must not block the rest
            errors += 1
            metrics.u6_l2_summaries_written_total.labels(result="error").inc()
            _log.warning(
                "u6_memory_scheduler summarize failed session=%s", cand["session_id"],
                exc_info=True,
            )
    return written, duplicates, errors, skipped


# ── Arm B: decay (content-free state transitions) ────────────────────────────

# Batched (audit F4): an unbounded backlog UPDATE would hit the pool's 30s
# statement_timeout, roll back, and retry identically forever — zero progress.
# A bounded batch always completes; the remainder drains on later ticks.
_DECAY_EXPIRED_SQL = """
UPDATE l3_facts SET state = 'superseded'
WHERE id IN (
    SELECT id FROM l3_facts
    WHERE state = 'promoted' AND valid_until IS NOT NULL AND valid_until < $1
    LIMIT $2
)
RETURNING id
"""

# TTL-rejected candidates were NEVER user-confirmed — destroy the dek_ref too
# (crypto-shred posture, mirrors erase_user: destroys the key, reads nothing).
_DECAY_QUARANTINE_SQL = """
UPDATE l3_facts SET state = 'rejected', dek_ref = '{}'::jsonb
WHERE id IN (
    SELECT id FROM l3_facts
    WHERE state = 'quarantined'
      AND created_at < now() - ($1::float8 * interval '1 day')
    LIMIT $2
)
RETURNING id
"""


async def _decay_facts(conn, *, today_iso: str) -> tuple[int, int]:
    """Run both decay transitions (one batch each). ``valid_until`` is the
    U6-1a zero-padded ISO date grammar, so lexicographic ``<`` IS
    chronological. NOTE: decay is hygiene — the U6-7 reader excludes expired
    facts at read time regardless of whether this sweep has run."""
    ttl_days = _env_float(
        _QUARANTINE_TTL_ENV, _DEFAULT_QUARANTINE_TTL_DAYS, minimum=1.0
    )
    expired = await conn.fetch(_DECAY_EXPIRED_SQL, today_iso, _DECAY_BATCH)
    stale = await conn.fetch(_DECAY_QUARANTINE_SQL, ttl_days, _DECAY_BATCH)
    if expired:
        metrics.u6_l3_decay_total.labels(kind="expired").inc(len(expired))
    if stale:
        metrics.u6_l3_decay_total.labels(kind="quarantine_ttl").inc(len(stale))
    return len(expired), len(stale)


# ── The tick + the loop ──────────────────────────────────────────────────────

async def run_memory_scheduler_once(pool, *, now_ts: float | None = None) -> TickResult:
    """One tick: leader-gate, then both arms, independently guarded. Never
    raises (except CancelledError); a failed arm lands in ``arm_errors`` and
    the other arm still runs. A non-leader tick (another worker holds the
    advisory lock this interval) does NO work and reports ``leader=False``."""
    import time as _time

    now = float(now_ts) if now_ts is not None else _time.time()
    summarized = duplicates = summarize_errors = skipped = 0
    decayed_expired = decayed_quarantine = 0
    arm_errors: list[str] = []
    async with pool.acquire() as conn:
        # Leader gate (session-level try-lock; N workers → 1 scan/interval).
        # Released in ``finally`` on the SAME connection; a dropped connection
        # releases it server-side.
        got = await conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", _LEADER_LOCK_KEY
        )
        if not got:
            return TickResult(leader=False)
        try:
            if l2_write_enabled():
                try:
                    summarized, duplicates, summarize_errors, skipped = (
                        await _summarize_inactive_sessions(conn, now_ts=now)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — arm-isolated; decay still runs
                    arm_errors.append("summarize")
                    _log.warning("u6_memory_scheduler summarize arm failed", exc_info=True)
            try:
                # UTC date: every other timestamp in the system is UTC (the
                # pool forces timezone=UTC) — the day boundary must match.
                decayed_expired, decayed_quarantine = await _decay_facts(
                    conn, today_iso=datetime.now(timezone.utc).date().isoformat()
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — arm-isolated
                arm_errors.append("decay")
                _log.warning("u6_memory_scheduler decay arm failed", exc_info=True)
        finally:
            try:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))", _LEADER_LOCK_KEY
                )
            except Exception:  # noqa: BLE001 — conn teardown releases it anyway
                pass
    result = TickResult(
        summarized=summarized,
        duplicates=duplicates,
        summarize_errors=summarize_errors,
        skipped_oversize=skipped,
        decayed_expired=decayed_expired,
        decayed_quarantine=decayed_quarantine,
        arm_errors=tuple(arm_errors),
    )
    _log.info(
        "u6_memory_scheduler tick summarized=%d duplicates=%d s_errors=%d "
        "skipped_oversize=%d decayed_expired=%d decayed_quarantine=%d arm_errors=%s",
        result.summarized, result.duplicates, result.summarize_errors,
        result.skipped_oversize, result.decayed_expired, result.decayed_quarantine,
        ",".join(result.arm_errors) or "-",
    )
    return result


async def run_memory_scheduler_loop(
    *,
    get_pool: Callable[[], object] = db_pool.get_pool,
    interval_s: float | None = None,
    should_continue: Callable[[], bool] = lambda: True,
    max_ticks: "int | None" = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Run the scheduler loop IFF ``OMNISIGHT_SORA_MEMORY_SCHEDULER`` is set;
    else an immediate inert return (0 ticks). Mirrors the metrics-refresh loop
    contract exactly: ticks immediately then sleeps; ``get_pool`` is called
    LAZILY per tick (a disabled task never triggers ``db_pool.get_pool()`` on a
    SQLite/no-DSN startup); cancellable at shutdown; returns the tick count."""
    if not memory_scheduler_enabled():
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
            result = await run_memory_scheduler_once(pool)
            if not result.leader:
                outcome = "skipped"  # another worker ran this interval's tick
            else:
                outcome = "ok" if result.ok else "error"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — no pool yet / unexpected: keep looping
            outcome = "error"
            _log.warning("u6_memory_scheduler tick failed", exc_info=True)
        metrics.u6_memory_scheduler_ticks_total.labels(outcome=outcome).inc()
        ticks += 1
        await sleep(interval_s)
    return ticks
