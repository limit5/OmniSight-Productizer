"""1F.P1 — system-of-systems product planner (OP-1826).

A planner layer *above* :mod:`backend.embedded_planner`. Where the
embedded planner turns ONE skill pack into a sub-DAG, this module merges
SEVERAL packs' sub-DAGs into ONE product DAG.

P1 scope is MERGE + NAMESPACING only:

  1. For each pack, call
     ``embedded_planner.plan_embedded_product(spec, hw, skill_pack=pack)``
     to get that pack's sub-DAG.
  2. Namespace every task pack-scoped so ids/outputs cannot collide
     across packs: ``task_id`` is prefixed ``<pack>__`` and
     ``expected_output`` is prefixed ``<pack>/``. Intra-pack references
     (``depends_on`` task ids and ``inputs`` that point at a sibling's
     output) are rewritten to the namespaced names so the merged graph
     stays internally consistent. ``external:`` / ``user:`` inputs pass
     through untouched.
  3. Concatenate the namespaced sub-DAGs into one :class:`DAG` and run
     the existing ``dag_validator.validate`` over it.

P1 deliberately adds NO cross-pack edges. Wiring one pack's ``provides``
to another pack's ``requires`` is P2; toolchain reconciliation is P3;
product test / HIL orchestration is P4. This module only consumes
``embedded_planner`` — it never modifies it.

    dag = compose_product(spec, hw, ["imaging", "connectivity"])

Note: the design doc referenced by OP-1826
(``docs/operations/2026-05-28-1F-system-of-systems-planner-design.md``)
was not present in the tree at implementation time, so the namespace
format below is the locally chosen convention. ``__`` is the task-id
separator and ``/`` is the output separator; neither token appears in
the current pack names, so the prefix is injective (a namespaced id/
output maps back to exactly one ``(pack, original)`` pair).
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from backend import embedded_planner
from backend.dag_schema import DAG, Task
from backend.dag_validator import validate
from backend.hardware_profile import HardwareProfile
from backend.intent_parser import ParsedSpec

logger = logging.getLogger(__name__)

#: Separator between a pack name and the original ``task_id``.
TASK_ID_SEP = "__"
#: Separator between a pack name and the original ``expected_output``.
OUTPUT_SEP = "/"


class ProductCompositionError(ValueError):
    """Raised when the merged product DAG fails semantic validation."""


def _ns_task_id(pack: str, task_id: str) -> str:
    return f"{pack}{TASK_ID_SEP}{task_id}"


def _ns_output(pack: str, output: str) -> str:
    return f"{pack}{OUTPUT_SEP}{output}"


def _namespace_subdag(pack: str, sub: DAG) -> list[Task]:
    """Return ``sub``'s tasks with every name pack-scoped.

    ``task_id`` and ``expected_output`` get the pack prefix; ``depends_on``
    and intra-pack ``inputs`` are rewritten through the same maps so the
    sub-DAG's internal edges survive the rename. Cross-pack edges do not
    exist in P1, so every ``depends_on`` entry resolves within ``sub``.
    """
    id_map = {t.task_id: _ns_task_id(pack, t.task_id) for t in sub.tasks}
    out_map = {t.expected_output: _ns_output(pack, t.expected_output)
               for t in sub.tasks}

    namespaced: list[Task] = []
    for t in sub.tasks:
        namespaced.append(Task(
            task_id=id_map[t.task_id],
            description=t.description,
            required_tier=t.required_tier,
            toolchain=t.toolchain,
            # An input is either a sibling's output (namespace it) or an
            # external:/user: token (leave it alone).
            inputs=[out_map.get(inp, inp) for inp in t.inputs],
            expected_output=out_map[t.expected_output],
            depends_on=[id_map[d] for d in t.depends_on],
            output_overlap_ack=t.output_overlap_ack,
        ))
    return namespaced


def compose_product(
    spec: ParsedSpec,
    hw: HardwareProfile,
    packs: list[str],
    *,
    dag_id: Optional[str] = None,
) -> DAG:
    """Merge several skill packs' sub-DAGs into one product DAG.

    Parameters
    ----------
    spec, hw :
        Forwarded unchanged to ``plan_embedded_product`` for each pack.
    packs :
        Skill pack names to compose. Must be non-empty.
    dag_id : str, optional
        Override for the product DAG id. Auto-generated if omitted.

    Returns
    -------
    DAG
        The union of each pack's sub-DAG with pack-namespaced
        ``task_id`` / ``expected_output``, validated by
        ``dag_validator.validate``.

    Raises
    ------
    ValueError
        If ``packs`` is empty.
    ProductCompositionError
        If the merged DAG fails semantic validation.
    """
    if not packs:
        raise ValueError("compose_product requires at least one pack")

    tasks: list[Task] = []
    for pack in packs:
        sub = embedded_planner.plan_embedded_product(spec, hw, skill_pack=pack)
        tasks.extend(_namespace_subdag(pack, sub))

    if not dag_id:
        dag_id = f"product-{uuid.uuid4().hex[:12]}"

    merged = DAG(
        schema_version=1,
        dag_id=dag_id,
        total_tasks=len(tasks),
        tasks=tasks,
    )

    result = validate(merged)
    if not result.ok:
        raise ProductCompositionError(
            f"composed product DAG {dag_id!r} failed validation: "
            f"{result.summary()}"
        )

    logger.info(
        "composed product DAG %s from %d pack(s): %s tasks",
        dag_id, len(packs), len(tasks),
    )
    return merged
