"""OP-1770 — W1-T1: read-only planner-compat sweep over all skill packs.

Why this test exists
--------------------
``backend/embedded_planner.py`` turns a skill pack's ``tasks.yaml`` into a
DAG. It dereferences two template keys with a hard ``[]`` (not ``.get``):

  * ``_resolve_dependencies`` reads ``t["task_id"]``
  * ``_template_to_task``     reads ``tmpl["task_id"]`` and ``tmpl["expected_output"]``

and ``_evaluate_conditions`` assumes a task's ``when:`` is a mapping. Any
pack whose schema does not satisfy those expectations makes the planner
raise an *uncaught* ``KeyError`` / ``AttributeError`` mid-plan.

The pre-existing ``backend/tests/test_embedded_planner.py`` only ever fed
the planner ``_embedded_base``, so it never noticed that the bulk of the
shipped packs use a *different* (``id``/``name``/``artifacts``) schema that
the embedded planner cannot consume. That blind spot hid a schema BLOCKER.

This module closes the blind spot with a read-only, dynamically
parametrized sweep: for **every** ``configs/skills/*/tasks.yaml`` it asserts
the planner outcome is one of two well-defined classes —

  * ``parsed_ok``                — the planner produced a DAG, or
  * ``classified_incompatible``  — the pack's schema does not meet the
                                   embedded contract and the planner
                                   (predictably) cannot consume it

— and **never** an uncaught exception that escapes the sweep.

Regression guard
----------------
The outcome the planner *actually* produces (``_observed_outcome``, which
swallows the documented schema-mismatch exception family) must match the
outcome the schema contract *predicts* (``_expected_outcome``). So a future
pack that claims the embedded schema (every task carries ``task_id`` +
``expected_output``) but still blows up the planner flips ``observed`` to
``classified_incompatible`` while ``expected`` stays ``parsed_ok`` — the
equality assertion then FAILS. That is the regression guard the schema
BLOCKER lacked.

Scope (per OP-1770 acceptance criteria)
---------------------------------------
Read-only. This test does NOT migrate any pack schema and makes NO
production edits — repairing the incompatible packs is a separate, later
ticket. It only *surfaces and pins* the current compatibility partition.

Spec-ref:
  - docs/product/2026-05-27-customer-delivery-capability-audit-and-roadmap.md §3 (1D)
  - docs/audit/codex-reviews/customer-delivery-roadmap-codex-audit-2026-05-27.txt (points 1, 6)
"""

from __future__ import annotations

from pathlib import Path

import pytest

import backend.embedded_planner as ep
from backend.embedded_planner import (
    _template_to_task,
    plan_embedded_product,
    reload_tasks_cache,
)
from backend.hardware_profile import HardwareProfile, Peripheral
from backend.intent_parser import Field as SpecField
from backend.intent_parser import ParsedSpec

# ── Outcome classes ────────────────────────────────────────────────────
PARSED_OK = "parsed_ok"
CLASSIFIED_INCOMPATIBLE = "classified_incompatible"
_ALLOWED_OUTCOMES = {PARSED_OK, CLASSIFIED_INCOMPATIBLE}

# The exception family the embedded planner raises when it is handed a
# pack whose template schema it cannot consume. KeyError covers the
# missing ``task_id`` / ``expected_output`` hard derefs; AttributeError
# covers a non-mapping ``when:`` reaching ``_evaluate_conditions``;
# TypeError/ValueError cover other structural mismatches (e.g. a
# non-string id, a malformed depends_on, a cycle). We deliberately keep
# this broad: anything the planner raises on a schema-incompatible pack
# is "classified incompatible", never an exception that escapes the sweep.
_SCHEMA_MISMATCH_EXCEPTIONS = (KeyError, AttributeError, TypeError, ValueError)


# ── Pack discovery (dynamic — no hardcoded list) ────────────────────────
def _discover_skill_packs() -> list[str]:
    """Every pack directory under the planner's own skills dir that ships
    a ``tasks.yaml``.

    Anchored on the production constant ``embedded_planner._SKILLS_DIR`` so
    discovery tracks the real location the planner reads from — if a pack
    is added/removed/relocated this sweep follows automatically, which is
    the point of the regression guard.
    """
    skills_dir: Path = ep._SKILLS_DIR
    return sorted(p.parent.name for p in skills_dir.glob("*/tasks.yaml"))


_SKILL_PACKS = _discover_skill_packs()


# ── Representative planner inputs ───────────────────────────────────────
# A maximal hardware profile so that conditional (`when:`) tasks are
# *included* rather than filtered out — this maximises how much of each
# pack's template surface actually flows through `_template_to_task`.
_FULL_HW = HardwareProfile(
    soc="Hi3516DV300",
    npu="NNIE",
    sensor=["IMX307"],
    codec=["H.264", "H.265"],
    usb=["USB2.0 OTG"],
    display="7-inch LCD 1024x600",
    peripherals=[
        Peripheral(name="GPIO", interface="sysfs", count=40),
        Peripheral(name="I2C", interface="i2c-dev", count=3),
    ],
)

_SPEC = ParsedSpec(
    project_type=SpecField("embedded_firmware", 0.9),
    project_class=SpecField("embedded_product", 0.9),
    target_arch=SpecField("arm64", 0.9),
    target_os=SpecField("linux", 0.9),
    framework=SpecField("embedded", 0.8),
    raw_text="planner-compat sweep",
)


@pytest.fixture(autouse=True)
def _clear_planner_cache():
    """Isolate each parametrized case from the planner's module-level
    ``_TASKS_CACHE`` so packs never bleed across cases."""
    reload_tasks_cache()
    yield
    reload_tasks_cache()


# ── Schema contract & outcome derivation ────────────────────────────────
def _load_pack_templates(pack: str) -> list[dict]:
    """Load a pack's raw task templates via the planner's own loader."""
    return ep._load_tasks_yaml(pack)


def _meets_embedded_contract(templates: list[dict]) -> bool:
    """True iff the pack satisfies the embedded planner's structural
    contract: a non-empty task list where every task is a mapping that
    carries BOTH keys the planner hard-dereferences — ``task_id`` and
    ``expected_output``.

    This mirrors exactly what the planner requires; a pack that satisfies
    it cannot KeyError on those two derefs.
    """
    if not templates:
        return False
    return all(
        isinstance(t, dict) and "task_id" in t and "expected_output" in t
        for t in templates
    )


def _expected_outcome(pack: str) -> str:
    """The outcome the schema contract *predicts* for this pack."""
    templates = _load_pack_templates(pack)
    return PARSED_OK if _meets_embedded_contract(templates) else CLASSIFIED_INCOMPATIBLE


def _observed_full_plan_outcome(pack: str) -> str:
    """Attempt the end-to-end planner; report the observed outcome class.

    Catches the schema-mismatch exception family so a structurally
    incompatible pack is *classified* rather than crashing the sweep.
    """
    try:
        plan_embedded_product(_SPEC, _FULL_HW, pack, dag_id=f"compat-{pack}")
    except _SCHEMA_MISMATCH_EXCEPTIONS:
        return CLASSIFIED_INCOMPATIBLE
    return PARSED_OK


def _observed_template_outcome(pack: str) -> str:
    """Attempt the per-template ``_template_to_task`` conversion for every
    template in the pack; report the observed outcome class."""
    try:
        for tmpl in _load_pack_templates(pack):
            _template_to_task(tmpl)
    except _SCHEMA_MISMATCH_EXCEPTIONS:
        return CLASSIFIED_INCOMPATIBLE
    return PARSED_OK


# ── The sweep ───────────────────────────────────────────────────────────
def test_skill_packs_discovered():
    """Sanity: discovery is dynamic and non-empty (the audit found 27)."""
    assert _SKILL_PACKS, "no configs/skills/*/tasks.yaml packs discovered"
    # _embedded_base is the canonical embedded template and must be present.
    assert "_embedded_base" in _SKILL_PACKS


@pytest.mark.parametrize("pack", _SKILL_PACKS, ids=_SKILL_PACKS)
def test_pack_planner_compat_full_plan(pack: str):
    """``plan_embedded_product`` over every pack: outcome ∈ allowed set,
    and the observed outcome agrees with the schema-contract prediction.

    The membership assertion is the "never an uncaught exception" guarantee
    (AC 1). The equality assertion is the regression guard (AC 2): a future
    pack that meets the embedded contract but still breaks the planner makes
    ``observed`` diverge from ``expected`` and FAILS here.
    """
    expected = _expected_outcome(pack)
    reload_tasks_cache()
    observed = _observed_full_plan_outcome(pack)

    assert observed in _ALLOWED_OUTCOMES, (
        f"{pack}: planner produced an unclassifiable outcome {observed!r}"
    )
    assert observed == expected, (
        f"{pack}: planner outcome {observed!r} disagrees with the schema "
        f"contract prediction {expected!r}. Either an embedded-schema pack "
        f"regressed (broke the planner) or a pack's schema changed shape — "
        f"investigate before muting."
    )


@pytest.mark.parametrize("pack", _SKILL_PACKS, ids=_SKILL_PACKS)
def test_pack_planner_compat_template_conversion(pack: str):
    """``_template_to_task`` over every pack's templates: same two-class
    contract as the full-plan sweep, exercised at the template granularity
    the AC names explicitly."""
    expected = _expected_outcome(pack)
    observed = _observed_template_outcome(pack)

    assert observed in _ALLOWED_OUTCOMES, (
        f"{pack}: _template_to_task produced an unclassifiable outcome "
        f"{observed!r}"
    )
    assert observed == expected, (
        f"{pack}: _template_to_task outcome {observed!r} disagrees with the "
        f"schema contract prediction {expected!r}."
    )


def test_compatibility_partition_is_documented():
    """Pin the *current* compatibility partition so a silent shift (a pack
    flipping class without anyone noticing) is visible in the diff.

    NOTE (scope guard): this is descriptive, not prescriptive — OP-1770 is
    read-only and does NOT migrate the incompatible packs. The set below is
    expected to SHRINK as later tickets migrate packs; when it does, update
    this expectation in the same change that performs the migration.
    """
    parsed_ok = sorted(p for p in _SKILL_PACKS if _expected_outcome(p) == PARSED_OK)
    incompatible = sorted(
        p for p in _SKILL_PACKS if _expected_outcome(p) == CLASSIFIED_INCOMPATIBLE
    )

    # Every discovered pack lands in exactly one class.
    assert set(parsed_ok) | set(incompatible) == set(_SKILL_PACKS)
    assert not (set(parsed_ok) & set(incompatible))

    # The embedded-native packs that the planner can consume today.
    assert parsed_ok == ["_embedded_base", "connectivity"], (
        "embedded-compatible pack set changed — if a pack was migrated to "
        "the embedded schema this is expected; update the expectation."
    )
