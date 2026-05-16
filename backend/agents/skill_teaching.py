"""RPG.W12.6 (OP-175) — same-Guild idle teach with distilled summary.

The W12.3 :func:`backend.agents.skill_leveling.teach_other_agent`
primitive owns the *atomic* per-skill teach: it gates on the teacher
being Lv 5, the student at or below Lv 2, and the 7-day cooldown, then
awards a one-shot ``+25`` XP and stamps ``last_taught_at`` on the
teacher.

ADR-0008 §"Skill leveling (W12)" line 140 commits to a *thicker*
contract for the Lv-5 unlock:

    Lv-5 holders can inject distilled summaries into same-Guild
    same-skill Lv ≤ 2 instances (one-shot +25 XP, cooldown 7 day)

The "same-Guild", "idle when injecting", and "distilled summary
injected" pieces are not in the per-skill state row — they cross the
Guild registry and BP.M dim memory. This module ships the W12.6
orchestrator that wires those three gates around the W12.3 primitive:

1. Same-Guild check.  Both teacher and student card guilds must
   resolve to the same :class:`backend.sandbox_tier.Guild` value
   declared in :mod:`backend.agents.guild_registry`.  Mismatch raises
   :class:`TeachGuildMismatch` *before* any state mutates.
2. Idle gate on the teacher.  The caller passes
   ``teacher_last_dispatch_at`` (the last task-completion timestamp
   from the W4.1 XP engine).  If ``now - teacher_last_dispatch_at <
   TEACH_IDLE_MIN_SECONDS`` (default :data:`TEACH_IDLE_MIN_SECONDS`),
   :class:`TeachTeacherNotIdle` fires — the teacher is mid-task and
   may not interrupt itself to teach.  ``None`` means "no recent
   dispatch on record", which we treat as idle.
3. Atomic teach via :func:`teach_other_agent` — preserves the W12.3
   cooldown + Lv-gate contract.  Failures here short-circuit before
   summary injection so we never write a summary for a teach that was
   refused at the per-skill row.
4. Best-effort summary injection into BP.M dim memory via
   :func:`backend.agents.skill_memory.vectorize_distilled_skills`.
   Tagged with the *student* ``(agent_id, skill_id)`` so subsequent
   :func:`retrieve_distilled_skills` reads from the student's scope
   see the injected summary.  If summary writing fails the XP delta
   is still legitimately awarded — the caller sees
   ``summary_written=False`` on the outcome and can replay the
   summary write separately.

Module-global state audit (SOP Step 1)
--------------------------------------
This module owns *no* mutable module-global state.  All state is
delegated to the injected
:class:`~backend.agents.skill_leveling.SkillStateStore`,
:class:`~backend.agents.rag.EmbeddingProvider`, and
:class:`~backend.agents.rag.VectorStore`.  The constants
:data:`TEACH_IDLE_MIN_SECONDS` and exception classes live at module
scope and are immutable.

Read-after-write timing audit (SOP Step 1)
------------------------------------------
The teach + summary writes commit serially through the caller's
connection-bound stores; readers observe the new ``agent_skill_state``
row + BP.M vector entry only after their respective commits.  The
teach write happens *before* the summary write, so a partial failure
leaves XP awarded with no summary — never the reverse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from backend.agents.guild_registry import GUILDS
from backend.agents.rag import EmbeddingProvider, VectorStore
from backend.agents.skill_leveling import (
    SkillLevelingError,
    SkillStateStore,
    SkillXpAward,
    teach_other_agent,
)
from backend.agents.skill_memory import (
    DistilledSkillMemoryEntry,
    vectorize_distilled_skills,
)


logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────

# Minimum idle window the teacher must satisfy before injecting a
# distilled summary.  ADR-0008 §"Skill leveling (W12)" pins the teach
# to "idle 時"; it leaves the precise threshold to the implementation.
# 30 minutes mirrors the smallest "not actively dispatched" window the
# buff/debuff layer treats as significant — far smaller than the 4-hour
# ``WELL_RESTED_IDLE_SECONDS`` buff (a teach is a brief, opportunistic
# action; "well rested" is a sustained-rest state).
TEACH_IDLE_MIN_SECONDS: int = 30 * 60


# ── Errors ──────────────────────────────────────────────────────────


class TeachGuildMismatch(SkillLevelingError):
    """Raised when teacher and student are not in the same Guild.

    W12.6 narrows the W12.3 teach primitive to *same-Guild* pairs so
    teach traffic respects the Guild specialization boundary.  This
    error fires before any state mutates.
    """


class TeachTeacherNotIdle(SkillLevelingError):
    """Raised when a teach is attempted while the teacher is not idle.

    Per ADR-0008 §"Skill leveling (W12)" the Lv-5 teach is a *idle-
    time* injection — a teacher mid-task may not interrupt itself to
    push a summary into another agent.  The caller passes the
    teacher's last dispatch-completion timestamp; if the elapsed
    window is below :data:`TEACH_IDLE_MIN_SECONDS` this error fires
    before the W12.3 primitive runs.
    """


# ── Dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class DistilledTeachOutcome:
    """Return value of :func:`teach_distilled_summary`.

    ``award`` is the W12.3 :class:`SkillXpAward` returned by the
    underlying :func:`teach_other_agent` call — the source of truth
    for the +25 XP delta, the student's new level, and any unlock
    flags crossed in the same award.

    ``summary_written`` is ``True`` when the distilled summary was
    successfully upserted into the BP.M dim memory store.  ``False``
    means the XP award still happened but the summary write was
    skipped or failed; the caller can replay the summary write later
    without re-running the teach.
    """

    award: SkillXpAward
    teacher_guild: str
    student_guild: str
    summary_written: bool
    summary_entry: DistilledSkillMemoryEntry | None


# ── Public operation ────────────────────────────────────────────────


async def teach_distilled_summary(
    skill_store: SkillStateStore,
    *,
    teacher_id: str,
    teacher_guild: str,
    teacher_last_dispatch_at: datetime | None,
    student_id: str,
    student_guild: str,
    skill_id: str,
    tenant_id: str,
    distilled_summary: str,
    embedder: EmbeddingProvider,
    vector_store: VectorStore,
    source_skill_draft_id: str | None = None,
    idle_min_seconds: int = TEACH_IDLE_MIN_SECONDS,
    now: datetime | None = None,
) -> DistilledTeachOutcome:
    """Inject a Lv-5 distilled skill summary into a same-Guild Lv ≤ 2 peer.

    The W12.6 orchestration is:

    1. Validate ``teacher_guild`` / ``student_guild`` resolve to the
       same Guild registered in :data:`GUILDS`.
    2. Validate the teacher is idle: ``teacher_last_dispatch_at`` is
       ``None`` (no recent dispatch on record) or older than
       ``idle_min_seconds`` relative to ``now``.
    3. Call :func:`teach_other_agent` for the atomic +25 XP + 7-day
       cooldown stamp + Lv ≤ 2 student gate.
    4. Best-effort upsert of the distilled summary into the student's
       BP.M dim memory scope via
       :func:`vectorize_distilled_skills`.  Failures here are logged
       but do not roll back the XP award — the caller sees
       ``summary_written=False``.

    Parameters
    ----------
    skill_store:
        Backing :class:`SkillStateStore` for the per-skill row.
    teacher_id, student_id:
        Agent IDs.  Must differ (the W12.3 primitive enforces).
    teacher_guild, student_guild:
        Guild slugs from the agents' :class:`CharacterCard` rows.
        Must both be present in :data:`GUILDS` and must match.
    teacher_last_dispatch_at:
        Last task-completion timestamp for the teacher (from the W4.1
        XP engine telemetry).  ``None`` means "no recent dispatch on
        record" and is treated as idle.
    skill_id:
        The W12 skill the teach is scoped to.  Must be declared in
        ``skill_matrix.yaml``; the W12.3 primitive enforces.
    tenant_id:
        Tenant scope for the BP.M dim memory upsert.
    distilled_summary:
        The ≤200-token markdown body the W5.2 distiller produced
        (or the operator authored) for this teacher × skill pair.
    embedder, vector_store:
        BP.Q-compatible embedder + tenant-scoped vector store used by
        :func:`vectorize_distilled_skills`.
    source_skill_draft_id:
        Optional ``auto_distilled_skills.id`` (BP.M.1) so the BP.M
        entry's ``source_skill_draft_id`` metadata back-references the
        review-queue row.
    idle_min_seconds:
        Override the default :data:`TEACH_IDLE_MIN_SECONDS` threshold
        for tests or alternate policies.
    now:
        Override the wall-clock for deterministic tests.

    Raises
    ------
    TeachGuildMismatch
        when ``teacher_guild`` != ``student_guild``.
    TeachTeacherNotIdle
        when the teacher's idle window is below ``idle_min_seconds``.
    SkillLevelingError
        re-raised from :func:`teach_other_agent` (Lv gate, cooldown,
        student-too-high, teacher == student, skill_id drift).
    """

    if not isinstance(distilled_summary, str) or not distilled_summary.strip():
        raise ValueError("distilled_summary is required")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id is required")

    when = _utc(now or datetime.now(timezone.utc))

    teacher_guild_norm = _require_guild("teacher_guild", teacher_guild)
    student_guild_norm = _require_guild("student_guild", student_guild)
    if teacher_guild_norm != student_guild_norm:
        raise TeachGuildMismatch(
            f"teacher {teacher_id!r} guild {teacher_guild_norm!r} != "
            f"student {student_id!r} guild {student_guild_norm!r}; "
            "teach is scoped to same-Guild pairs (ADR-0008 W12)"
        )

    _assert_teacher_idle(
        teacher_id=teacher_id,
        teacher_last_dispatch_at=teacher_last_dispatch_at,
        now=when,
        idle_min_seconds=idle_min_seconds,
    )

    award = await teach_other_agent(
        skill_store,
        teacher_id,
        student_id,
        skill_id,
        now=when,
    )

    entry = DistilledSkillMemoryEntry(
        tenant_id=tenant_id.strip(),
        agent_id=student_id,
        skill_id=skill_id,
        summary=distilled_summary,
        source_skill_draft_id=source_skill_draft_id,
        metadata={"injected_by_teacher_id": teacher_id, "via": "rpg_w12_6_teach"},
    )

    summary_written = False
    try:
        written = await vectorize_distilled_skills(
            (entry,),
            embedder=embedder,
            store=vector_store,
        )
        summary_written = written == 1
    except Exception as exc:  # pragma: no cover — defensive log path
        logger.warning(
            "rpg_w12_6_teach summary write failed for teacher=%s student=%s "
            "skill=%s: %s",
            teacher_id,
            student_id,
            skill_id,
            exc,
        )

    return DistilledTeachOutcome(
        award=award,
        teacher_guild=teacher_guild_norm,
        student_guild=student_guild_norm,
        summary_written=summary_written,
        summary_entry=entry if summary_written else None,
    )


# ── Internal helpers ────────────────────────────────────────────────


def _require_guild(field: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} is required")
    if clean not in GUILDS:
        raise TeachGuildMismatch(
            f"{field}={clean!r} is not a registered Guild slug "
            "(see backend.agents.guild_registry.GUILDS)"
        )
    return clean


def _assert_teacher_idle(
    *,
    teacher_id: str,
    teacher_last_dispatch_at: datetime | None,
    now: datetime,
    idle_min_seconds: int,
) -> None:
    if idle_min_seconds < 0:
        raise ValueError("idle_min_seconds must be >= 0")
    if teacher_last_dispatch_at is None:
        return
    last = _utc(teacher_last_dispatch_at)
    elapsed = (now - last).total_seconds()
    if elapsed < idle_min_seconds:
        raise TeachTeacherNotIdle(
            f"teacher {teacher_id!r} idle for {int(elapsed)}s "
            f"< {idle_min_seconds}s; teach is gated on the teacher being "
            "idle per ADR-0008 §'Skill leveling (W12)'"
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "DistilledTeachOutcome",
    "TEACH_IDLE_MIN_SECONDS",
    "TeachGuildMismatch",
    "TeachTeacherNotIdle",
    "teach_distilled_summary",
]
