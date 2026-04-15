"""C4 — L4-CORE-03 Embedded product planner agent (#213).

Deterministic DAG generator for ``embedded_product`` project class.
Takes a HardwareProfile, ParsedSpec, and a skill pack name, then
produces a complete DAG covering BSP → kernel → drivers → protocol
→ app → UI → OTA → tests → docs.

The planner reads the skill pack's ``tasks.yaml`` as a template,
evaluates per-task ``when:`` conditions against the hardware profile
to decide which tasks to include, resolves inter-task dependencies
via topological sort, and emits a valid ``DAG`` object that passes
``dag_validator.validate()``.

    dag = plan_embedded_product(spec, hw_profile, "npu-detection")
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Optional

import yaml

from backend.dag_schema import DAG, Task
from backend.hardware_profile import HardwareProfile
from backend.intent_parser import ParsedSpec

logger = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "configs" / "skills"
_EMBEDDED_BASE = "_embedded_base"

_TASKS_CACHE: dict[str, list[dict]] = {}


def _load_tasks_yaml(skill_pack: str) -> list[dict]:
    if skill_pack in _TASKS_CACHE:
        return _TASKS_CACHE[skill_pack]

    path = _SKILLS_DIR / skill_pack / "tasks.yaml"
    if not path.exists():
        path = _SKILLS_DIR / _EMBEDDED_BASE / "tasks.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No tasks.yaml found for skill pack {skill_pack!r} "
            f"or base template at {path}"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    tasks = data.get("tasks") or []
    _TASKS_CACHE[skill_pack] = tasks
    return tasks


def reload_tasks_cache() -> None:
    _TASKS_CACHE.clear()


def _evaluate_conditions(
    conditions: dict[str, Any],
    hw: HardwareProfile,
) -> bool:
    """Evaluate a task's ``when:`` block against the hardware profile.

    Supported keys:
      has_sensor   — True when hw.sensor is non-empty
      has_npu      — True when hw.npu is non-empty
      has_codec    — True when hw.codec is non-empty
      has_display  — True when hw.display is non-empty
      has_usb      — True when hw.usb is non-empty
      has_peripherals — True when hw.peripherals is non-empty
      soc_contains — True when hw.soc contains the substring (case-insensitive)
    """
    for key, expected in conditions.items():
        if key == "has_sensor":
            if bool(hw.sensor) != expected:
                return False
        elif key == "has_npu":
            if bool(hw.npu) != expected:
                return False
        elif key == "has_codec":
            if bool(hw.codec) != expected:
                return False
        elif key == "has_display":
            if bool(hw.display) != expected:
                return False
        elif key == "has_usb":
            if bool(hw.usb) != expected:
                return False
        elif key == "has_peripherals":
            if bool(hw.peripherals) != expected:
                return False
        elif key == "soc_contains":
            if expected.lower() not in hw.soc.lower():
                return False
        else:
            logger.debug("unknown condition key %r — skipping", key)
    return True


def _filter_tasks(
    templates: list[dict],
    hw: HardwareProfile,
) -> list[dict]:
    """Return only the tasks whose ``when:`` conditions pass."""
    result: list[dict] = []
    for tmpl in templates:
        when = tmpl.get("when")
        if when is None or _evaluate_conditions(when, hw):
            result.append(tmpl)
    return result


def _resolve_dependencies(tasks: list[dict]) -> list[dict]:
    """Topological sort (Kahn's algorithm) + prune dangling deps.

    If a task depends on another task that was filtered out by
    conditions, the dependency is silently removed (the driver it
    depended on isn't needed). Raises ValueError on cycles.
    """
    task_ids = {t["task_id"] for t in tasks}
    by_id = {t["task_id"]: t for t in tasks}

    for t in tasks:
        t["depends_on"] = [d for d in (t.get("depends_on") or []) if d in task_ids]
        t["inputs"] = [
            inp for inp in (t.get("inputs") or [])
            if inp.startswith("external:") or inp.startswith("user:")
            or any(inp == by_id[tid]["expected_output"] for tid in task_ids if tid in by_id)
        ]

    indeg: dict[str, int] = {t["task_id"]: 0 for t in tasks}
    children: dict[str, list[str]] = {t["task_id"]: [] for t in tasks}
    for t in tasks:
        for d in t["depends_on"]:
            children[d].append(t["task_id"])
            indeg[t["task_id"]] += 1

    queue: deque[str] = deque(tid for tid, deg in indeg.items() if deg == 0)
    ordered: list[str] = []
    while queue:
        n = queue.popleft()
        ordered.append(n)
        for child in children[n]:
            indeg[child] -= 1
            if indeg[child] == 0:
                queue.append(child)

    if len(ordered) != len(tasks):
        unresolved = [tid for tid, deg in indeg.items() if deg > 0]
        raise ValueError(
            f"Cyclic dependency detected in embedded plan: {unresolved}"
        )

    return [by_id[tid] for tid in ordered]


def _template_to_task(tmpl: dict) -> Task:
    """Convert a tasks.yaml entry to a DAG Task object."""
    return Task(
        task_id=tmpl["task_id"],
        description=tmpl.get("description", ""),
        required_tier=tmpl.get("required_tier", "t1"),
        toolchain=tmpl.get("toolchain", "cmake"),
        inputs=tmpl.get("inputs") or [],
        expected_output=tmpl["expected_output"],
        depends_on=tmpl.get("depends_on") or [],
    )


def plan_embedded_product(
    spec: ParsedSpec,
    hw: HardwareProfile,
    skill_pack: str = "",
    *,
    dag_id: Optional[str] = None,
) -> DAG:
    """Generate a complete embedded product DAG.

    Parameters
    ----------
    spec : ParsedSpec
        The structured product intent.
    hw : HardwareProfile
        Target hardware capabilities.
    skill_pack : str
        Skill pack name under ``configs/skills/``. Falls back to
        ``_embedded_base`` if the pack has no ``tasks.yaml``.
    dag_id : str, optional
        Override for the DAG ID. Auto-generated if omitted.

    Returns
    -------
    DAG
        A validated DAG with tasks ordered by dependency.
    """
    pack = skill_pack or _EMBEDDED_BASE
    templates = _load_tasks_yaml(pack)

    filtered = _filter_tasks(templates, hw)

    ordered = _resolve_dependencies(filtered)

    tasks = [_template_to_task(t) for t in ordered]

    if not dag_id:
        soc_slug = hw.soc.replace(" ", "-").lower()[:20] if hw.soc else "generic"
        dag_id = f"embedded-{soc_slug}-{uuid.uuid4().hex[:8]}"

    return DAG(
        schema_version=1,
        dag_id=dag_id,
        total_tasks=len(tasks),
        tasks=tasks,
    )


def get_task_count_by_phase(dag: DAG) -> dict[str, int]:
    """Group tasks by phase prefix for topology inspection."""
    phases: dict[str, int] = {}
    for t in dag.tasks:
        prefix = t.task_id.split("-")[0]
        phases[prefix] = phases.get(prefix, 0) + 1
    return phases


def get_dependency_depth(dag: DAG) -> int:
    """Return the longest dependency chain length (critical path)."""
    by_id = {t.task_id: t for t in dag.tasks}
    depth_cache: dict[str, int] = {}

    def _depth(tid: str) -> int:
        if tid in depth_cache:
            return depth_cache[tid]
        task = by_id.get(tid)
        if not task or not task.depends_on:
            depth_cache[tid] = 0
            return 0
        d = 1 + max(_depth(dep) for dep in task.depends_on)
        depth_cache[tid] = d
        return d

    if not dag.tasks:
        return 0
    return max(_depth(t.task_id) for t in dag.tasks)
