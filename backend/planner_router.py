"""C0 — ProjectClass-based planner routing.

Maps a ParsedSpec's ``project_class`` to the correct planner
configuration. Each project class gets a tailored system-prompt
supplement and DAG-generation strategy so the orchestrator produces
domain-appropriate task graphs.

The router is called after intent parsing and before DAG drafting:

    planner_cfg = route_to_planner(parsed_spec)
    # planner_cfg.planner_id → which planner to invoke
    # planner_cfg.prompt_supplement → extra context for the LLM
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from backend.intent_parser import ParsedSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannerConfig:
    planner_id: str
    prompt_supplement: str
    skill_pack_hint: Optional[str] = None


_PLANNER_REGISTRY: dict[str, PlannerConfig] = {
    "embedded_product": PlannerConfig(
        planner_id="embedded",
        prompt_supplement=(
            "You are planning an embedded product (firmware + hardware). "
            "Generate a DAG covering: BSP bring-up, kernel config, driver "
            "integration, protocol layer, application logic, OTA, HIL "
            "tests, and compliance artifacts."
        ),
        skill_pack_hint="SKILL-*",
    ),
    "algo_sim": PlannerConfig(
        planner_id="algo_sim",
        prompt_supplement=(
            "You are planning an algorithm/ML simulation project. "
            "Generate a DAG covering: environment setup, dataset "
            "preparation, model training/evaluation, benchmark runs, "
            "result archival, and reproducibility artifacts."
        ),
    ),
    "optical_sim": PlannerConfig(
        planner_id="optical_sim",
        prompt_supplement=(
            "You are planning an optical simulation project. "
            "Generate a DAG covering: lens prescription entry, "
            "ray-tracing runs, tolerance analysis, MTF/spot-diagram "
            "generation, and design report."
        ),
    ),
    "iso_standard": PlannerConfig(
        planner_id="iso_standard",
        prompt_supplement=(
            "You are planning an ISO/IEC standard implementation. "
            "Generate a DAG covering: requirements traceability, "
            "design documentation, formal verification hooks, "
            "certification artifacts, and compliance checklists."
        ),
    ),
    "test_tool": PlannerConfig(
        planner_id="test_tool",
        prompt_supplement=(
            "You are planning a test/validation tool. Generate a DAG "
            "covering: fixture setup, test sequencer, result "
            "collection, report generation, and CI integration."
        ),
    ),
    "factory_tool": PlannerConfig(
        planner_id="factory_tool",
        prompt_supplement=(
            "You are planning a factory/production-line tool. "
            "Generate a DAG covering: jig control, test sequencer, "
            "MES integration, yield dashboard, and station lockout."
        ),
    ),
    "enterprise_web": PlannerConfig(
        planner_id="enterprise_web",
        prompt_supplement=(
            "You are planning an enterprise web application. "
            "Generate a DAG covering: auth/SSO, RBAC, audit logging, "
            "CRUD modules, report/chart views, i18n, multi-tenant "
            "support, and import/export."
        ),
    ),
}

_DEFAULT_CONFIG = PlannerConfig(
    planner_id="general",
    prompt_supplement=(
        "Generate a general-purpose DAG based on the project spec. "
        "Include setup, implementation, testing, and deployment tasks."
    ),
)


def route_to_planner(spec: ParsedSpec) -> PlannerConfig:
    """Select the planner configuration for the given spec.

    Returns the class-specific config when ``project_class`` is known,
    otherwise falls back to a general-purpose config.
    """
    pc = spec.project_class.value
    cfg = _PLANNER_REGISTRY.get(pc)
    if cfg is not None:
        logger.info("planner_router: class=%s → planner=%s", pc, cfg.planner_id)
        return cfg
    logger.info("planner_router: class=%s (unknown) → general planner", pc)
    return _DEFAULT_CONFIG


def get_planner_ids() -> list[str]:
    """Return all registered planner IDs (for introspection / tests)."""
    return sorted(cfg.planner_id for cfg in _PLANNER_REGISTRY.values())


def get_config_for_class(project_class: str) -> PlannerConfig:
    """Look up config by class name. Returns default if not found."""
    return _PLANNER_REGISTRY.get(project_class, _DEFAULT_CONFIG)
