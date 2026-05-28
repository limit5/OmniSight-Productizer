"""1F.P1+P2a — unit tests for the system-of-systems product planner.

P1 (OP-1826) — compose_product() merge + namespacing:
  - composing >=2 parseable packs (imaging + connectivity) yields ONE
    DAG that passes dag_validator.validate()
  - no task_id collisions across packs
  - every pack's tasks are present, pack-namespaced
  - expected_output / depends_on / inputs are namespaced consistently
  - the DAG validator is reused (invalid merge raises)
  - empty packs list is rejected

P2a (OP-1827) — cross-pack provides/requires wiring MECHANISM, exercised
with SYNTHETIC fixtures (no real pack declares tokens — that is P2b):
  - {A provides "x", B requires "x"} -> B->A cross-pack edge
  - {A, B both provide "x"} -> DuplicateProvideError
  - {B requires "y", none provides} -> surfaced unmet_requires
  - external:/user: and self-provided requires are neither wired nor surfaced
"""

from __future__ import annotations

import pytest

from backend.dag_schema import Task
from backend.dag_validator import validate
from backend.embedded_planner import plan_embedded_product, reload_tasks_cache
from backend.hardware_profile import HardwareProfile
from backend.intent_parser import Field as SpecField
from backend.intent_parser import ParsedSpec
from backend.product_planner import (
    OUTPUT_SEP,
    TASK_ID_SEP,
    DuplicateProvideError,
    ProductCompositionError,
    _ns_output,
    _ns_task_id,
    _PackPlan,
    _roots,
    _sinks,
    _wire_cross_pack,
    compose_product,
)
from backend.skill_manifest import SkillManifest

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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P2a (OP-1827) — cross-pack wiring MECHANISM (pure helper, synthetic)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _task(task_id: str, depends_on: list[str] | None = None) -> Task:
    """A minimal synthetic namespaced task (no real pack involved)."""
    return Task(
        task_id=task_id,
        description="synthetic",
        required_tier="t1",
        toolchain="cmake",
        inputs=[],
        expected_output=f"{task_id}.bin",
        depends_on=depends_on or [],
    )


def _two_pack_plans() -> tuple[_PackPlan, _PackPlan]:
    # Each pack: one root (a1/b1) -> one sink (a2/b2).
    a = _PackPlan(
        pack="packa",
        tasks=[_task("packa__a1"), _task("packa__a2", ["packa__a1"])],
    )
    b = _PackPlan(
        pack="packb",
        tasks=[_task("packb__b1"), _task("packb__b2", ["packb__b1"])],
    )
    return a, b


class TestCrossPackWiringHelper:
    def test_roots_and_sinks(self):
        a, _ = _two_pack_plans()
        assert [t.task_id for t in _roots(a.tasks)] == ["packa__a1"]
        assert [t.task_id for t in _sinks(a.tasks)] == ["packa__a2"]

    def test_provides_requires_wires_b_to_a(self):
        a, b = _two_pack_plans()
        a.provides = ["x"]
        b.requires = ["x"]
        unmet = _wire_cross_pack([a, b])
        assert unmet == []
        # B's root now depends on A's sink — a cross-pack B->A edge.
        b1 = next(t for t in b.tasks if t.task_id == "packb__b1")
        assert "packa__a2" in b1.depends_on
        # The producing pack is untouched (no A->B edge introduced).
        for t in a.tasks:
            assert all(not d.startswith("packb__") for d in t.depends_on)

    def test_duplicate_provides_raises(self):
        a, b = _two_pack_plans()
        a.provides = ["x"]
        b.provides = ["x"]
        with pytest.raises(DuplicateProvideError) as exc:
            _wire_cross_pack([a, b])
        assert exc.value.conflicts == {"x": ["packa", "packb"]}

    def test_unmet_requires_surfaced(self):
        a, b = _two_pack_plans()
        b.requires = ["y"]
        before = {t.task_id: list(t.depends_on) for t in b.tasks}
        unmet = _wire_cross_pack([a, b])
        assert unmet == [{"pack": "packb", "token": "y"}]
        # Surfaced, never silently wired.
        after = {t.task_id: list(t.depends_on) for t in b.tasks}
        assert before == after

    def test_external_and_user_requires_neither_wired_nor_surfaced(self):
        a, b = _two_pack_plans()
        b.requires = ["external:hardware_profile", "user:api_key"]
        before = {t.task_id: list(t.depends_on) for t in b.tasks}
        unmet = _wire_cross_pack([a, b])
        assert unmet == []
        after = {t.task_id: list(t.depends_on) for t in b.tasks}
        assert before == after

    def test_self_provided_requires_not_unmet(self):
        a, b = _two_pack_plans()
        b.provides = ["z"]
        b.requires = ["z"]
        before = {t.task_id: list(t.depends_on) for t in b.tasks}
        unmet = _wire_cross_pack([a, b])
        assert unmet == []
        after = {t.task_id: list(t.depends_on) for t in b.tasks}
        assert before == after  # met in-pack, no self-edge added

    def test_multiple_roots_all_wired_to_producer_sink(self):
        # Consumer with two independent roots: both must wait on producer.
        producer = _PackPlan(pack="prod", tasks=[_task("prod__only")])
        consumer = _PackPlan(
            pack="cons",
            tasks=[_task("cons__r1"), _task("cons__r2")],
        )
        producer.provides = ["cap"]
        consumer.requires = ["cap"]
        assert _wire_cross_pack([producer, consumer]) == []
        for t in consumer.tasks:
            assert "prod__only" in t.depends_on

    def test_multi_require_consumer_wired_to_all_providers(self):
        # OP-1829 regression: a consumer requiring TWO tokens from two
        # DISTINCT providers must get BOTH cross-pack edges on its entry
        # task. The pre-fix loop re-computed _roots() inside the requires
        # loop; wiring the first token made the entry task non-root, so the
        # second token was silently dropped. This test FAILS on that code
        # (only proda's edge present) and PASSES once roots are snapshotted
        # once before the loop.
        prod_a = _PackPlan(
            pack="proda",
            tasks=[_task("proda__a1"), _task("proda__a2", ["proda__a1"])],
            provides=["cap_a"],
        )
        prod_b = _PackPlan(
            pack="prodb",
            tasks=[_task("prodb__b1"), _task("prodb__b2", ["prodb__b1"])],
            provides=["cap_b"],
        )
        consumer = _PackPlan(
            pack="cons",
            tasks=[_task("cons__c1"), _task("cons__c2", ["cons__c1"])],
            requires=["cap_a", "cap_b"],
        )
        unmet = _wire_cross_pack([prod_a, prod_b, consumer])
        assert unmet == []
        c1 = next(t for t in consumer.tasks if t.task_id == "cons__c1")
        # BOTH providers' sinks wired onto the consumer's single entry task.
        assert "proda__a2" in c1.depends_on
        assert "prodb__b2" in c1.depends_on
        # Producers themselves remain unwired across packs.
        for plan in (prod_a, prod_b):
            for t in plan.tasks:
                assert all(_pack_of(d) == plan.pack for d in t.depends_on)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P2a (OP-1827) — cross-pack wiring through compose_product (injected
#  synthetic manifests; real packs declare no tokens, so this is hermetic)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCrossPackWiringCompose:
    def test_merged_dag_has_consumer_to_producer_edge(self, spec, hw):
        manifests = {
            "imaging": SkillManifest(name="imaging", provides=["scan_pipeline"]),
            "connectivity": SkillManifest(
                name="connectivity", requires=["scan_pipeline"]
            ),
        }
        dag = compose_product(
            spec, hw, TWO_PACKS, dag_id="prod-wire", manifests=manifests
        )
        result = validate(dag)
        assert result.ok, result.summary()
        cross_edges = [
            (t.task_id, dep)
            for t in dag.tasks
            for dep in t.depends_on
            if dep.split(TASK_ID_SEP, 1)[0] == "imaging"
            and t.task_id.split(TASK_ID_SEP, 1)[0] == "connectivity"
        ]
        assert cross_edges, "expected a connectivity->imaging cross-pack edge"

    def test_duplicate_provides_raises(self, spec, hw):
        manifests = {
            "imaging": SkillManifest(name="imaging", provides=["dup"]),
            "connectivity": SkillManifest(name="connectivity", provides=["dup"]),
        }
        with pytest.raises(DuplicateProvideError) as exc:
            compose_product(spec, hw, TWO_PACKS, manifests=manifests)
        assert exc.value.conflicts == {"dup": ["connectivity", "imaging"]}

    def test_unmet_requires_surfaced_in_log(self, spec, hw, caplog):
        manifests = {
            "connectivity": SkillManifest(
                name="connectivity", requires=["nonexistent_cap"]
            ),
        }
        with caplog.at_level("WARNING", logger="backend.product_planner"):
            dag = compose_product(
                spec, hw, TWO_PACKS, dag_id="prod-unmet", manifests=manifests
            )
        assert validate(dag).ok
        records = [r for r in caplog.records if hasattr(r, "unmet_requires")]
        assert records
        assert {"pack": "connectivity", "token": "nonexistent_cap"} in \
            records[0].unmet_requires

    def test_no_manifests_preserves_p1_no_cross_pack_edges(self, spec, hw):
        # imaging/connectivity now declare provides but no requires, and
        # npu-detection is not in this pair -> still no cross-pack edges
        # between just these two packs even when reading real manifests.
        dag = compose_product(spec, hw, TWO_PACKS, dag_id="prod-nowire")
        for t in dag.tasks:
            owner = t.task_id.split(TASK_ID_SEP, 1)[0]
            for dep in t.depends_on:
                assert dep.split(TASK_ID_SEP, 1)[0] == owner


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P2b (OP-1829) — REAL case-8 composition: imaging + connectivity +
#  npu-detection, using the packs' own declared provides/requires tokens
#  (no injected manifests). Exercises the multi-require snapshot fix on
#  real packs: npu-detection requires BOTH camera-frames (imaging) AND
#  plc-transport (connectivity).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CASE8_PACKS = ["imaging", "connectivity", "npu-detection"]


class TestRealCaseEightComposition:
    def test_three_packs_load_via_registry_with_tokens(self):
        from backend import skill_registry

        expected = {
            "imaging": (["camera-frames"], []),
            "connectivity": (["plc-transport"], []),
            "npu-detection": (
                ["inspection-verdict"],
                ["camera-frames", "plc-transport"],
            ),
        }
        for pack, (provides, requires) in expected.items():
            info = skill_registry.get_skill(pack)
            assert info is not None, f"{pack} not found in registry"
            assert info.manifest is not None, f"{pack} has no parseable manifest"
            assert info.manifest.provides == provides
            assert info.manifest.requires == requires

    def test_three_packs_parse_via_embedded_planner(self, spec, hw):
        # npu-detection has no tasks.yaml -> falls back to _embedded_base;
        # all three still yield a non-empty sub-DAG.
        for pack in CASE8_PACKS:
            sub = plan_embedded_product(spec, hw, skill_pack=pack)
            assert sub.tasks, f"{pack} produced an empty sub-DAG"

    def test_case8_composes_and_validates(self, spec, hw):
        dag = compose_product(spec, hw, CASE8_PACKS, dag_id="prod-case8")
        assert validate(dag).ok, validate(dag).summary()
        packs_seen = {_pack_of(t.task_id) for t in dag.tasks}
        assert packs_seen == set(CASE8_PACKS)

    def test_npu_detection_wired_to_both_providers(self, spec, hw):
        dag = compose_product(spec, hw, CASE8_PACKS, dag_id="prod-case8")
        npu_cross_deps = {
            dep
            for t in dag.tasks
            if _pack_of(t.task_id) == "npu-detection"
            for dep in t.depends_on
            if _pack_of(dep) != "npu-detection"
        }
        providers_wired = {_pack_of(d) for d in npu_cross_deps}
        # The whole point of the fix: BOTH required tokens are wired, not
        # just the first one (camera-frames). Pre-fix this set is {imaging}.
        assert providers_wired == {"imaging", "connectivity"}, providers_wired

    def test_case8_has_no_unmet_requires(self, spec, hw, caplog):
        # Scope to the product planner logger so the embedded planner's own
        # unmet_deps warnings (from the base template) are not conflated.
        with caplog.at_level("WARNING", logger="backend.product_planner"):
            compose_product(spec, hw, CASE8_PACKS, dag_id="prod-case8")
        unmet = [r for r in caplog.records if hasattr(r, "unmet_requires")]
        assert unmet == [], [r.unmet_requires for r in unmet]
