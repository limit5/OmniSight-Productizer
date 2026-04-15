"""C4 — Unit tests for the embedded product planner (#213).

Covers:
  - Full-profile DAG generation (all hardware present)
  - Minimal-profile DAG (bare SoC, no sensors/NPU/display)
  - Conditional task filtering (when: conditions)
  - Dependency resolution and topological ordering
  - Cycle detection
  - Critical path depth
  - Phase grouping
  - DAG validator pass
  - Skill pack fallback to _embedded_base
  - Custom skill pack with tasks.yaml
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from backend.dag_schema import DAG, Task as DAGTask
from backend.dag_validator import validate
from backend.embedded_planner import (
    _evaluate_conditions,
    _filter_tasks,
    _resolve_dependencies,
    get_dependency_depth,
    get_task_count_by_phase,
    plan_embedded_product,
    reload_tasks_cache,
)
from backend.hardware_profile import HardwareProfile, Peripheral
from backend.intent_parser import ParsedSpec
from backend.intent_parser import Field as SpecField


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture
def full_hw() -> HardwareProfile:
    return HardwareProfile(
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


@pytest.fixture
def minimal_hw() -> HardwareProfile:
    return HardwareProfile(soc="ESP32-S3")


@pytest.fixture
def camera_no_display_hw() -> HardwareProfile:
    return HardwareProfile(
        soc="RK3566",
        sensor=["IMX415"],
        codec=["H.265"],
        npu="RKNN",
        usb=["USB3.0"],
    )


@pytest.fixture
def spec() -> ParsedSpec:
    return ParsedSpec(
        project_type=SpecField("embedded_firmware", 0.9),
        project_class=SpecField("embedded_product", 0.9),
        target_arch=SpecField("arm64", 0.9),
        target_os=SpecField("linux", 0.9),
        framework=SpecField("embedded", 0.8),
        raw_text="Build an IP camera product based on Hi3516DV300",
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    reload_tasks_cache()
    yield
    reload_tasks_cache()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Condition evaluation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConditionEvaluation:
    def test_has_sensor_true(self, full_hw):
        assert _evaluate_conditions({"has_sensor": True}, full_hw) is True

    def test_has_sensor_false_on_minimal(self, minimal_hw):
        assert _evaluate_conditions({"has_sensor": True}, minimal_hw) is False

    def test_has_npu_true(self, full_hw):
        assert _evaluate_conditions({"has_npu": True}, full_hw) is True

    def test_has_npu_false_on_minimal(self, minimal_hw):
        assert _evaluate_conditions({"has_npu": True}, minimal_hw) is False

    def test_has_display_true(self, full_hw):
        assert _evaluate_conditions({"has_display": True}, full_hw) is True

    def test_has_display_false(self, minimal_hw):
        assert _evaluate_conditions({"has_display": True}, minimal_hw) is False

    def test_has_codec_true(self, full_hw):
        assert _evaluate_conditions({"has_codec": True}, full_hw) is True

    def test_has_usb_true(self, full_hw):
        assert _evaluate_conditions({"has_usb": True}, full_hw) is True

    def test_has_peripherals_true(self, full_hw):
        assert _evaluate_conditions({"has_peripherals": True}, full_hw) is True

    def test_has_peripherals_false(self, minimal_hw):
        assert _evaluate_conditions({"has_peripherals": True}, minimal_hw) is False

    def test_soc_contains_match(self, full_hw):
        assert _evaluate_conditions({"soc_contains": "hi3516"}, full_hw) is True

    def test_soc_contains_no_match(self, full_hw):
        assert _evaluate_conditions({"soc_contains": "rk3566"}, full_hw) is False

    def test_multiple_conditions_all_pass(self, full_hw):
        assert _evaluate_conditions(
            {"has_sensor": True, "has_npu": True}, full_hw
        ) is True

    def test_multiple_conditions_one_fails(self, minimal_hw):
        assert _evaluate_conditions(
            {"has_sensor": True, "has_npu": True}, minimal_hw
        ) is False

    def test_empty_conditions(self, minimal_hw):
        assert _evaluate_conditions({}, minimal_hw) is True

    def test_unknown_condition_key_ignored(self, full_hw):
        assert _evaluate_conditions({"unknown_key": True}, full_hw) is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Task filtering
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTaskFiltering:
    def test_unconditional_tasks_always_included(self, minimal_hw):
        templates = [
            {"task_id": "a", "expected_output": "a.bin", "depends_on": []},
        ]
        assert len(_filter_tasks(templates, minimal_hw)) == 1

    def test_conditional_tasks_filtered(self, minimal_hw):
        templates = [
            {"task_id": "a", "expected_output": "a.bin", "depends_on": []},
            {"task_id": "b", "expected_output": "b.bin", "depends_on": [],
             "when": {"has_npu": True}},
        ]
        result = _filter_tasks(templates, minimal_hw)
        assert len(result) == 1
        assert result[0]["task_id"] == "a"

    def test_conditional_tasks_included_when_match(self, full_hw):
        templates = [
            {"task_id": "a", "expected_output": "a.bin", "depends_on": []},
            {"task_id": "b", "expected_output": "b.bin", "depends_on": [],
             "when": {"has_npu": True}},
        ]
        result = _filter_tasks(templates, full_hw)
        assert len(result) == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dependency resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDependencyResolution:
    def test_topological_order(self):
        tasks = [
            {"task_id": "c", "depends_on": ["b"], "inputs": [], "expected_output": "c.bin"},
            {"task_id": "a", "depends_on": [], "inputs": [], "expected_output": "a.bin"},
            {"task_id": "b", "depends_on": ["a"], "inputs": [], "expected_output": "b.bin"},
        ]
        ordered = _resolve_dependencies(tasks)
        ids = [t["task_id"] for t in ordered]
        assert ids.index("a") < ids.index("b")
        assert ids.index("b") < ids.index("c")

    def test_dangling_dep_pruned(self):
        tasks = [
            {"task_id": "a", "depends_on": ["removed"], "inputs": [], "expected_output": "a.bin"},
        ]
        ordered = _resolve_dependencies(tasks)
        assert ordered[0]["depends_on"] == []

    def test_cycle_detection(self):
        tasks = [
            {"task_id": "a", "depends_on": ["b"], "inputs": [], "expected_output": "a.bin"},
            {"task_id": "b", "depends_on": ["a"], "inputs": [], "expected_output": "b.bin"},
        ]
        with pytest.raises(ValueError, match="Cyclic dependency"):
            _resolve_dependencies(tasks)

    def test_diamond_dependency(self):
        tasks = [
            {"task_id": "d", "depends_on": ["b", "c"], "inputs": [], "expected_output": "d.bin"},
            {"task_id": "a", "depends_on": [], "inputs": [], "expected_output": "a.bin"},
            {"task_id": "b", "depends_on": ["a"], "inputs": [], "expected_output": "b.bin"},
            {"task_id": "c", "depends_on": ["a"], "inputs": [], "expected_output": "c.bin"},
        ]
        ordered = _resolve_dependencies(tasks)
        ids = [t["task_id"] for t in ordered]
        assert ids.index("a") < ids.index("b")
        assert ids.index("a") < ids.index("c")
        assert ids.index("b") < ids.index("d")
        assert ids.index("c") < ids.index("d")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Full plan generation — full hardware
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFullPlan:
    def test_full_profile_task_count(self, spec, full_hw):
        dag = plan_embedded_product(spec, full_hw, dag_id="test-full")
        assert dag.total_tasks >= 20
        assert dag.total_tasks == len(dag.tasks)

    def test_full_profile_has_all_phases(self, spec, full_hw):
        dag = plan_embedded_product(spec, full_hw, dag_id="test-full")
        task_ids = {t.task_id for t in dag.tasks}
        assert "bsp-toolchain-setup" in task_ids
        assert "kernel-build" in task_ids
        assert "driver-sensor" in task_ids
        assert "driver-npu" in task_ids
        assert "driver-display" in task_ids
        assert "app-main" in task_ids
        assert "ui-panel" in task_ids
        assert "ota-package" in task_ids
        assert "test-unit" in task_ids
        assert "test-hil" in task_ids
        assert "docs-datasheet" in task_ids

    def test_full_profile_passes_validator(self, spec, full_hw):
        dag = plan_embedded_product(spec, full_hw, dag_id="test-full")
        result = validate(dag)
        assert result.ok, result.summary()

    def test_dag_id_contains_soc(self, spec, full_hw):
        dag = plan_embedded_product(spec, full_hw)
        assert "hi3516dv300" in dag.dag_id

    def test_schema_version(self, spec, full_hw):
        dag = plan_embedded_product(spec, full_hw, dag_id="test-full")
        assert dag.schema_version == 1

    def test_topological_order_maintained(self, spec, full_hw):
        dag = plan_embedded_product(spec, full_hw, dag_id="test-full")
        id_to_idx = {t.task_id: i for i, t in enumerate(dag.tasks)}
        for task in dag.tasks:
            for dep in task.depends_on:
                assert id_to_idx[dep] < id_to_idx[task.task_id], (
                    f"{task.task_id} appears before its dependency {dep}"
                )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Minimal plan — bare SoC only
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestMinimalPlan:
    def test_minimal_profile_excludes_conditional_tasks(self, spec, minimal_hw):
        dag = plan_embedded_product(spec, minimal_hw, dag_id="test-minimal")
        task_ids = {t.task_id for t in dag.tasks}
        assert "driver-sensor" not in task_ids
        assert "driver-npu" not in task_ids
        assert "driver-display" not in task_ids
        assert "driver-codec" not in task_ids
        assert "ui-panel" not in task_ids
        assert "protocol-streaming" not in task_ids

    def test_minimal_profile_includes_core_tasks(self, spec, minimal_hw):
        dag = plan_embedded_product(spec, minimal_hw, dag_id="test-minimal")
        task_ids = {t.task_id for t in dag.tasks}
        assert "bsp-toolchain-setup" in task_ids
        assert "kernel-build" in task_ids
        assert "app-main" in task_ids
        assert "test-unit" in task_ids

    def test_minimal_fewer_tasks_than_full(self, spec, minimal_hw, full_hw):
        dag_min = plan_embedded_product(spec, minimal_hw, dag_id="test-min")
        dag_full = plan_embedded_product(spec, full_hw, dag_id="test-full")
        assert dag_min.total_tasks < dag_full.total_tasks

    def test_minimal_passes_validator(self, spec, minimal_hw):
        dag = plan_embedded_product(spec, minimal_hw, dag_id="test-minimal")
        result = validate(dag)
        assert result.ok, result.summary()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Camera without display
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCameraNoDisplay:
    def test_has_sensor_and_npu_but_no_display(self, spec, camera_no_display_hw):
        dag = plan_embedded_product(spec, camera_no_display_hw, dag_id="test-cam")
        task_ids = {t.task_id for t in dag.tasks}
        assert "driver-sensor" in task_ids
        assert "driver-npu" in task_ids
        assert "driver-usb" in task_ids
        assert "protocol-streaming" in task_ids
        assert "driver-display" not in task_ids
        assert "ui-panel" not in task_ids

    def test_passes_validator(self, spec, camera_no_display_hw):
        dag = plan_embedded_product(spec, camera_no_display_hw, dag_id="test-cam")
        result = validate(dag)
        assert result.ok, result.summary()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Topology helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTopologyHelpers:
    def test_phase_grouping(self, spec, full_hw):
        dag = plan_embedded_product(spec, full_hw, dag_id="test-phases")
        phases = get_task_count_by_phase(dag)
        assert "bsp" in phases
        assert "kernel" in phases
        assert "driver" in phases
        assert "app" in phases
        assert "test" in phases

    def test_dependency_depth_full(self, spec, full_hw):
        dag = plan_embedded_product(spec, full_hw, dag_id="test-depth")
        depth = get_dependency_depth(dag)
        assert depth >= 4

    def test_dependency_depth_minimal(self, spec, minimal_hw):
        dag = plan_embedded_product(spec, minimal_hw, dag_id="test-depth")
        depth = get_dependency_depth(dag)
        assert depth >= 3

    def test_empty_dag_depth(self):
        dag = DAG(schema_version=1, dag_id="empty", tasks=[
            DAGTask(task_id="a", description="x", required_tier="t1",
                    toolchain="cmake", expected_output="build/out/a.bin"),
        ])
        assert get_dependency_depth(dag) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Skill pack fallback & custom pack
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSkillPackLoading:
    def test_fallback_to_embedded_base(self, spec, full_hw):
        dag = plan_embedded_product(spec, full_hw, "nonexistent-pack", dag_id="test-fb")
        assert dag.total_tasks > 0

    def test_empty_skill_pack_uses_base(self, spec, full_hw):
        dag = plan_embedded_product(spec, full_hw, "", dag_id="test-empty")
        assert dag.total_tasks > 0

    def test_custom_skill_pack(self, spec, minimal_hw, tmp_path):
        custom_dir = tmp_path / "custom-skill"
        custom_dir.mkdir()
        (custom_dir / "tasks.yaml").write_text(yaml.dump({
            "schema_version": 1,
            "tasks": [
                {
                    "task_id": "custom-task-1",
                    "description": "Custom task one",
                    "required_tier": "t1",
                    "toolchain": "cmake",
                    "inputs": [],
                    "expected_output": "build/custom/out1.bin",
                    "depends_on": [],
                },
                {
                    "task_id": "custom-task-2",
                    "description": "Custom task two",
                    "required_tier": "t1",
                    "toolchain": "cmake",
                    "inputs": ["build/custom/out1.bin"],
                    "expected_output": "build/custom/out2.bin",
                    "depends_on": ["custom-task-1"],
                },
            ],
        }))
        import backend.embedded_planner as ep
        original_dir = ep._SKILLS_DIR
        ep._SKILLS_DIR = tmp_path
        try:
            reload_tasks_cache()
            dag = plan_embedded_product(spec, minimal_hw, "custom-skill", dag_id="test-custom")
            assert dag.total_tasks == 2
            assert dag.tasks[0].task_id == "custom-task-1"
            assert dag.tasks[1].task_id == "custom-task-2"
        finally:
            ep._SKILLS_DIR = original_dir


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Edge cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEdgeCases:
    def test_no_tasks_after_filter_raises(self, spec, tmp_path):
        custom_dir = tmp_path / "empty-skill"
        custom_dir.mkdir()
        (custom_dir / "tasks.yaml").write_text(yaml.dump({
            "schema_version": 1,
            "tasks": [
                {
                    "task_id": "conditional-only",
                    "description": "Only included with NPU",
                    "required_tier": "t1",
                    "toolchain": "cmake",
                    "inputs": [],
                    "expected_output": "build/out.bin",
                    "depends_on": [],
                    "when": {"has_npu": True},
                },
            ],
        }))
        hw = HardwareProfile(soc="bare")
        import backend.embedded_planner as ep
        original_dir = ep._SKILLS_DIR
        ep._SKILLS_DIR = tmp_path
        try:
            reload_tasks_cache()
            with pytest.raises(Exception):
                plan_embedded_product(spec, hw, "empty-skill", dag_id="test-empty-filter")
        finally:
            ep._SKILLS_DIR = original_dir

    def test_all_task_ids_unique(self, spec, full_hw):
        dag = plan_embedded_product(spec, full_hw, dag_id="test-unique")
        ids = [t.task_id for t in dag.tasks]
        assert len(ids) == len(set(ids))

    def test_all_deps_reference_valid_ids(self, spec, full_hw):
        dag = plan_embedded_product(spec, full_hw, dag_id="test-deps")
        valid_ids = {t.task_id for t in dag.tasks}
        for task in dag.tasks:
            for dep in task.depends_on:
                assert dep in valid_ids, f"{task.task_id} depends on unknown {dep}"

    def test_no_self_dependency(self, spec, full_hw):
        dag = plan_embedded_product(spec, full_hw, dag_id="test-self")
        for task in dag.tasks:
            assert task.task_id not in task.depends_on
