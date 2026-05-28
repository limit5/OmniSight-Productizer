"""1F.P1 — unit tests for the system-of-systems product planner (OP-1826).

Covers compose_product():
  - composing >=2 parseable packs (imaging + connectivity) yields ONE
    DAG that passes dag_validator.validate()
  - no task_id collisions across packs
  - every pack's tasks are present, pack-namespaced
  - expected_output / depends_on / inputs are namespaced consistently
  - P1 scope guard: no cross-pack depends_on edges
  - the DAG validator is reused (invalid merge raises)
  - empty packs list is rejected
"""

from __future__ import annotations

import pytest

from backend.dag_validator import validate
from backend.embedded_planner import plan_embedded_product, reload_tasks_cache
from backend.hardware_profile import HardwareProfile
from backend.intent_parser import Field as SpecField
from backend.intent_parser import ParsedSpec
from backend.product_planner import (
    OUTPUT_SEP,
    TASK_ID_SEP,
    ProductCompositionError,
    _ns_output,
    _ns_task_id,
    compose_product,
)

TWO_PACKS = ["imaging", "connectivity"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture
def spec() -> ParsedSpec:
    return ParsedSpec(
        project_type=SpecField("embedded_firmware", 0.9),
        project_class=SpecField("embedded_product", 0.9),
        target_arch=SpecField("arm64", 0.9),
        target_os=SpecField("linux", 0.9),
        framework=SpecField("embedded", 0.8),
        raw_text="Compose imaging + connectivity into one product",
    )


@pytest.fixture
def hw() -> HardwareProfile:
    return HardwareProfile(
        soc="RK3566",
        sensor=["IMX415"],
        codec=["H.265"],
        npu="RKNN",
        usb=["USB3.0"],
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    reload_tasks_cache()
    yield
    reload_tasks_cache()


def _pack_of(task_id: str) -> str:
    return task_id.split(TASK_ID_SEP, 1)[0]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Core acceptance: merge + validate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestComposeAndValidate:
    def test_two_packs_compose_to_one_valid_dag(self, spec, hw):
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="prod-two")
        result = validate(dag)
        assert result.ok, result.summary()

    def test_no_task_id_collisions(self, spec, hw):
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="prod-two")
        ids = [t.task_id for t in dag.tasks]
        assert len(ids) == len(set(ids))

    def test_contains_every_packs_namespaced_tasks(self, spec, hw):
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="prod-two")
        merged_ids = {t.task_id for t in dag.tasks}

        expected_total = 0
        for pack in TWO_PACKS:
            sub = plan_embedded_product(spec, hw, skill_pack=pack)
            assert sub.tasks, f"{pack} produced an empty sub-DAG"
            for t in sub.tasks:
                assert _ns_task_id(pack, t.task_id) in merged_ids
            expected_total += len(sub.tasks)

        # Union, nothing dropped or duplicated.
        assert len(dag.tasks) == expected_total
        assert dag.total_tasks == expected_total

    def test_both_packs_represented(self, spec, hw):
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="prod-two")
        packs_seen = {_pack_of(t.task_id) for t in dag.tasks}
        assert packs_seen == set(TWO_PACKS)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Namespacing correctness
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestNamespacing:
    def test_task_ids_prefixed_with_pack(self, spec, hw):
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="prod-two")
        for t in dag.tasks:
            assert _pack_of(t.task_id) in TWO_PACKS
            assert t.task_id.startswith(_pack_of(t.task_id) + TASK_ID_SEP)

    def test_expected_outputs_prefixed_with_pack(self, spec, hw):
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="prod-two")
        for t in dag.tasks:
            pack = _pack_of(t.task_id)
            assert t.expected_output.startswith(pack + OUTPUT_SEP)

    def test_depends_on_references_resolve_in_merged_dag(self, spec, hw):
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="prod-two")
        ids = {t.task_id for t in dag.tasks}
        for t in dag.tasks:
            for dep in t.depends_on:
                assert dep in ids, f"{t.task_id} -> unknown dep {dep}"

    def test_intra_pack_inputs_namespaced_externals_untouched(self, spec, hw):
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="prod-two")
        produced = {t.expected_output for t in dag.tasks}
        for t in dag.tasks:
            for inp in t.inputs:
                if inp.startswith("external:") or inp.startswith("user:"):
                    continue
                # A non-external input must point at a namespaced output
                # produced by some task in the merged DAG.
                assert inp in produced, f"{t.task_id} input {inp} not produced"
                assert inp.startswith(_pack_of(t.task_id) + OUTPUT_SEP)

    def test_external_inputs_survive(self, spec, hw):
        # connectivity's BLE/WiFi/modem stacks carry external:hardware_profile.
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="prod-two")
        all_inputs = [inp for t in dag.tasks for inp in t.inputs]
        assert any(inp == "external:hardware_profile" for inp in all_inputs)

    def test_ns_helpers(self):
        assert _ns_task_id("imaging", "isp") == "imaging__isp"
        assert _ns_output("imaging", "build/x.bin") == "imaging/build/x.bin"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P1 scope guard — no cross-pack wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestScopeGuardNoCrossPackEdges:
    def test_no_cross_pack_depends_on(self, spec, hw):
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="prod-two")
        for t in dag.tasks:
            owner = _pack_of(t.task_id)
            for dep in t.depends_on:
                assert _pack_of(dep) == owner, (
                    f"P1 must not wire across packs: {t.task_id} -> {dep}"
                )

    def test_no_cross_pack_inputs(self, spec, hw):
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="prod-two")
        for t in dag.tasks:
            owner = _pack_of(t.task_id)
            for inp in t.inputs:
                if inp.startswith("external:") or inp.startswith("user:"):
                    continue
                assert inp.startswith(owner + OUTPUT_SEP), (
                    f"P1 must not wire across packs: {t.task_id} <- {inp}"
                )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validator reuse + boundary behaviour
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestValidatorReuseAndBoundaries:
    def test_empty_packs_rejected(self, spec, hw):
        with pytest.raises(ValueError, match="at least one pack"):
            compose_product(spec, hw, [])

    def test_duplicate_pack_fails_validation(self, spec, hw):
        # Same pack twice -> identical namespaced ids -> the reused
        # validator flags duplicate_id and compose_product raises.
        with pytest.raises(ProductCompositionError):
            compose_product(spec, hw, ["imaging", "imaging"])

    def test_single_pack_composes(self, spec, hw):
        dag = compose_product(spec, hw, ["imaging"], dag_id="prod-one")
        assert validate(dag).ok
        assert all(_pack_of(t.task_id) == "imaging" for t in dag.tasks)

    def test_custom_dag_id_respected(self, spec, hw):
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="my-product")
        assert dag.dag_id == "my-product"

    def test_auto_dag_id_prefixed(self, spec, hw):
        dag = compose_product(spec, hw, TWO_PACKS)
        assert dag.dag_id.startswith("product-")

    def test_schema_version_one(self, spec, hw):
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="prod-two")
        assert dag.schema_version == 1
