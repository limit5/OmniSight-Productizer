"""BP.M.2 -- Architect Guild trajectory -> skill draft distiller.

This is the runtime companion to BP.M.1's ``auto_distilled_skills``
review queue.  A successful trajectory is eligible when it crossed the
Blueprint Phase M difficulty gate:

    (tool_calls > 5 OR iterations > 3) AND success == true

The distiller deliberately writes ``draft`` rows only.  BP.M.3 owns the
REST review/promote surface and BP.M.5 owns audit_log traceability.

Module-global / cross-worker state audit (SOP Step 1)
----------------------------------------------------
Only immutable thresholds and compiled regex live at module scope.  The
draft row is written to the database, which is the cross-worker source
of truth; no process-local mutable cache participates in decisions.

Read-after-write timing audit (SOP Step 1)
-----------------------------------------
This module adds a new best-effort insert after a workflow has already
finished successfully.  It does not parallelise an existing write path
or change workflow status commit ordering; readers observe drafts after
the DB insert commits.
"""

from __future__ import annotations

import json
import logging
import os
import re
import hashlib
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any

from backend.db_context import current_tenant_id, set_tenant_id, tenant_insert_value
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
    """Inserted ``auto_distilled_skills`` draft row."""

    id: str
    tenant_id: str
    skill_name: str
    source_task_id: str | None
    markdown_content: str
    version: int = 1
    status: str = "draft"


@dataclass(frozen=True)
class SkillDistillationResult:
    """Result from ``distill`` / ``architect_guild_hook``."""

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
    draft creation.  The review/promote gate remains human-owned.
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


def _row_to_draft(row: Any, *, fallback: DistilledSkillDraft) -> DistilledSkillDraft:
    if row is None:
        return fallback
    return DistilledSkillDraft(
        id=row["id"],
        tenant_id=row["tenant_id"],
        skill_name=row["skill_name"],
        source_task_id=row["source_task_id"],
        markdown_content=row["markdown_content"],
        version=int(row["version"]),
        status=row["status"],
    )


async def _insert_draft(
    draft: DistilledSkillDraft,
    *,
    conn: Any | None = None,
) -> DistilledSkillDraft:
    sql = (
        "INSERT INTO auto_distilled_skills ("
        "id, tenant_id, skill_name, source_task_id, markdown_content, "
        "version, status"
        ") VALUES ($1, $2, $3, $4, $5, $6, $7) "
        "RETURNING id, tenant_id, skill_name, source_task_id, "
        "markdown_content, version, status"
    )
    params = (
        draft.id,
        draft.tenant_id,
        draft.skill_name,
        draft.source_task_id,
        draft.markdown_content,
        draft.version,
        draft.status,
    )
    if conn is not None:
        row = await conn.fetchrow(sql, *params)
        return _row_to_draft(row, fallback=draft)

    from backend.db_pool import get_pool

    async with get_pool().acquire() as owned:
        row = await owned.fetchrow(sql, *params)
    return _row_to_draft(row, fallback=draft)


async def _emit_distillation_audit(
    draft: DistilledSkillDraft,
    *,
    stats: TrajectoryStats,
) -> None:
    """Best-effort Phase D traceability row for a distilled draft.

    Tenant context is temporarily set from the durable draft row so
    audit.log writes the same tenant chain in every worker/process.
    """

    saved = current_tenant_id()
    try:
        set_tenant_id(draft.tenant_id)
        try:
            from backend import audit as _audit
            await _audit.log(
                action="skill_distilled",
                entity_kind="auto_distilled_skill",
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


async def distill(
    run: Any,
    steps: list[Any],
    *,
    tenant_id: str | None = None,
    conn: Any | None = None,
) -> SkillDistillationResult:
    """Summarize an eligible trajectory and insert a draft row."""

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

    draft = DistilledSkillDraft(
        id=f"ads-{uuid.uuid4().hex[:12]}",
        tenant_id=_tenant_id_for(run, tenant_id),
        skill_name=skill_name,
        source_task_id=_source_task_id_for(run),
        markdown_content=markdown,
    )
    inserted = await _insert_draft(draft, conn=conn)
    await _emit_distillation_audit(inserted, stats=stats)
    return SkillDistillationResult(
        written=True,
        draft=inserted,
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
