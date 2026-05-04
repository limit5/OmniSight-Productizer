"""BP.C.6 — ProjectClass / target_triple / T-shirt size orthogonality.

The Blueprint-v2 C4 decision keeps three axes independent:

* ``ProjectClass`` — business/domain routing from C0
* ``target_triple`` — compile target from Blueprint templates
* ``size`` — S/M/XL graph scale from Phase C

These tests pin that contract without changing runtime routing. The
planner router should only look at ``ProjectClass``; topology selection
should only look at ``GraphState.size``; template target triples should
round-trip independently of both.
"""

from __future__ import annotations

import pytest

from backend import intent_parser as ip
from backend.agents.graph import _select_graph_for_state
from backend.agents.state import GraphState
from backend.graph_topology import VALID_TOPOLOGY_SIZES
from backend.models import ProjectClass
from backend.planner_router import get_config_for_class, route_to_planner
from backend.templates.task import TaskTemplate


TARGET_TRIPLES = (
    "x86_64-pc-linux-gnu",
    "aarch64-unknown-linux-gnu",
    "armv7-unknown-linux-gnueabihf",
    "x86_64-apple-darwin",
)


def _task_template(target_triple: str, size: str) -> TaskTemplate:
    return TaskTemplate(
        target_triple=target_triple,
        allowed_dependencies=[],
        max_cognitive_load_tokens=4096,
        guild_id="backend",
        size=size,  # type: ignore[arg-type]
    )


class TestBlueprintAxisSurfaces:
    def test_axis_value_sets_are_disjoint(self) -> None:
        project_classes = {pc.value for pc in ProjectClass}
        sizes = set(VALID_TOPOLOGY_SIZES)

        assert project_classes.isdisjoint(sizes)
        assert all(triple not in project_classes for triple in TARGET_TRIPLES)
        assert all(triple not in sizes for triple in TARGET_TRIPLES)

    @pytest.mark.parametrize("project_class", [pc.value for pc in ProjectClass])
    @pytest.mark.parametrize("target_triple", TARGET_TRIPLES)
    @pytest.mark.parametrize("size", VALID_TOPOLOGY_SIZES)
    def test_project_class_target_triple_and_size_cross_product_is_valid(
        self,
        project_class: str,
        target_triple: str,
        size: str,
    ) -> None:
        spec = ip.ParsedSpec(project_class=ip.Field(project_class, 0.9))
        task = _task_template(target_triple, size)
        state = GraphState(size=size)  # type: ignore[arg-type]

        assert spec.project_class.value == project_class
        assert task.target_triple == target_triple
        assert task.size == state.size == size


class TestPlannerAxisIsolation:
    @pytest.mark.parametrize("project_class", [pc.value for pc in ProjectClass])
    def test_planner_routing_depends_only_on_project_class(
        self,
        project_class: str,
    ) -> None:
        baseline = get_config_for_class(project_class)

        for target_triple in TARGET_TRIPLES:
            for size in VALID_TOPOLOGY_SIZES:
                _task_template(target_triple, size)
                spec = ip.ParsedSpec(project_class=ip.Field(project_class, 0.9))
                assert route_to_planner(spec) == baseline


class TestTopologyAxisIsolation:
    @pytest.mark.parametrize(
        ("size", "expected_node"),
        [
            ("S", "single_track"),
            ("M", "firmware"),
            ("XL", "portfolio_architect"),
        ],
    )
    def test_topology_selection_depends_only_on_size(
        self,
        monkeypatch: pytest.MonkeyPatch,
        size: str,
        expected_node: str,
    ) -> None:
        monkeypatch.setenv("OMNISIGHT_TOPOLOGY_MODE", "smxl")

        for project_class in ProjectClass:
            for target_triple in TARGET_TRIPLES:
                ip.ParsedSpec(project_class=ip.Field(project_class.value, 0.9))
                _task_template(target_triple, size)
                graph = _select_graph_for_state(
                    GraphState(size=size),  # type: ignore[arg-type]
                )
                assert expected_node in graph.nodes
