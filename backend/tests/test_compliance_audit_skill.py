"""BP.D.7 contract tests for auxiliary compliance audit skills."""

from __future__ import annotations

from pathlib import Path

import yaml

from backend.skill_registry import get_skill, list_skills, validate_skill


_SKILL_NAME = "compliance-audit"
_SKILL_DIR = Path("configs/skills") / _SKILL_NAME
_EXPECTED_SKILLS = {
    "audit_iec62304_traceability_auxiliary",
    "scan_phi_data_leakage_auxiliary",
    "audit_iso13485_design_controls_auxiliary",
    "scan_misra_c_strict_auxiliary",
    "verify_asil_d_redundancy_auxiliary",
    "audit_autosar_interface_contract_auxiliary",
    "analyze_state_machine_deadlocks_auxiliary",
    "verify_watchdog_pet_timing_auxiliary",
    "audit_sil_claim_evidence_auxiliary",
    "verify_mcdc_100_percent_auxiliary",
    "run_formal_verification_proof_auxiliary",
    "audit_mil_std_882e_hazard_log_auxiliary",
}


def _tasks() -> list[dict[str, object]]:
    data = yaml.safe_load((_SKILL_DIR / "tasks.yaml").read_text(encoding="utf-8"))
    return data["tasks"]


def test_compliance_audit_skill_pack_is_discoverable_and_valid() -> None:
    names = {skill.name for skill in list_skills()}
    assert _SKILL_NAME in names

    result = validate_skill(_SKILL_NAME)
    assert result.ok, [(issue.level, issue.message) for issue in result.issues]

    info = get_skill(_SKILL_NAME)
    assert info is not None
    assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}


def test_compliance_audit_declares_ten_plus_auxiliary_skills() -> None:
    task_ids = {task["task_id"] for task in _tasks()}

    assert len(task_ids) >= 10
    assert _EXPECTED_SKILLS.issubset(task_ids)
    assert all(str(task_id).endswith("_auxiliary") for task_id in task_ids)


def test_compliance_audit_tasks_are_advisory_human_signoff_only() -> None:
    for task in _tasks():
        assert task["phase"] == "advisory_audit"
        assert task["requires_human_signoff"] is True
        assert task["outputs"] == ["advisory_audit_report"]
        assert task["matrix"] in {"medical", "automotive", "industrial", "military"}


def test_compliance_audit_markdown_repeats_auxiliary_disclaimer() -> None:
    body = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "advisory only" in body
    assert "AI-assisted output MUST be reviewed" in body
    assert "human certified engineer" in body
    assert '"audit_type": "advisory"' in body
    assert '"requires_human_signoff": true' in body
    for skill_name in _EXPECTED_SKILLS:
        assert f"`{skill_name}`" in body
