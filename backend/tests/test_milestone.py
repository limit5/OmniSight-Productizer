"""OP-868 fixVersion milestone definer/checker tests."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.agents import milestone_query
from backend.agents.milestone_query import MilestoneTicket, VerifiedRun


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFINE_SCRIPT = REPO_ROOT / "scripts" / "milestone_define.py"
CHECK_SCRIPT = REPO_ROOT / "scripts" / "milestone_check.py"
MIGRATION = REPO_ROOT / "backend" / "alembic" / "versions" / "0221_release_milestones.py"
RUNBOOK = REPO_ROOT / "docs" / "operations" / "milestone-runbook.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "milestone-check.yml"


def _load_script(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


define = _load_script(DEFINE_SCRIPT, "milestone_define_under_test")
check = _load_script(CHECK_SCRIPT, "milestone_check_under_test")


@pytest.fixture()
def store(tmp_path: Path) -> milestone_query.ReleaseMilestoneStore:
    engine = milestone_query.create_engine(f"sqlite:///{tmp_path / 'milestones.db'}")
    milestone_query.ensure_release_milestones_table_for_dry_run(engine)
    return milestone_query.ReleaseMilestoneStore(engine)


def test_define_happy_path_creates_jira_version_and_db_row(store) -> None:
    jira = define.DryRunJiraVersionClient()

    result = define.define_milestone("v9.99.0", jira=jira, store=store)

    assert result["version"] == "v9.99.0"
    assert result["jira_version_id"] == "dry-run-version-id"
    assert store.exists("v9.99.0") is True


def test_define_duplicate_refuses_without_second_create(store) -> None:
    jira = define.DryRunJiraVersionClient()
    define.define_milestone("v9.99.0", jira=jira, store=store)

    with pytest.raises(RuntimeError, match=milestone_query.ERR_ALREADY_EXISTS):
        define.define_milestone("v9.99.0", jira=jira, store=store)


def test_accept_green_returns_exit_zero() -> None:
    result = milestone_query.evaluate_milestone(
        "v9.99.0",
        tickets=[MilestoneTicket("OP-901", "公開済み")],
        blockers=[],
        verified_runs=[VerifiedRun("OP-901", "abc123", "green")],
    )

    assert result.ready is True
    assert result.exit_code == 0
    assert result.status == "ready"


def test_accept_not_ready_lists_unpublished_ticket() -> None:
    result = milestone_query.evaluate_milestone(
        "v9.99.0",
        tickets=[MilestoneTicket("OP-901", "進行中")],
        blockers=[],
        verified_runs=[VerifiedRun("OP-901", "abc123", "green")],
    )

    assert result.ready is False
    assert result.exit_code == 1
    assert result.errors[0]["code"] == "MilestoneTicketsNotPublished"
    assert result.errors[0]["tickets"] == [{"key": "OP-901", "status": "進行中"}]


def test_force_accept_override_skips_verified_requirement(tmp_path: Path) -> None:
    report = check.check_one(
        "v9.99.0",
        jira=_FakeJira(
            [MilestoneTicket("OP-901", "公開済み", ("milestone:force-accept",))],
            [],
        ),
        gerrit=_FakeGerrit([VerifiedRun("OP-901", "abc123", "red")]),
        audit_log=tmp_path / "audit.jsonl",
    )

    assert report["ready"] is True
    assert report["status"] == "force_accepted"
    assert report["warnings"][0]["code"] == "MilestoneForceAcceptOverride"
    assert "milestone_force_accept" in (tmp_path / "audit.jsonl").read_text()


def test_op739_missing_falls_back_to_todo_marker() -> None:
    result = milestone_query.evaluate_milestone(
        "v9.99.0",
        tickets=[MilestoneTicket("OP-901", "公開済み")],
        blockers=[],
        verified_runs=[VerifiedRun("OP-901", "abc123", "missing", label_present=False)],
        verified_required=False,
    )

    assert result.ready is False
    assert result.exit_code == 1
    assert result.warnings[0]["code"] == milestone_query.ERR_VERIFIED_LABEL_MISSING
    assert "TODO" in result.warnings[0]["detail"] or "todo" in result.warnings[0]


def test_nightly_status_change_posts_meta_comment(monkeypatch, tmp_path: Path, store) -> None:
    define.define_milestone("v9.99.0", jira=define.DryRunJiraVersionClient(), store=store)
    calls = []

    def fake_add_comment(client, key, text, idem_key=None):
        calls.append((client, key, text, idem_key))

    monkeypatch.setattr(check.jira_dispatch, "add_comment", fake_add_comment)
    report = check.check_one(
        "v9.99.0",
        jira=_FakeJira([MilestoneTicket("OP-901", "公開済み")], []),
        gerrit=_FakeGerrit([VerifiedRun("OP-901", "abc123", "green")]),
        audit_log=tmp_path / "audit.jsonl",
    )
    previous = store.update_status("v9.99.0", status=report["status"], report=report)
    check._post_status_change(
        client="client",
        meta_ticket="OP-761",
        version="v9.99.0",
        previous=previous,
        current=report["status"],
    )

    assert calls == [
        (
            "client",
            "OP-761",
            "Milestone v9.99.0 status changed: defined -> ready.",
            None,
        )
    ]


def test_migration_and_runbook_document_ci_integration() -> None:
    assert "CREATE TABLE IF NOT EXISTS release_milestones" in MIGRATION.read_text()
    assert "nightly" in RUNBOOK.read_text()
    assert "milestone:force-accept" in RUNBOOK.read_text()
    workflow = WORKFLOW.read_text()
    assert 'cron: "17 19 * * *"' in workflow
    assert "scripts/milestone_check.py" in workflow
    assert "--nightly" in workflow


class _FakeJira:
    def __init__(self, tickets, blockers):
        self.tickets = tickets
        self.blockers = blockers

    def tickets_for_fix_version(self, version):
        assert version == "v9.99.0"
        return self.tickets

    def open_blockers_for_fix_version(self, version):
        assert version == "v9.99.0"
        return self.blockers


class _FakeGerrit:
    def __init__(self, runs):
        self.runs = runs

    def verified_runs_for_tickets(self, tickets):
        assert tickets
        return self.runs
