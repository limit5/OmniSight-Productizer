"""1F.P1+P2a — system-of-systems product planner (OP-1826, OP-1827).

A planner layer *above* :mod:`backend.embedded_planner`. Where the
embedded planner turns ONE skill pack into a sub-DAG, this module merges
SEVERAL packs' sub-DAGs into ONE product DAG.

P1 (OP-1826) scope was MERGE + NAMESPACING only:

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

P2a (OP-1827) adds the cross-pack WIRING MECHANISM, run after the P1
merge+namespacing and before validation:

  4. Read each pack's free-form ``provides`` / ``requires`` tokens from
     its :class:`~backend.skill_manifest.SkillManifest` (or from an
     injected ``manifests`` override — used by the synthetic tests so the
     mechanism can be exercised without declaring tokens on real packs,
     which is P2b). For every ``requires`` token of pack P, find the pack
     that ``provides`` it and add cross-pack dependency edges so P depends
     on the producer: each ROOT task of P (in-degree 0 within its own
     namespaced sub-DAG) gains a ``depends_on`` edge to every SINK task of
     the producing pack (out-degree 0). A pack-level capability is only
     ready once the producing pack's terminal tasks complete, and the
     requiring pack must not begin its entry tasks until then. The
     existing validator's cycle / unknown_dep rules catch any pathological
     wiring (e.g. a mutual requires cycle) — we add edges, never inputs,
     so ``dep_closure`` is unaffected.

  Fail-loud / surface contract (design §3, §6):
    * **Duplicate provides** — a token listed in ≥2 *distinct* packs'
      ``provides`` raises :class:`DuplicateProvideError` (a
      ``ProductCompositionError``) before any DAG is built.
    * **Unmet requires** — a ``requires`` token that no pack provides and
      that is not an ``external:`` / ``user:`` token is never silently
      dropped: it is collected and logged as structured ``unmet_requires``
      (the OP-1774 unmet-deps pattern, mirrored from
      ``embedded_planner._resolve_dependencies``).

P3 (OP-1830) adds SoC-compatibility RECONCILIATION, run on the merged
composition. Each pack declares a ``compatible_socs`` list on its
:class:`~backend.skill_manifest.SkillManifest` (empty = SoC-agnostic, no
constraint). The product target SoC is ``hw.soc``. A pack whose NON-EMPTY
``compatible_socs`` does not contain the target SoC — compared
case-insensitively, matching the embedded planner's ``soc_contains``
case-folding (``embedded_planner.py``) — cannot run on the product
hardware, so (per design §7 decision-2: error-on-conflict, mirroring
duplicate provides rather than silently dropping)
:func:`compose_product` raises :class:`SocIncompatibilityError` naming the
offending pack(s), their ``compatible_socs``, and the target SoC. The
product's compatible-SoC envelope — the intersection of every NON-EMPTY
list — is computed and logged for downstream consumers.

Product test / HIL orchestration is P4 — out of scope here. This module
only consumes ``embedded_planner`` and reads ``skill_registry`` — it
never modifies either.

    dag = compose_product(spec, hw, ["imaging", "connectivity"])

Note: the design doc referenced by OP-1826/OP-1827
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
from dataclasses import dataclass, field
from typing import Mapping, Optional

from backend import embedded_planner
from backend.dag_schema import DAG, Task
from backend.dag_validator import validate
from backend.hardware_profile import HardwareProfile
from backend.intent_parser import ParsedSpec
from backend.skill_manifest import SkillManifest

logger = logging.getLogger(__name__)

#: Separator between a pack name and the original ``task_id``.
TASK_ID_SEP = "__"
#: Separator between a pack name and the original ``expected_output``.
OUTPUT_SEP = "/"

#: Prefixes a ``requires`` token may carry to mark it as caller-satisfied
#: (not produced by any pack). Mirrors the embedded planner's input
#: exemption so an ``external:``/``user:`` requirement is neither wired
#: nor surfaced as unmet.
_CALLER_SATISFIED_PREFIXES = ("external:", "user:")


class ProductCompositionError(ValueError):
    """Raised when the merged product DAG fails semantic validation."""


class DuplicateProvideError(ProductCompositionError):
    """Raised when ≥2 distinct packs ``provides`` the same token.

    Cross-pack wiring is ambiguous when two packs claim to provide the
    same capability — there is no single producer to wire a requiring
    pack to — so :func:`compose_product` fails loud before building any
    DAG. :attr:`conflicts` maps each duplicated token to the sorted list
    of pack names that provide it.
    """

    def __init__(self, conflicts: dict[str, list[str]]):
        self.conflicts = conflicts
        detail = "; ".join(
            f"{tok!r} provided by {pks}" for tok, pks in sorted(conflicts.items())
        )
        super().__init__(f"duplicate cross-pack provides: {detail}")


class SocIncompatibilityError(ProductCompositionError):
    """Raised when a composed pack cannot run on the product target SoC.

    A pack's NON-EMPTY ``compatible_socs`` that does not contain the
    product's ``hw.soc`` (compared case-insensitively) is a hard hardware
    conflict — that pack's firmware cannot run on the target silicon — so
    :func:`compose_product` fails loud (design §7 decision-2:
    error-on-conflict, like :class:`DuplicateProvideError`) rather than
    composing an unrunnable product. :attr:`conflicts` maps each offending
    pack to its declared ``compatible_socs``; :attr:`target_soc` is the
    product target SoC the packs were reconciled against.
    """

    def __init__(self, target_soc: str, conflicts: dict[str, list[str]]):
        self.target_soc = target_soc
        self.conflicts = conflicts
        detail = "; ".join(
            f"{pack!r} supports {socs} (excludes target {target_soc!r})"
            for pack, socs in sorted(conflicts.items())
        )
        super().__init__(f"SoC-incompatible composed pack(s): {detail}")


def _ns_task_id(pack: str, task_id: str) -> str:
    return f"{pack}{TASK_ID_SEP}{task_id}"


def _ns_output(pack: str, output: str) -> str:
    return f"{pack}{OUTPUT_SEP}{output}"


def _namespace_subdag(
    pack: str,
    sub: DAG,
    *,
    failure_policy: str = "abort",
) -> list[Task]:
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
            on_failure=failure_policy,
        ))
    return namespaced


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P2a (OP-1827) — cross-pack provides/requires wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class _PackPlan:
    """One pack's namespaced sub-DAG plus its cross-pack wiring tokens."""

    pack: str
    tasks: list[Task]
    provides: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    failure_policy: str = "abort"


def _roots(tasks: list[Task]) -> list[Task]:
    """Entry tasks of a namespaced sub-DAG (no intra-pack ``depends_on``).

    Computed *before* cross-pack edges are added, so a task's
    ``depends_on`` still holds only intra-pack ids; an empty list therefore
    means the task is a true pack entry point.
    """
    return [t for t in tasks if not t.depends_on]


def _sinks(tasks: list[Task]) -> list[Task]:
    """Terminal tasks of a sub-DAG (nothing in the same pack depends on)."""
    depended_on: set[str] = set()
    for t in tasks:
        depended_on.update(t.depends_on)
    return [t for t in tasks if t.task_id not in depended_on]


def _pack_provides_requires(
    pack: str,
    manifests: Optional[Mapping[str, SkillManifest]],
) -> tuple[list[str], list[str]]:
    """Resolve a pack's ``(provides, requires)`` tokens.

    When ``manifests`` is supplied it is authoritative (a pack absent from
    it has no tokens) — this keeps the synthetic tests hermetic and lets
    the mechanism be exercised without touching real ``skill.yaml`` files
    (that is P2b). Otherwise the pack's manifest is read from the skill
    registry; a pack with no manifest contributes no tokens, so the real
    path is a no-op until packs actually declare tokens.
    """
    manifest: Optional[SkillManifest]
    if manifests is not None:
        manifest = manifests.get(pack)
    else:
        from backend import skill_registry

        info = skill_registry.get_skill(pack)
        manifest = info.manifest if info is not None else None

    if manifest is None:
        return [], []
    return list(manifest.provides), list(manifest.requires)


def _pack_compatible_socs(
    pack: str,
    manifests: Optional[Mapping[str, SkillManifest]],
) -> list[str]:
    """Resolve a pack's declared ``compatible_socs`` (empty = SoC-agnostic).

    Mirrors :func:`_pack_provides_requires`'s manifest resolution: when
    ``manifests`` is supplied it is authoritative (a pack absent from it is
    SoC-agnostic), otherwise the manifest is read from the skill registry; a
    pack with no manifest contributes no constraint. Kept as a separate
    reader rather than folded into the wiring helper so the P2a wiring path
    is left untouched per this ticket's scope guard.
    """
    manifest: Optional[SkillManifest]
    if manifests is not None:
        manifest = manifests.get(pack)
    else:
        from backend import skill_registry

        info = skill_registry.get_skill(pack)
        manifest = info.manifest if info is not None else None

    return list(manifest.compatible_socs) if manifest is not None else []


def _pack_failure_policy(
    pack: str,
    manifests: Optional[Mapping[str, SkillManifest]],
) -> str:
    """Resolve a pack's product-level failure policy."""
    manifest: Optional[SkillManifest]
    if manifests is not None:
        manifest = manifests.get(pack)
    else:
        from backend import skill_registry

        info = skill_registry.get_skill(pack)
        manifest = info.manifest if info is not None else None

    return manifest.failure_policy if manifest is not None else "abort"


def _product_acceptance_node(plans: list[_PackPlan]) -> Task:
    """Forward-looking product-level test join-node for composed packs."""
    sink_ids = [
        sink.task_id
        for plan in plans
        for sink in _sinks(plan.tasks)
    ]
    sink_outputs = [
        sink.expected_output
        for plan in plans
        for sink in _sinks(plan.tasks)
    ]
    return Task(
        task_id="product__acceptance",
        description="product-level integration test spanning all subsystems",
        required_tier="t1",
        toolchain="cmake",
        inputs=sink_outputs,
        expected_output="build/product-acceptance.json",
        depends_on=list(dict.fromkeys(sink_ids)),
        on_failure="abort",
    )


def _wire_cross_pack(plans: list[_PackPlan]) -> list[dict[str, str]]:
    """Add cross-pack ``requires``→``provides`` edges in place.

    Mutates each requiring pack's root tasks' ``depends_on`` to point at
    the producing pack's sink tasks. Returns the structured
    ``unmet_requires`` list (``{"pack", "token"}`` entries) for any
    ``requires`` token no pack provides and that is not caller-satisfied.

    Raises
    ------
    DuplicateProvideError
        If any token is provided by ≥2 distinct packs.
    """
    # token -> set of distinct providing pack names. A pack listing a
    # token twice, or the same pack name appearing twice in ``packs``,
    # collapses to one entry — the latter is caught later by the
    # validator's duplicate_id rule, not mistaken for a duplicate provide.
    providers: dict[str, set[str]] = {}
    for p in plans:
        for tok in set(p.provides):
            providers.setdefault(tok, set()).add(p.pack)

    conflicts = {
        tok: sorted(pks) for tok, pks in providers.items() if len(pks) > 1
    }
    if conflicts:
        raise DuplicateProvideError(conflicts)

    by_pack: dict[str, _PackPlan] = {p.pack: p for p in plans}
    unmet: list[dict[str, str]] = []

    for p in plans:
        # Snapshot the consumer's roots ONCE, before wiring any cross-pack
        # edge. ``_roots`` keys off an empty ``depends_on``; wiring the first
        # required token onto a root makes its ``depends_on`` non-empty, so a
        # re-computation inside the loop would no longer see it as a root and
        # the second+ required tokens would silently go unwired. Snapshotting
        # honours ``_roots``'s "computed before cross-pack edges" invariant so
        # a multi-require consumer wires ALL its tokens.
        roots = _roots(p.tasks)
        for tok in p.requires:
            if tok.startswith(_CALLER_SATISFIED_PREFIXES):
                continue
            producers = providers.get(tok, set()) - {p.pack}
            if not producers:
                # Self-provided tokens are satisfied in-pack (no edge);
                # everything else is a genuine unmet requirement.
                if tok in set(p.provides):
                    continue
                unmet.append({"pack": p.pack, "token": tok})
                continue

            # Unique by construction (duplicate provides already raised).
            producer = by_pack[next(iter(producers))]
            sinks = _sinks(producer.tasks)
            for r in roots:
                for s in sinks:
                    if s.task_id not in r.depends_on:
                        r.depends_on.append(s.task_id)

    return unmet


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P3 (OP-1830) — SoC-compatibility reconciliation across composed packs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _reconcile_socs(
    target_soc: str,
    socs_by_pack: Mapping[str, list[str]],
) -> set[str]:
    """Reconcile each composed pack's ``compatible_socs`` vs the product SoC.

    ``target_soc`` is the product's ``hw.soc`` (e.g. ``"RK3566"``).
    ``socs_by_pack`` maps each composed pack to its declared
    ``compatible_socs``. An empty list is SoC-agnostic (no constraint). A
    pack whose NON-EMPTY list does not contain ``target_soc`` — compared
    case-insensitively, matching the embedded planner's ``soc_contains``
    case-folding — cannot run on the target silicon and is a fail-loud
    conflict.

    Returns the product's compatible-SoC envelope: the case-folded
    intersection of every NON-EMPTY list (an empty set means no pack pins a
    SoC, i.e. the product is SoC-agnostic).

    Raises
    ------
    SocIncompatibilityError
        If any pack's non-empty ``compatible_socs`` excludes ``target_soc``.
    """
    target = target_soc.lower()
    conflicts: dict[str, list[str]] = {}
    nonempty_folded: list[set[str]] = []
    for pack, socs in socs_by_pack.items():
        if not socs:
            continue  # SoC-agnostic — imposes no constraint
        folded = {s.lower() for s in socs}
        nonempty_folded.append(folded)
        if target not in folded:
            conflicts[pack] = list(socs)

    if conflicts:
        raise SocIncompatibilityError(target_soc, conflicts)

    return set.intersection(*nonempty_folded) if nonempty_folded else set()


def compose_product(
    spec: ParsedSpec,
    hw: HardwareProfile,
    packs: list[str],
    *,
    dag_id: Optional[str] = None,
    manifests: Optional[Mapping[str, SkillManifest]] = None,
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
    manifests : Mapping[str, SkillManifest], optional
        Per-pack manifest override for cross-pack wiring. When omitted,
        each pack's ``provides`` / ``requires`` tokens are read from its
        registry manifest (P2a). Supplying this dict makes wiring hermetic
        — used by the synthetic tests so the mechanism can be exercised
        without declaring tokens on real packs (that is P2b).

    Returns
    -------
    DAG
        The union of each pack's sub-DAG with pack-namespaced
        ``task_id`` / ``expected_output``, cross-pack ``requires``→
        ``provides`` edges wired in, validated by ``dag_validator.validate``.

    Raises
    ------
    ValueError
        If ``packs`` is empty.
    SocIncompatibilityError
        If any pack's non-empty ``compatible_socs`` excludes ``hw.soc``.
    DuplicateProvideError
        If ≥2 distinct packs provide the same token.
    ProductCompositionError
        If the merged DAG fails semantic validation.
    """
    if not packs:
        raise ValueError("compose_product requires at least one pack")

    plans: list[_PackPlan] = []
    socs_by_pack: dict[str, list[str]] = {}
    for pack in packs:
        sub = embedded_planner.plan_embedded_product(spec, hw, skill_pack=pack)
        provides, requires = _pack_provides_requires(pack, manifests)
        failure_policy = _pack_failure_policy(pack, manifests)
        plans.append(_PackPlan(
            pack=pack,
            tasks=_namespace_subdag(pack, sub, failure_policy=failure_policy),
            provides=provides,
            requires=requires,
            failure_policy=failure_policy,
        ))
        socs_by_pack[pack] = _pack_compatible_socs(pack, manifests)

    # P3 (OP-1830) — reconcile each pack's compatible_socs against the
    # product target SoC. Fail loud (design §7 decision-2) before wiring or
    # validation if any composed pack cannot run on hw.soc.
    product_socs = _reconcile_socs(hw.soc, socs_by_pack)
    logger.info(
        "product compatible-SoC envelope for target %r: %s",
        hw.soc, sorted(product_socs) or "(SoC-agnostic)",
    )

    # P2a — wire cross-pack edges, fail loud on duplicate provides, and
    # surface unmet requires (OP-1774-style structured log).
    unmet_requires = _wire_cross_pack(plans)
    if unmet_requires:
        logger.warning(
            "product planner found unmet cross-pack requires",
            extra={"unmet_requires": unmet_requires},
        )

    tasks: list[Task] = [t for p in plans for t in p.tasks]
    tasks.append(_product_acceptance_node(plans))

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
