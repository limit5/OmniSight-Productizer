"""OP-2576 U4-I — Architect Guild trajectory → quarantined learned item.

Redirect of the legacy BP.M.2 distiller: the ``auto_distilled_skills``
draft-writer is REPLACED by the substrate producer
(:mod:`backend.learned_item_producer`). Every eligible trajectory now
lands as a quarantined substrate version row (inert downstream — no
eval/approval/publication until U4-J).

Existing readers of ``auto_distilled_skills`` (routers/auto_skills.py,
agents/skill_teaching.py, agents/skill_memory.py) keep working on the
FROZEN ARCHIVE rows — this module simply stops adding to it. No dual
truth: exactly one system writes learned content going forward.

Thresholds (MIN_TOOL_CALLS/MIN_ITERATIONS), the ``is_enabled()`` L1
gate, and the best-effort wrapper semantics of ``architect_guild_hook``
are UNCHANGED — the workflow finish hook shape is preserved.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from backend.db_context import current_tenant_id, set_tenant_id, tenant_insert_value
from backend.learned_item_producer import submit_quarantined_version
from backend.skills_scrubber import is_safe_to_promote, scrub

logger = logging.getLogger(__name__)

MIN_TOOL_CALLS = 6
MIN_ITERATIONS = 4

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class TrajectoryStats:
    """Difficulty + outcome counters extracted from a workflow trajectory."""

    tool_calls: int
    iterations: int
    success: bool


@dataclass(frozen=True)
class DistilledSkillDraft:
    """Legacy result shell preserved for callers/tests.

    The row itself no longer lives in ``auto_distilled_skills`` — the
    ``id`` field now carries the substrate version id and
    ``markdown_content`` carries the scrubbed source template. The
    other fields document what the record was distilled from.
    """

    id: str
    tenant_id: str
    skill_name: str
    source_task_id: str | None
    markdown_content: str
    version: int = 1
    status: str = "quarantined"


@dataclass(frozen=True)
class SkillDistillationResult:
    """Result from ``distill`` / ``architect_guild_hook``.

    Shape preserved (written / draft / stats / hits / skipped_reason) —
    callers and existing tests key off these fields.
    """

    written: bool
    draft: DistilledSkillDraft | None
    stats: TrajectoryStats
    hits: Counter[str]
    skipped_reason: str = ""


def _markdown_sha256(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def is_enabled() -> bool:
    """Mirror the existing L1 self-improvement gate.

    ``off | l3`` keeps the hook inert; ``l1 | l1+l3 | all`` enables
    quarantined-version creation. The eval/approval/publish gate is a
    separate kill-switch (OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED)
    owned by U4-C/D/J — this L1 gate remains the intake gate.
    """

    level = (os.environ.get("OMNISIGHT_SELF_IMPROVE_LEVEL") or "off").strip().lower()
    if level in {"", "off"}:
        return False
    return level == "all" or "l1" in level


def _slugify(text: str, max_len: int = 48) -> str:
    slug = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return (slug[:max_len].strip("-") or "trajectory-skill")


def _coerce_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        text = value.strip()
        return int(text) if text.isdigit() else None
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if isinstance(value, dict):
        for key in ("count", "total", "value"):
            count = _coerce_count(value.get(key))
            if count is not None:
                return count
        return len(value)
    return None


def _metadata_count(metadata: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        count = _coerce_count(metadata.get(key))
        if count is not None:
            return count
    return None


def _step_output(step: Any) -> Any:
    output = getattr(step, "output", None)
    if isinstance(output, str):
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return output
    return output


def _step_count(steps: list[Any], keys: tuple[str, ...]) -> int:
    total = 0
    for step in steps:
        output = _step_output(step)
        if not isinstance(output, dict):
            continue
        count = _metadata_count(output, keys)
        if count is not None:
            total += count
    return total


def trajectory_stats(run: Any, steps: list[Any]) -> TrajectoryStats:
    """Extract BP.M.2 trigger stats from workflow metadata + steps."""

    metadata = getattr(run, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        metadata = {}

    tool_calls = _metadata_count(
        metadata,
        ("tool_calls", "tool_call_count", "total_tool_calls"),
    )
    if tool_calls is None:
        tool_calls = _step_count(
            steps,
            ("tool_calls", "tool_call_count", "total_tool_calls"),
        )

    iterations = _metadata_count(
        metadata,
        ("iterations", "iteration_count", "total_iterations"),
    )
    if iterations is None:
        iterations = len(steps)

    success_meta = metadata.get("success")
    if isinstance(success_meta, bool):
        success = success_meta
    else:
        success = getattr(run, "status", "") == "completed"

    return TrajectoryStats(
        tool_calls=int(tool_calls or 0),
        iterations=int(iterations or 0),
        success=success,
    )


def should_distill(run: Any, steps: list[Any]) -> bool:
    stats = trajectory_stats(run, steps)
    return (
        stats.success
        and (
            stats.tool_calls >= MIN_TOOL_CALLS
            or stats.iterations >= MIN_ITERATIONS
        )
    )


def _tenant_id_for(run: Any, tenant_id: str | None) -> str:
    if tenant_id:
        return tenant_id
    metadata = getattr(run, "metadata", {}) or {}
    if isinstance(metadata, dict) and metadata.get("tenant_id"):
        return str(metadata["tenant_id"])
    return tenant_insert_value()


def _source_task_id_for(run: Any) -> str | None:
    metadata = getattr(run, "metadata", {}) or {}
    if isinstance(metadata, dict):
        for key in ("task_id", "source_task_id"):
            if metadata.get(key):
                return str(metadata[key])
    task_id = getattr(run, "task_id", None)
    return str(task_id) if task_id else None


def _step_summary_lines(steps: list[Any], *, limit: int = 12) -> list[str]:
    lines: list[str] = []
    for step in steps[:limit]:
        key = getattr(step, "idempotency_key", "") or getattr(step, "id", "step")
        error = (getattr(step, "error", "") or "").strip()
        output = _step_output(step)
        detail = ""
        if error:
            detail = "failed: " + " ".join(error.splitlines())[:160]
        elif isinstance(output, dict):
            detail = str(
                output.get("summary")
                or output.get("status")
                or output.get("message")
                or ""
            )[:160]
        elif isinstance(output, str):
            detail = output.strip().splitlines()[0][:160] if output.strip() else ""
        suffix = f" -- {detail}" if detail else ""
        lines.append(f"- `{key}`{suffix}")
    if len(steps) > limit:
        lines.append(f"- ... {len(steps) - limit} additional trajectory step(s) omitted")
    return lines


def build_markdown(
    run: Any,
    steps: list[Any],
    *,
    stats: TrajectoryStats | None = None,
    tenant_id: str | None = None,
) -> tuple[str, str]:
    """Return ``(skill_name, markdown)`` for an eligible trajectory."""

    stats = stats or trajectory_stats(run, steps)
    kind = getattr(run, "kind", "") or "workflow"
    source_task_id = _source_task_id_for(run) or ""
    source_run_id = getattr(run, "id", "") or ""
    tid = _tenant_id_for(run, tenant_id)
    skill_name = f"auto-{_slugify(kind)}"
    created_at = int(time.time())

    step_lines = _step_summary_lines(steps)
    if not step_lines:
        step_lines = ["- _(trajectory steps unavailable)_"]

    frontmatter = [
        "---",
        f"name: {skill_name}",
        'description: "Auto-distilled draft from a successful Architect Guild trajectory."',
        "status: draft",
        "source: architect_guild",
        f"source_task_id: {source_task_id!r}",
        f"source_workflow_run_id: {source_run_id!r}",
        f"tenant_id: {tid!r}",
        f"tool_call_count: {stats.tool_calls}",
        f"iteration_count: {stats.iterations}",
        f"created_at: {created_at}",
        "---",
        "",
    ]
    body = [
        f"# Skill: {kind}",
        "",
        "## When To Use",
        "",
        (
            "- Use this draft when a similar workflow shows high tool-call "
            "or iteration pressure and still reaches a successful outcome."
        ),
        "",
        "## Trigger Evidence",
        "",
        f"- source task: `{source_task_id or 'unknown'}`",
        f"- source workflow run: `{source_run_id or 'unknown'}`",
        f"- tool calls: `{stats.tool_calls}`",
        f"- iterations: `{stats.iterations}`",
        "- success: `true`",
        "",
        "## Trajectory Summary",
        "",
        *step_lines,
        "",
        "## Draft Procedure",
        "",
        "1. Recreate the relevant setup and inputs from the source task.",
        "2. Apply the successful trajectory steps in order, adapting names and paths.",
        "3. Re-run the same verification step that made the source task successful.",
        "",
        "## Human Review Notes",
        "",
        (
            "This row is isolated from production skill packs. Review the "
            "procedure, remove task-specific details, then use the BP.M.3 "
            "review/promote flow when it lands."
        ),
        "",
    ]
    return skill_name, "\n".join(frontmatter + body)


_WHEN_HEADING = "## When To Use"
_PROCEDURE_HEADING = "## Draft Procedure"
_TRAJECTORY_HEADING = "## Trajectory Summary"
_NEXT_HEADING_PREFIX = "## "


def _extract_section(markdown: str, heading: str) -> list[str]:
    """Return the non-empty lines inside a ``## Heading`` section
    (until the next ``## Heading`` or end-of-doc)."""
    lines = markdown.splitlines()
    try:
        start = lines.index(heading)
    except ValueError:
        return []
    out: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith(_NEXT_HEADING_PREFIX):
            break
        stripped = line.strip()
        if stripped:
            out.append(stripped)
    return out


_BULLET_RE = re.compile(r"^-\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\d+\.\s+(.*)$")


def _strip_bullet(line: str) -> str | None:
    m = _BULLET_RE.match(line)
    if m:
        return m.group(1).strip()
    return None


def _strip_numbered(line: str) -> str | None:
    m = _NUMBERED_RE.match(line)
    if m:
        return m.group(1).strip()
    return None


def _draft_to_record(
    markdown: str, run: Any, steps: list[Any]
) -> dict[str, Any] | None:
    """Parse the distiller's OWN fixed markdown template into an A2
    typed-record payload. Deterministic (scrub rewrites only secret
    spans, not headings). Returns ``None`` when the template didn't
    match — the caller skips + logs, never a malformed submit.

    Grammar bans backticks in ``evidence_references``, so this helper
    NEVER puts the backticked Trigger Evidence bullets there (they use
    ``source task: `id``` etc.).
    """
    when_lines = _extract_section(markdown, _WHEN_HEADING)
    procedure_lines = _extract_section(markdown, _PROCEDURE_HEADING)
    trajectory_lines = _extract_section(markdown, _TRAJECTORY_HEADING)

    scope_bullets = [
        s for s in (_strip_bullet(line) for line in when_lines) if s
    ]
    scope = scope_bullets[0] if scope_bullets else ""

    procedure_steps: list[str] = []
    for line in procedure_lines:
        step = _strip_numbered(line) or _strip_bullet(line)
        if step:
            procedure_steps.append(step)

    preconditions: list[str] = []
    for line in trajectory_lines[:16]:
        item = _strip_bullet(line)
        if item and item.startswith("_(") is False:
            preconditions.append(item)

    if not scope.strip() or not procedure_steps:
        return None

    return {
        "scope": scope,
        "preconditions": preconditions[:16],
        "procedure_steps": procedure_steps[:16],
        "verification": "",
        "known_failures": [],
        "prohibited_actions": [],
        "evidence_references": [],
    }


async def _emit_distillation_audit(
    draft: DistilledSkillDraft,
    *,
    stats: TrajectoryStats,
) -> None:
    """Best-effort Phase D traceability row for a quarantined version.

    Retargeted from ``auto_distilled_skill`` to ``learned_item_version``
    per U4-I: the ``draft.id`` is now the substrate version id.
    """

    saved = current_tenant_id()
    try:
        set_tenant_id(draft.tenant_id)
        try:
            from backend import audit as _audit
            await _audit.log(
                action="skill_distilled",
                entity_kind="learned_item_version",
                entity_id=draft.id,
                before=None,
                after={
                    "id": draft.id,
                    "tenant_id": draft.tenant_id,
                    "skill_name": draft.skill_name,
                    "source_task_id": draft.source_task_id,
                    "version": draft.version,
                    "status": draft.status,
                    "markdown_sha256": _markdown_sha256(draft.markdown_content),
                    "tool_calls": stats.tool_calls,
                    "iterations": stats.iterations,
                    "success": stats.success,
                },
                actor="system:skill-distiller",
            )
        except Exception as exc:  # pragma: no cover — audit.log swallows
            logger.debug("audit log for skill_distilled failed: %s", exc)
    finally:
        set_tenant_id(saved)


def _evidence_from_run(run: Any) -> tuple:
    """Best-effort raw lookup data assembly from the run's own context.

    Only fires when the caller's run metadata already carries a
    ``change_ref`` — live Gerrit/JIRA fetch wiring is U4-J's, so the
    distiller passes the injected raw shape through unchanged (empty
    ``gerrit_change`` / ``jira_labels`` if the metadata omits them).
    """
    metadata = getattr(run, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        return ()
    change_ref = metadata.get("change_ref") or metadata.get("gerrit_change_ref")
    if not change_ref:
        return ()
    return (
        {
            "change_ref": change_ref,
            "gerrit_change": metadata.get("gerrit_change") or {},
            "jira_labels": metadata.get("jira_labels") or [],
        },
    )


async def _resolve_conn(conn: Any | None) -> tuple[Any, Any]:
    """Return ``(conn, releaser)``: either the caller's conn (releaser
    is a no-op) or a pool-acquired conn (releaser closes it). Mirrors
    the historic ``_insert_draft`` fallback shape."""
    if conn is not None:
        async def _noop():  # pragma: no cover — trivial
            return None
        return conn, _noop
    from backend.db_pool import get_pool
    pool = get_pool()
    acquired = await pool.acquire()

    async def _release():
        await pool.release(acquired)

    return acquired, _release


async def distill(
    run: Any,
    steps: list[Any],
    *,
    tenant_id: str | None = None,
    conn: Any | None = None,
) -> SkillDistillationResult:
    """Summarize an eligible trajectory and submit it as a quarantined
    version row (U4-I)."""

    stats = trajectory_stats(run, steps)
    if not (
        stats.success
        and (
            stats.tool_calls >= MIN_TOOL_CALLS
            or stats.iterations >= MIN_ITERATIONS
        )
    ):
        return SkillDistillationResult(
            written=False,
            draft=None,
            stats=stats,
            hits=Counter(),
            skipped_reason=(
                "below threshold "
                f"(tool_calls={stats.tool_calls}, iterations={stats.iterations}, "
                f"success={stats.success})"
            ),
        )

    skill_name, raw = build_markdown(
        run,
        steps,
        stats=stats,
        tenant_id=tenant_id,
    )
    markdown, hits = scrub(raw)
    if not is_safe_to_promote(hits):
        return SkillDistillationResult(
            written=False,
            draft=None,
            stats=stats,
            hits=hits,
            skipped_reason=f"too many secret hits ({sum(hits.values())})",
        )

    payload = _draft_to_record(markdown, run, steps)
    if payload is None:
        logger.info(
            "skill_distiller: template did not parse — skipping submit for "
            "run=%s",
            getattr(run, "id", "?"),
        )
        return SkillDistillationResult(
            written=False,
            draft=None,
            stats=stats,
            hits=hits,
            skipped_reason="template_unparseable",
        )

    tid = _tenant_id_for(run, tenant_id)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    evidence = _evidence_from_run(run)

    owned_conn, release = await _resolve_conn(conn)
    try:
        try:
            result = await submit_quarantined_version(
                owned_conn,
                payload=payload,
                kind="skill",
                audience="tenant",
                tenant_id=tid,
                created_by="skill_distiller",
                name=skill_name,
                description="Auto-distilled from a successful Architect Guild trajectory.",
                trigger_condition="",
                keywords=[],
                delivery_mode="retrieved",
                evidence=evidence,
                now=now,
            )
        except Exception as exc:
            logger.warning(
                "skill_distiller submit failed for run=%s: %s",
                getattr(run, "id", "?"),
                exc,
            )
            return SkillDistillationResult(
                written=False,
                draft=None,
                stats=stats,
                hits=hits,
                skipped_reason=f"submit_failed: {exc}",
            )
    finally:
        await release()

    draft = DistilledSkillDraft(
        id=result.version_id,
        tenant_id=tid,
        skill_name=skill_name,
        source_task_id=_source_task_id_for(run),
        markdown_content=markdown,
    )
    await _emit_distillation_audit(draft, stats=stats)
    return SkillDistillationResult(
        written=True,
        draft=draft,
        stats=stats,
        hits=hits,
    )


async def architect_guild_hook(
    run: Any,
    steps: list[Any],
    *,
    tenant_id: str | None = None,
    conn: Any | None = None,
) -> SkillDistillationResult:
    """Best-effort hook for successful Architect Guild trajectories."""

    stats = trajectory_stats(run, steps)
    if not is_enabled():
        return SkillDistillationResult(
            written=False,
            draft=None,
            stats=stats,
            hits=Counter(),
            skipped_reason="disabled",
        )
    return await distill(run, steps, tenant_id=tenant_id, conn=conn)


__all__ = [
    "DistilledSkillDraft",
    "MIN_ITERATIONS",
    "MIN_TOOL_CALLS",
    "SkillDistillationResult",
    "TrajectoryStats",
    "architect_guild_hook",
    "build_markdown",
    "distill",
    "is_enabled",
    "should_distill",
    "trajectory_stats",
]
