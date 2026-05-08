"""OP-762 release milestone checker tests."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "release_milestone_checker.py"
SERVICE = REPO_ROOT / "deploy" / "systemd" / "release-milestone-checker.service"
TIMER = REPO_ROOT / "deploy" / "systemd" / "release-milestone-checker.timer"
ADR = REPO_ROOT / "docs" / "adr" / "0010-deployment-automation.md"
LESSON = (
    REPO_ROOT
    / "docs"
    / "sop"
    / "lessons"
    / "L-OP-762-milestones-need-machine-checkable-gates.md"
)


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location("release_milestone_checker_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["release_milestone_checker_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


checker = _load_checker()


class FakeJira:
    def __init__(self, tickets, blockers=None):
        self.tickets = tickets
        self.blockers = blockers or []

    def open_fix_versions(self):
        return [checker.FixVersion("v9.99.0")]

    def tickets_for_fix_version(self, version):
        assert version == "v9.99.0"
        return self.tickets

    def highest_open_affects_tickets(self, version):
        assert version == "v9.99.0"
        return self.blockers


class FakeGerrit:
    def __init__(self, merged):
        self.merged = set(merged)

    def develop_tip(self):
        return "develop-tip"

    def ticket_merged_on_develop(self, ticket_key):
        return ticket_key in self.merged


class FakeStatusReader:
    def __init__(self, now):
        self.now = now

    def latest(self, suite, *, branch, revision):
        assert branch == "develop"
        assert revision == "develop-tip"
        return {
            "suite": suite,
            "branch": branch,
            "revision": revision,
            "status": "green",
            "run_id": f"{suite}-run",
            "timestamp": (self.now - timedelta(minutes=10)).isoformat(),
        }


def _tickets(statuses):
    return [
        checker.JiraTicket(key=f"OP-90{i}", status=status)
        for i, status in enumerate(statuses, start=1)
    ]


def test_synthetic_one_in_progress_blocks_milestone() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    tickets = _tickets(["公開済み", "公開済み", "公開済み", "公開済み", "進行中"])
    result = checker.evaluate_version(
        "v9.99.0",
        jira=FakeJira(tickets),
        gerrit=FakeGerrit(ticket.key for ticket in tickets),
        status_reader=FakeStatusReader(now),
        now=now,
    )

    assert result.event == "milestone_blocked"
    assert result.reasons == (
        {
            "gate": "jira_fixversion",
            "code": "tickets_not_published",
            "tickets": ["OP-905"],
        },
    )


def test_synthetic_all_published_emits_ready() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    tickets = _tickets(["公開済み", "公開済み", "公開済み", "公開済み", "公開済み"])
    result = checker.evaluate_version(
        "v9.99.0",
        jira=FakeJira(tickets),
        gerrit=FakeGerrit(ticket.key for ticket in tickets),
        status_reader=FakeStatusReader(now),
        now=now,
    )

    assert result.event == "milestone_ready"
    assert result.reasons == ()


def test_blocked_event_carries_structured_reasons_for_each_red_gate() -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    tickets = _tickets(["公開済み", "公開済み"])

    class RedStatusReader:
        def latest(self, suite, *, branch, revision):
            if suite == "canary":
                return {"suite": suite, "branch": branch, "revision": revision, "status": "fail"}
            return {
                "suite": suite,
                "branch": branch,
                "revision": revision,
                "status": "green",
                "run_id": "old-smoke",
                "timestamp": (now - timedelta(hours=5)).isoformat(),
            }

    result = checker.evaluate_version(
        "v9.99.0",
        jira=FakeJira(tickets, blockers=["OP-999"]),
        gerrit=FakeGerrit(["OP-901"]),
        status_reader=RedStatusReader(),
        now=now,
    )

    assert result.event == "milestone_blocked"
    assert {reason["gate"] for reason in result.reasons} == {
        "gerrit_develop",
        "jira_blockers",
        "ci_canary",
        "smoke_suite",
    }
    assert all("code" in reason for reason in result.reasons)


def test_check_all_emits_ready_and_blocked_events(capsys) -> None:
    now = datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    tickets = _tickets(["公開済み"])

    checker.check_all(
        jira=FakeJira(tickets),
        gerrit=FakeGerrit(["OP-901"]),
        status_reader=FakeStatusReader(now),
        now=now,
    )

    out = capsys.readouterr().out
    assert '"event": "milestone_ready"' in out
    assert '"fixVersion": "v9.99.0"' in out


def test_systemd_timer_runs_every_five_minutes() -> None:
    service_text = SERVICE.read_text()
    timer_text = TIMER.read_text()

    assert (
        "ExecStart=/usr/bin/python3 "
        "/home/user/sora-bridge/scripts/release_milestone_checker.py"
    ) in service_text
    assert "Unit=release-milestone-checker.service" in timer_text
    assert "OnUnitActiveSec=5min" in timer_text
    assert "Persistent=true" in timer_text


def test_docs_created_for_deployment_automation_and_lesson() -> None:
    adr = ADR.read_text()
    lesson = LESSON.read_text()

    assert "ADR 0010" in adr
    assert "release_milestone_checker.py" in adr
    assert "milestone_ready" in adr
    assert "ticket: OP-762" in lesson
    assert "Machine-checkable release milestones" in lesson
