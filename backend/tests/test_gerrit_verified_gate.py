"""OP-740 — Gerrit Verified label scaffolding contract.

These tests intentionally stay static: OP-740 C1 lands project.config
and provisioning/runbook scaffolding before real CI exists, so CI cannot
exercise a live Gerrit submit yet.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_CONFIG = _REPO_ROOT / ".gerrit" / "project.config"
_PROJECT_CONFIG_EXAMPLE = _REPO_ROOT / ".gerrit" / "project.config.example"
_PROVISION_SCRIPT = _REPO_ROOT / "deploy" / "scripts" / "provision-ci-bot.sh"
_RUNBOOK = _REPO_ROOT / "docs" / "ops" / "gerrit_dual_two_rule.md"
_LESSONS = _REPO_ROOT / "docs" / "sop" / "lessons-learned.md"


def _section(text: str, header: str) -> str:
    marker = f"[{header}]"
    start = text.index(marker)
    next_start = text.find("\n[", start + len(marker))
    if next_start == -1:
        return text[start:]
    return text[start:next_start]


def test_project_config_defines_verified_label_default_off_submit_requirement() -> None:
    text = _PROJECT_CONFIG.read_text(encoding="utf-8")

    verified_label = _section(text, 'label "Verified"')
    assert "function = MaxWithBlock" in verified_label
    assert "value = -1 Fails" in verified_label
    assert "value =  0 No score" in verified_label
    assert "value = +1 Verified" in verified_label
    assert (
        "copyCondition = changekind:NO_CODE_CHANGE OR changekind:TRIVIAL_REBASE"
        in verified_label
    )

    verified_requirement = _section(text, 'submit-requirement "Verified"')
    assert "applicableIf = is:false" in verified_requirement
    assert "submittableIf = label:Verified=+1" in verified_requirement
    assert "canOverrideInChildProjects = false" in verified_requirement


def test_project_config_grants_ci_bot_verified_only_on_develop_and_release() -> None:
    text = _PROJECT_CONFIG.read_text(encoding="utf-8")

    develop_acl = _section(text, 'access "refs/heads/develop"')
    assert "label-Verified = -1..+1 group ci-bot" in develop_acl
    assert "label-Code-Review" not in develop_acl

    release_acl = _section(text, 'access "refs/heads/release/*"')
    assert "label-Verified = -1..+1 group ci-bot" in release_acl
    assert "label-Code-Review" not in release_acl


def test_project_config_example_keeps_op740_blocks_in_sync() -> None:
    deployed = _PROJECT_CONFIG.read_text(encoding="utf-8")
    example = _PROJECT_CONFIG_EXAMPLE.read_text(encoding="utf-8")

    for header in (
        'access "refs/heads/develop"',
        'access "refs/heads/release/*"',
        'label "Verified"',
        'submit-requirement "Verified"',
    ):
        assert _section(example, header) == _section(deployed, header)


def test_provision_ci_bot_script_pins_key_bag_acl_and_vote_smoke_test() -> None:
    script = _PROVISION_SCRIPT.read_text(encoding="utf-8")

    assert "gerrit-ci-bot-ed25519" in script
    assert "gerrit-ci-bot-http-password" in script
    assert "chmod 0600 \"$CI_BOT_SSH_KEY\"" in script
    assert "chmod 0600 \"$CI_BOT_HTTP_PASSWORD\"" in script
    assert "create-account \"$CI_BOT_USERNAME\"" in script
    assert "set-members ai-reviewer-bots --remove \"$CI_BOT_USERNAME\"" in script
    assert "set-members non-ai-reviewer --remove \"$CI_BOT_USERNAME\"" in script
    assert "gerrit review --label Verified=+1 \"$VERIFY_CHANGE\"" in script
    assert "gerrit review --label Verified=-1 \"$VERIFY_CHANGE\"" in script
    assert "gerrit review --label Code-Review=+1 \"$VERIFY_CHANGE\"" in script


def test_runbook_documents_parallel_gate_migration_and_escape() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")

    assert "OP-740 Verified parallel gate" in runbook
    assert "Code-Review track" in runbook
    assert "Verified track" in runbook
    assert "Read the label state in the Gerrit change UI" in runbook
    assert "C2 ships real CI and flips the `Verified` requirement" in runbook
    assert "Verified CI outage placeholder" in runbook
    assert "Force-Submit escape" in runbook


def test_lesson_28_records_parallel_gate_default_off_rationale() -> None:
    lessons = _LESSONS.read_text(encoding="utf-8")

    assert "## Lesson 28" in lessons
    assert "Parallel gates should ship default-off before enforcement" in lessons
    assert "applicableIf = is:false" in lessons
