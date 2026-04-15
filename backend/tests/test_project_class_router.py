"""C0 — ProjectClass enum + multi-planner routing tests.

Covers:
  * ProjectClass enum values in models.py
  * ParsedSpec.project_class field presence and serialisation
  * Heuristic inference of project_class from keywords
  * YAML conflict rules for ambiguous project_class
  * Planner router: each class dispatches to its own planner
"""

from __future__ import annotations

import json

import pytest

from backend import intent_parser as ip
from backend.models import ProjectClass
from backend.planner_router import (
    PlannerConfig,
    get_config_for_class,
    get_planner_ids,
    route_to_planner,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ProjectClass enum
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestProjectClassEnum:
    EXPECTED = {
        "embedded_product", "algo_sim", "optical_sim",
        "iso_standard", "test_tool", "factory_tool", "enterprise_web",
    }

    def test_all_values_present(self):
        actual = {e.value for e in ProjectClass}
        assert actual == self.EXPECTED

    def test_string_identity(self):
        for e in ProjectClass:
            assert str(e) == f"ProjectClass.{e.name}"
            assert e.value == e.name


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ParsedSpec.project_class field
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_parsed_spec_has_project_class_field():
    ps = ip.ParsedSpec()
    assert hasattr(ps, "project_class")
    assert ps.project_class.value == "unknown"
    assert ps.project_class.confidence == 0.0


def test_project_class_in_to_dict():
    ps = ip.ParsedSpec(project_class=ip.Field("embedded_product", 0.8))
    d = ps.to_dict()
    json.dumps(d)
    assert d["project_class"] == {"value": "embedded_product", "confidence": 0.8}


def test_project_class_in_low_confidence():
    ps = ip.ParsedSpec()
    low = ps.low_confidence(0.7)
    assert "project_class" in low


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Heuristic inference
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_embedded_firmware_infers_embedded_product():
    p = await ip.parse_intent("Build an RTOS driver for the IMX335 sensor over MIPI CSI.")
    assert p.project_class.value == "embedded_product"
    assert p.project_class.confidence > 0


@pytest.mark.asyncio
async def test_zemax_infers_optical_sim():
    p = await ip.parse_intent("Run a Zemax ray tracing simulation for the new lens design.")
    assert p.project_class.value == "optical_sim"
    assert p.project_class.confidence >= 0.5


@pytest.mark.asyncio
async def test_erp_infers_enterprise_web():
    p = await ip.parse_intent("Build an ERP system with multi-tenant RBAC and SSO.")
    assert p.project_class.value == "enterprise_web"
    assert p.project_class.confidence >= 0.5


@pytest.mark.asyncio
async def test_factory_jig_infers_factory_tool():
    p = await ip.parse_intent("Create a factory jig controller with MES integration and SPC charts.")
    assert p.project_class.value == "factory_tool"
    assert p.project_class.confidence >= 0.5


@pytest.mark.asyncio
async def test_pytorch_training_infers_algo_sim():
    p = await ip.parse_intent("Set up a PyTorch training pipeline with dataset versioning.")
    assert p.project_class.value == "algo_sim"
    assert p.project_class.confidence >= 0.5


@pytest.mark.asyncio
async def test_iso_compliance_infers_iso_standard():
    p = await ip.parse_intent("Implement ISO 26262 ASIL-B compliance for the braking ECU.")
    assert p.project_class.value == "iso_standard"
    assert p.project_class.confidence >= 0.5


@pytest.mark.asyncio
async def test_test_harness_infers_test_tool():
    p = await ip.parse_intent("Build a test harness for regression testing the codec library.")
    assert p.project_class.value == "test_tool"
    assert p.project_class.confidence >= 0.5


@pytest.mark.asyncio
async def test_empty_input_leaves_project_class_unknown():
    p = await ip.parse_intent("")
    assert p.project_class.value == "unknown"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  YAML conflict rules for project_class
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_embedded_class_ambiguous_conflict_fires():
    ps = ip.ParsedSpec(
        project_type=ip.Field("embedded_firmware", 0.9),
        project_class=ip.Field("unknown", 0.0),
        raw_text="",
    )
    conflicts = ip.detect_conflicts(ps)
    assert any(c.id == "embedded_class_ambiguous" for c in conflicts)
    c = next(c for c in conflicts if c.id == "embedded_class_ambiguous")
    assert len(c.options) >= 2


def test_webapp_class_ambiguous_conflict_fires():
    ps = ip.ParsedSpec(
        project_type=ip.Field("web_app", 0.9),
        project_class=ip.Field("unknown", 0.0),
        raw_text="",
    )
    conflicts = ip.detect_conflicts(ps)
    assert any(c.id == "webapp_class_ambiguous" for c in conflicts)


def test_research_class_ambiguous_conflict_fires():
    ps = ip.ParsedSpec(
        project_type=ip.Field("research", 0.9),
        project_class=ip.Field("unknown", 0.0),
        raw_text="",
    )
    conflicts = ip.detect_conflicts(ps)
    assert any(c.id == "research_class_ambiguous" for c in conflicts)


def test_known_class_does_not_fire_ambiguity():
    ps = ip.ParsedSpec(
        project_type=ip.Field("embedded_firmware", 0.9),
        project_class=ip.Field("embedded_product", 0.8),
        raw_text="",
    )
    conflicts = ip.detect_conflicts(ps)
    assert not any(c.id == "embedded_class_ambiguous" for c in conflicts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Planner router
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPlannerRouter:
    ALL_CLASSES = [
        "embedded_product", "algo_sim", "optical_sim",
        "iso_standard", "test_tool", "factory_tool", "enterprise_web",
    ]

    def test_each_class_routes_to_its_planner(self):
        for cls in self.ALL_CLASSES:
            spec = ip.ParsedSpec(project_class=ip.Field(cls, 0.9))
            cfg = route_to_planner(spec)
            assert isinstance(cfg, PlannerConfig)
            assert cfg.planner_id != "general", (
                f"class {cls!r} fell through to general planner"
            )
            assert cfg.prompt_supplement, (
                f"class {cls!r} has empty prompt supplement"
            )

    def test_unknown_class_routes_to_general(self):
        spec = ip.ParsedSpec(project_class=ip.Field("unknown", 0.0))
        cfg = route_to_planner(spec)
        assert cfg.planner_id == "general"

    def test_all_planner_ids_unique(self):
        ids = get_planner_ids()
        assert len(ids) == len(set(ids))

    def test_get_config_for_class_returns_correct_planner(self):
        cfg = get_config_for_class("embedded_product")
        assert cfg.planner_id == "embedded"
        cfg_unknown = get_config_for_class("nonexistent")
        assert cfg_unknown.planner_id == "general"

    def test_embedded_planner_has_skill_pack_hint(self):
        cfg = get_config_for_class("embedded_product")
        assert cfg.skill_pack_hint is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LLM parse includes project_class
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_llm_parse_returns_project_class():
    async def ask_fn(model, prompt):
        return json.dumps({
            "project_type": {"value": "embedded_firmware", "confidence": 0.9},
            "project_class": {"value": "embedded_product", "confidence": 0.85},
            "runtime_model": {"value": "unknown", "confidence": 0.0},
            "target_arch": {"value": "arm64", "confidence": 0.9},
            "target_os": {"value": "linux", "confidence": 0.9},
            "framework": {"value": "embedded", "confidence": 0.9},
            "persistence": {"value": "none", "confidence": 0.9},
            "deploy_target": {"value": "edge_device", "confidence": 0.9},
            "hardware_required": {"value": "yes", "confidence": 0.9},
        }), 80

    p = await ip.parse_intent(
        "Build UVC camera firmware for RK3588",
        ask_fn=ask_fn, model="test",
    )
    assert p.project_class.value == "embedded_product"
    assert p.project_class.confidence == 0.85
