"""AB DoD -- seven-family batch velocity evidence drift guard."""

from __future__ import annotations

from pathlib import Path

from backend.agents.batch_eligibility import DEFAULT_ROUTING


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = PROJECT_ROOT / "docs" / "ops" / "ab_batch_velocity_evidence.md"
ADR = PROJECT_ROOT / "docs" / "operations" / "anthropic-api-migration-and-batch-mode.md"
RUNBOOK = PROJECT_ROOT / "docs" / "ops" / "anthropic-api-migration-runbook.md"


EXPECTED_FAMILIES: dict[str, tuple[str, ...]] = {
    "HD.1": (
        "hd_parse_kicad",
        "hd_parse_altium",
        "hd_parse_odb",
        "hd_parse_eagle",
    ),
    "HD.4": ("hd_diff_reference",),
    "HD.5.13": ("hd_sensor_kb_extract",),
    "HD.18.6": ("hd_cve_impact",),
    "L4.1": ("l4_determinism_regression",),
    "L4.3": ("l4_adversarial_ci",),
    "TODO routine": ("todo_routine",),
}


def _read(path: Path) -> str:
    assert path.is_file(), f"missing AB batch velocity evidence file: {path}"
    return path.read_text(encoding="utf-8")


def _normalized_lower(path: Path) -> str:
    return " ".join(_read(path).lower().split())


def test_velocity_evidence_doc_exists_and_defines_scope() -> None:
    body = _normalized_lower(EVIDENCE)

    required = [
        "ab batch velocity evidence",
        "seven batch-accelerated development task families",
        "hd.1 eda parse tasks",
        "hd.4 reference design diff tasks",
        "hd.5.13 datasheet vision extraction tasks",
        "hd.18.6 cve impact backfill tasks",
        "l4.1 determinism regression tasks",
        "l4.3 adversarial ci tasks",
        "todo routine checkbox-processing tasks",
        "current status is `dev-only`",
        "api_batch_tasks_per_day",
        ">= 2x baseline",
        "actual within 10% of estimate",
    ]

    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"AB batch velocity evidence missing scope terms: {missing}"


def test_seven_families_are_batch_eligible_with_thresholds() -> None:
    for family, task_kinds in EXPECTED_FAMILIES.items():
        for task_kind in task_kinds:
            rule = DEFAULT_ROUTING[task_kind]
            assert rule.batch_eligible, f"{family}/{task_kind} must go batch"
            assert not rule.realtime_required, f"{family}/{task_kind} cannot veto batch"
            assert rule.auto_batch_threshold is not None, (
                f"{family}/{task_kind} must have an auto-batch threshold"
            )
            assert rule.auto_batch_threshold > 0


def test_evidence_matrix_names_runtime_and_cost_artifacts() -> None:
    body = _read(EVIDENCE)

    for family, task_kinds in EXPECTED_FAMILIES.items():
        assert family in body
        for task_kind in task_kinds:
            assert task_kind in body

    for phrase in [
        "backend/agents/batch_eligibility.py::DEFAULT_ROUTING",
        "backend/tests/test_ab_batch_velocity_evidence.py",
        "ADR section 6.3",
        "HD.1 schematic parse",
        "HD.4 reference diff",
        "HD.5.13 datasheet vision",
        "HD.18.6 CVE impact",
        "L4.1 determinism regression",
        "L4.3 adversarial CI",
        "TODO `[ ]` routine 任務 batch",
    ]:
        assert phrase in body


def test_source_adr_and_runbook_link_velocity_evidence() -> None:
    adr = _read(ADR)
    runbook = _read(RUNBOOK)

    for phrase in [
        "ab_batch_velocity_evidence.md",
        "HD.1",
        "HD.4",
        "HD.5.13",
        "HD.18.6",
        "L4.1",
        "L4.3",
        "TODO routine",
    ]:
        assert phrase in adr
        assert phrase in runbook

    for phrase in [
        "api_batch_tasks_per_day",
        "wall_clock_hours_saved",
        "batch_discount_observed_pct",
        "p95_batch_completion_hours",
        "dlq_rate_pct",
        ">= 100 tasks",
        ">= 訂閱版 baseline 2x",
        "偏差 < 10%",
    ]:
        assert phrase in runbook
