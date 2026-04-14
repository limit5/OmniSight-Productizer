"""E2E Orchestration Pipeline — automated SPEC→Release flow.

Defines ordered pipeline steps and provides:
  - PIPELINE_STEPS: ordered phase definitions with task templates
  - run_pipeline(): start a full pipeline run from SPEC
  - advance_pipeline(): check phase completion and auto-advance
  - on_task_completed(): event hook for automatic phase progression
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from backend.events import emit_pipeline_phase, emit_invoke

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pipeline step definitions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PIPELINE_STEPS = [
    {
        "id": "spec",
        "name": "SPEC Analysis",
        "npi_phase": "phase-1",
        "tasks": [
            {"title": "Analyze hardware manifest and client spec", "agent_type": "software"},
            {"title": "Generate task breakdown from requirements", "agent_type": "software"},
        ],
        "auto_advance": True,
    },
    {
        "id": "develop",
        "name": "Development",
        "npi_phase": "phase-2",
        "tasks": [
            {"title": "Implement BSP and HAL layer", "agent_type": "firmware", "sub_type": "bsp"},
            {"title": "Implement sensor driver and ISP pipeline", "agent_type": "firmware", "sub_type": "isp"},
            {"title": "Implement application software", "agent_type": "software"},
        ],
        "auto_advance": True,
    },
    {
        "id": "review",
        "name": "Code Review",
        "npi_phase": "phase-3",
        "tasks": [
            {"title": "Review all code changes via Gerrit", "agent_type": "reviewer"},
        ],
        "auto_advance": False,  # Requires human Gerrit +2
        "human_checkpoint": "Gerrit +2 merge required",
    },
    {
        "id": "test",
        "name": "Testing & Validation",
        "npi_phase": "phase-4",
        "tasks": [
            {"title": "Run algo-track simulation and verify coverage", "agent_type": "validator"},
            {"title": "Run hw-track simulation with QEMU", "agent_type": "validator"},
            {"title": "Run NPU model accuracy verification", "agent_type": "validator"},
        ],
        "auto_advance": True,
    },
    {
        "id": "deploy",
        "name": "Hardware Deployment",
        "npi_phase": "phase-5",
        "tasks": [
            {"title": "Cross-compile and deploy to EVK board", "agent_type": "firmware"},
            {"title": "Run on-device hardware verification", "agent_type": "validator"},
        ],
        "auto_advance": False,  # Requires HVT confirmation
        "human_checkpoint": "HVT hardware verification required",
    },
    {
        "id": "package",
        "name": "Release Packaging",
        "npi_phase": "phase-6",
        "tasks": [
            {"title": "Create release bundle with manifest", "agent_type": "software"},
        ],
        "auto_advance": True,
    },
    {
        "id": "docs",
        "name": "Documentation",
        "npi_phase": "phase-7",
        "tasks": [
            {"title": "Generate compliance report (FCC/CE)", "agent_type": "reporter"},
            {"title": "Generate test summary report", "agent_type": "reporter"},
        ],
        "auto_advance": True,
    },
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pipeline state
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_active_pipeline: dict | None = None  # {id, current_step, status, started_at, tasks_created}
_last_completed_pipeline: dict | None = None  # last finished/halted run for UI replay

# Async lock for all mutations of _active_pipeline. FastAPI request handlers
# can be reentrant under concurrent requests; on_task_completed() runs from
# arbitrary background tasks, and force_advance() is user-triggered. Without
# a lock, advance_pipeline()+force_advance() can interleave and double-create
# tasks for the same step.
import asyncio as _asyncio
_pipeline_lock: _asyncio.Lock | None = None


def _get_pipeline_lock() -> _asyncio.Lock:
    """Lazy-init the lock — Lock() must be created inside a running loop."""
    global _pipeline_lock
    if _pipeline_lock is None:
        _pipeline_lock = _asyncio.Lock()
    return _pipeline_lock


def get_pipeline_status() -> dict:
    """Get the current pipeline run status."""
    if not _active_pipeline:
        if _last_completed_pipeline is not None:
            return {
                **_last_completed_pipeline,
                "steps": [s["id"] for s in PIPELINE_STEPS],
                "is_history": True,
            }
        return {"status": "idle", "current_step": "", "steps": [s["id"] for s in PIPELINE_STEPS]}
    return {**_active_pipeline, "steps": [s["id"] for s in PIPELINE_STEPS]}


async def run_pipeline(spec_context: str = "") -> dict:
    """Start a full E2E pipeline run from SPEC to Release.

    Creates tasks for the first phase and begins execution.
    Subsequent phases are triggered automatically via on_task_completed().
    """
    global _active_pipeline

    async with _get_pipeline_lock():
        if _active_pipeline and _active_pipeline.get("status") == "running":
            return {"status": "error", "detail": "Pipeline already running"}

        pipeline_id = f"pipeline-{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat()
        _active_pipeline = {
            "id": pipeline_id,
            "current_step": PIPELINE_STEPS[0]["id"],
            "current_step_index": 0,
            "status": "running",
            "started_at": now,
            "tasks_created": 0,
            "spec_context": spec_context[:500],
            # Phase 50A: per-step timing so the timeline can show real
            # elapsed + compute velocity. Shape: list aligned to
            # PIPELINE_STEPS order; first step opens at run_pipeline().
            "step_history": [
                {"id": s["id"], "started_at": None, "completed_at": None}
                for s in PIPELINE_STEPS
            ],
        }
        _active_pipeline["step_history"][0]["started_at"] = now

        emit_pipeline_phase("pipeline_start", f"Pipeline {pipeline_id} started: {len(PIPELINE_STEPS)} steps")
        emit_invoke("pipeline", f"E2E pipeline started: SPEC → Release ({len(PIPELINE_STEPS)} phases)")

        await _create_tasks_for_step(0, spec_context)

    return get_pipeline_status()


async def advance_pipeline() -> dict:
    """Check if the current step is complete and advance to the next.

    Called after task completions or manual checkpoints. Holds the
    pipeline lock for the entire mutation so concurrent advance / force
    paths do not race.
    """
    global _active_pipeline

    async with _get_pipeline_lock():
        return await _advance_pipeline_locked()


async def _advance_pipeline_locked() -> dict:
    global _active_pipeline
    if not _active_pipeline or _active_pipeline["status"] != "running":
        return {"status": "idle", "detail": "No active pipeline"}

    step_idx = _active_pipeline["current_step_index"]
    step = PIPELINE_STEPS[step_idx]

    # Check if all tasks for this step are complete
    phase_complete = await _check_phase_complete(step["npi_phase"])

    if not phase_complete:
        return {"status": "waiting", "step": step["id"], "detail": "Tasks still in progress"}

    # Human checkpoint?
    if not step.get("auto_advance"):
        checkpoint = step.get("human_checkpoint", "Human approval required")
        emit_pipeline_phase("pipeline_checkpoint", f"Step '{step['name']}' complete — {checkpoint}")
        try:
            from backend.events import emit_token_warning
            emit_token_warning("warn", f"Pipeline checkpoint: {checkpoint}")
        except Exception:
            pass
        return {"status": "checkpoint", "step": step["id"], "detail": checkpoint}

    # Stamp the completing step (Phase 50A).
    now_iso = datetime.now().isoformat()
    hist = _active_pipeline.get("step_history")
    if hist and step_idx < len(hist):
        hist[step_idx]["completed_at"] = now_iso

    # Auto-advance to next step
    next_idx = step_idx + 1
    if next_idx >= len(PIPELINE_STEPS):
        # Pipeline complete — move state to history slot so memory doesn't
        # accumulate across many runs (M17).
        global _last_completed_pipeline
        _active_pipeline["status"] = "completed"
        _active_pipeline["completed_at"] = now_iso
        emit_pipeline_phase("pipeline_complete", f"Pipeline {_active_pipeline['id']} completed!")
        emit_invoke("pipeline", "E2E pipeline completed: all phases done")
        _last_completed_pipeline = _active_pipeline
        _active_pipeline = None
        return {"status": "completed", "detail": "All pipeline steps finished"}

    # Advance
    next_step = PIPELINE_STEPS[next_idx]
    _active_pipeline["current_step"] = next_step["id"]
    _active_pipeline["current_step_index"] = next_idx
    if hist and next_idx < len(hist):
        hist[next_idx]["started_at"] = now_iso

    emit_pipeline_phase("pipeline_advance", f"Advancing to step: {next_step['name']}")

    # Create tasks for the next step
    await _create_tasks_for_step(next_idx, _active_pipeline.get("spec_context", ""))

    return {"status": "advanced", "step": next_step["id"], "detail": f"Now at: {next_step['name']}"}


async def force_advance() -> dict:
    """Force-advance past a human checkpoint (user approved)."""
    global _active_pipeline

    async with _get_pipeline_lock():
        if not _active_pipeline or _active_pipeline["status"] != "running":
            return {"status": "error", "detail": "No active pipeline"}

        step_idx = _active_pipeline["current_step_index"]
        step = PIPELINE_STEPS[step_idx]

        # Audit: log + emit if there are still blocked tasks in the current
        # phase when the user force-advances. This is a conscious override
        # and must leave a trail (compliance + later debugging).
        try:
            from backend.routers.invoke import _tasks
            from backend.models import TaskStatus
            stuck_names = {"blocked", "error", "failed"}
            STUCK = {getattr(TaskStatus, n) for n in stuck_names if hasattr(TaskStatus, n)}
            stuck = [t.id for t in _tasks.values()
                     if getattr(t, "npi_phase_id", None) == step["npi_phase"]
                     and t.status in STUCK]
            if stuck:
                logger.warning(
                    "[FORCE-ADVANCE] User overriding %d stuck tasks in phase %s: %s",
                    len(stuck), step["npi_phase"], stuck[:5],
                )
                emit_pipeline_phase(
                    "pipeline_force_override",
                    f"force_advance with {len(stuck)} stuck task(s) in {step['name']}",
                )
        except Exception as exc:
            logger.debug("force_advance audit failed: %s", exc)

        now_iso = datetime.now().isoformat()
        hist = _active_pipeline.get("step_history")
        if hist and step_idx < len(hist):
            hist[step_idx]["completed_at"] = now_iso

        next_idx = step_idx + 1

        if next_idx >= len(PIPELINE_STEPS):
            global _last_completed_pipeline
            _active_pipeline["status"] = "completed"
            _active_pipeline["completed_at"] = now_iso
            emit_pipeline_phase("pipeline_complete", "Pipeline completed (force-advanced)")
            _last_completed_pipeline = _active_pipeline
            _active_pipeline = None
            return {"status": "completed"}

        next_step = PIPELINE_STEPS[next_idx]
        _active_pipeline["current_step"] = next_step["id"]
        _active_pipeline["current_step_index"] = next_idx
        if hist and next_idx < len(hist):
            hist[next_idx]["started_at"] = now_iso

        emit_pipeline_phase("pipeline_advance", f"Force-advanced to: {next_step['name']}")
        await _create_tasks_for_step(next_idx, _active_pipeline.get("spec_context", ""))

        return {"status": "advanced", "step": next_step["id"]}


async def on_task_completed(task_id: str, npi_phase_id: str | None = None) -> None:
    """Event hook: called when a task is completed. Checks if pipeline should advance."""
    if not _active_pipeline or _active_pipeline["status"] != "running":
        return

    if not npi_phase_id:
        return

    step_idx = _active_pipeline["current_step_index"]
    step = PIPELINE_STEPS[step_idx]

    if npi_phase_id == step["npi_phase"]:
        # A task in the current phase completed — check if whole phase is done
        result = await advance_pipeline()
        logger.info("Pipeline auto-advance check: %s", result)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internal helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _create_tasks_for_step(step_idx: int, spec_context: str = "") -> None:
    """Create tasks for a pipeline step and register them."""
    from backend.models import Task, TaskStatus, TaskPriority
    from backend.routers.invoke import _tasks, _persist_task
    from backend.events import emit_task_update

    step = PIPELINE_STEPS[step_idx]
    npi_phase = step["npi_phase"]

    created = 0
    failed = 0
    for tmpl in step["tasks"]:
        task_id = f"pipe-{step['id']}-{uuid.uuid4().hex[:6]}"
        desc = tmpl["title"]
        if spec_context:
            desc += f"\n\nSpec context: {spec_context[:200]}"
        try:
            task = Task(
                id=task_id,
                title=tmpl["title"],
                description=desc,
                priority=TaskPriority.high,
                status=TaskStatus.backlog,
                suggested_agent_type=tmpl.get("agent_type"),
                suggested_sub_type=tmpl.get("sub_type"),
                npi_phase_id=npi_phase,
            )
            _tasks[task_id] = task
            await _persist_task(task)
            emit_task_update(task_id, "backlog")
            if _active_pipeline:
                _active_pipeline["tasks_created"] = _active_pipeline.get("tasks_created", 0) + 1
            created += 1
        except Exception as exc:
            # Don't let one bad template halt the whole step. Surface the
            # failure to the pipeline panel and keep going so the rest of
            # the phase still has a chance to complete.
            failed += 1
            logger.error("Pipeline step '%s': task %s persist failed: %s",
                         step["name"], task_id, exc)
            emit_pipeline_phase(
                "pipeline_task_create_failed",
                f"{step['name']}: {tmpl.get('title','?')} → {type(exc).__name__}",
            )

    logger.info("Pipeline step '%s': created %d tasks (%d failed)",
                step["name"], created, failed)
    emit_pipeline_phase(
        "pipeline_tasks",
        f"Created {created}/{len(step['tasks'])} tasks for {step['name']}"
        + (f" ({failed} failed)" if failed else ""),
    )


async def _check_phase_complete(npi_phase_id: str) -> bool:
    """Check if all tasks linked to an NPI phase are completed.

    H17 fix: deleted/cancelled tasks no longer count toward phase membership.
    C14 fix: any blocked / error task means the phase is *terminally stuck* —
    we surface a `pipeline_blocked` event so the user can intervene rather
    than hanging silently. The phase is not considered complete in that case;
    the user must `force_advance()` (which now logs the override).
    """
    from backend.routers.invoke import _tasks
    from backend.models import TaskStatus

    # Best-effort filter for terminal/dead states across enum revisions.
    dead_names = {"cancelled", "deleted"}
    DEAD = {getattr(TaskStatus, n) for n in dead_names if hasattr(TaskStatus, n)}
    phase_tasks = [
        t for t in _tasks.values()
        if getattr(t, "npi_phase_id", None) == npi_phase_id and t.status not in DEAD
    ]
    if not phase_tasks:
        return True

    stuck_names = {"blocked", "error", "failed"}
    STUCK = {getattr(TaskStatus, n) for n in stuck_names if hasattr(TaskStatus, n)}
    blocked = [t for t in phase_tasks if t.status in STUCK]
    if blocked:
        # Surface stuck pipeline once per check (idempotent — frontend can dedupe).
        emit_pipeline_phase(
            "pipeline_blocked",
            f"Phase {npi_phase_id} blocked: {len(blocked)} task(s) stuck — manual intervention required",
        )
        return False

    return all(t.status == TaskStatus.completed for t in phase_tasks)
