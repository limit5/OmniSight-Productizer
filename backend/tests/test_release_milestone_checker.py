"""OP-762 release milestone checker tests."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "release_milestone_checker.py"
SERVICE = REPO_ROOT / "deploy" / "systemd" / "release-milestone-checker.service"
TIMER = REPO_ROOT / "deploy" / "systemd" / "release-milestone-checker.timer"
ADR = REPO_ROOT / "docs" / "adr" / "ADR-0010-deployment-automation.md"
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
    def __init__(self, tickets, blockers=None, labels=None, labels_error=None):
        self.tickets = tickets
        self.blockers = blockers or []
        self.labels = list(labels or [])
        self.labels_error = labels_error

    def open_fix_versions(self):
        return [checker.FixVersion("v9.99.0")]

    def tickets_for_fix_version(self, version):
        assert version == "v9.99.0"
        return self.tickets

    def highest_open_affects_tickets(self, version):
        assert version == "v9.99.0"
        return self.blockers

    def fix_version_labels(self, version):
        assert version == "v9.99.0"
        if self.labels_error is not None:
            raise self.labels_error
        return list(self.labels)


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


# ── OP-959 — Gerrit 3.13 query response shape ──────────────────────


def _gerrit_client() -> Any:
    return checker.SshGerritClient(
        host="codex-bot@gerrit.example",
        port=29418,
        key_path=Path("/dev/null"),
        project="omnisight/OmniSight-Productizer",
    )


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr="")


def test_query_passes_current_patch_set_flag_and_parses_revision() -> None:
    """OP-959 case 1: ``_query`` invokes ``gerrit query --current-patch-set``
    so a Gerrit 3.13 response carries ``currentPatchSet`` and ``develop_tip``
    parses the revision."""
    gerrit = _gerrit_client()
    # 3.13 only emits this block *because* the flag is present.
    response = "\n".join(
        [
            json.dumps(
                {
                    "project": "omnisight/OmniSight-Productizer",
                    "branch": "develop",
                    "status": "MERGED",
                    "currentPatchSet": {"revision": "deadbeefcafe1234"},
                }
            ),
            json.dumps({"type": "stats", "rowCount": 1}),
        ]
    )
    with mock.patch.object(checker.subprocess, "run", return_value=_completed(response)) as run:
        revision = gerrit.develop_tip()

    assert revision == "deadbeefcafe1234"
    (cmd,), kwargs = run.call_args
    assert "--current-patch-set" in cmd
    # flag sits in the gerrit-query argv segment, before the free-form query
    assert cmd.index("--current-patch-set") > cmd.index("query")
    assert cmd.index("--current-patch-set") < cmd.index(
        "project:omnisight/OmniSight-Productizer branch:develop status:merged"
    )


def test_develop_tip_raises_pointing_at_flag_on_stale_3_13_shape() -> None:
    """OP-959 case 2 (defensive): if Gerrit still returns a row without
    ``currentPatchSet`` (e.g. flag silently dropped), the error names the
    missing flag."""
    gerrit = _gerrit_client()
    response = "\n".join(
        [
            json.dumps({"project": "p", "branch": "develop", "status": "MERGED"}),
            json.dumps({"type": "stats", "rowCount": 1}),
        ]
    )
    with mock.patch.object(checker.subprocess, "run", return_value=_completed(response)):
        with pytest.raises(RuntimeError, match="current-patch-set"):
            gerrit.develop_tip()


def test_ticket_merged_on_develop_uses_current_patch_set_flag() -> None:
    """All ``_query`` callers go through the patched argv, so the flag is
    present uniformly (SubprocessFlagDrift guard)."""
    gerrit = _gerrit_client()
    response = json.dumps({"id": "I123", "branch": "develop", "status": "MERGED"})
    with mock.patch.object(checker.subprocess, "run", return_value=_completed(response)) as run:
        assert gerrit.ticket_merged_on_develop("OP-959") is True
    (cmd,), _ = run.call_args
    assert "--current-patch-set" in cmd


# ── OP-1589 RT-22 — retired unattended release:force-promote labels ──


_NOW = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)


def _eval(*, tickets, labels=None, labels_error=None):
    return checker.evaluate_version(
        "v9.99.0",
        jira=FakeJira(tickets, labels=labels, labels_error=labels_error),
        gerrit=FakeGerrit(ticket.key for ticket in tickets),
        status_reader=FakeStatusReader(_NOW),
        now=_NOW,
    )


def test_parse_fix_version_labels_mirrors_cron_heredoc() -> None:
    """Union of the version's ``labels`` array and ``release:*`` tokens in
    its description — the same surface release_conductor_cron.sh reads."""
    rows = [
        {"name": "v0.4.0", "labels": ["release:skip-auto-conductor"]},
        {
            "name": "v9.99.0",
            "labels": ["release:force-promote", "", None],
            "description": "ops note: also see release:force-create somewhere",
        },
    ]
    assert checker.parse_fix_version_labels(rows, "v9.99.0") == {
        "release:force-promote",
        "release:force-create",
    }
    # A version with no matching row contributes nothing.
    assert checker.parse_fix_version_labels(rows, "v1.2.3") == set()


def test_resolve_force_promote_distinguishes_present_absent_and_typo() -> None:
    exact = checker.resolve_force_promote(["release:force-promote"], "v9.99.0")
    assert isinstance(exact, checker.MilestoneResult)
    assert exact.event == "LabelInvalid"
    assert exact.reasons[0]["code"] == "label_format_collision"
    assert checker.resolve_force_promote(["release:skip-auto-conductor"], "v9.99.0") is False
    assert checker.resolve_force_promote([], "v9.99.0") is False
    typo = checker.resolve_force_promote(["release:force_promote"], "v9.99.0")
    assert isinstance(typo, checker.MilestoneResult)
    assert typo.event == "LabelInvalid"
    assert typo.reasons[0]["code"] == "label_format_collision"
    assert typo.reasons[0]["labels"] == ["release:force_promote"]


def test_force_promote_label_is_rejected_instead_of_force_promoted() -> None:
    """RT-22: the old fixVersion label must not turn red gates green."""
    tickets = _tickets(["公開済み", "進行中"])
    result = _eval(tickets=tickets, labels=["release:force-promote"])

    assert result.event == "LabelInvalid"
    assert result.operator_override is False
    assert result.reasons == (
        {
            "gate": "operator_label",
            "code": "label_format_collision",
            "labels": ["release:force-promote"],
            "detail": (
                "fixVersion label(s) ['release:force-promote'] match the retired "
                "unattended force-promote path; use the audited human "
                "release-train break-glass command instead"
            ),
        },
    )


def test_without_force_promote_label_stays_blocked() -> None:
    """Same input, no label → ``milestone_blocked`` (AC #4)."""
    tickets = _tickets(["公開済み", "進行中"])
    result = _eval(tickets=tickets)

    assert result.event == "milestone_blocked"
    assert result.operator_override is False
    assert result.reasons == (
        {
            "gate": "jira_fixversion",
            "code": "tickets_not_published",
            "tickets": ["OP-902"],
        },
    )


def test_force_promote_label_is_rejected_even_when_gates_green() -> None:
    """RT-22: the retired unattended force label is invalid, not a no-op."""
    tickets = _tickets(["公開済み", "公開済み"])
    result = _eval(tickets=tickets, labels=["release:force-promote"])

    assert result.event == "LabelInvalid"
    assert result.operator_override is False
    assert result.reasons[0]["code"] == "label_format_collision"


def test_force_promote_typo_label_emits_label_invalid() -> None:
    """``release:force_promote`` (underscored) is rejected, not honored —
    same pattern as AUDIT-13a's ``release:force_create`` rejection."""
    tickets = _tickets(["公開済み", "進行中"])
    result = _eval(tickets=tickets, labels=["release:force_promote"])

    assert result.event == "LabelInvalid"
    assert result.operator_override is False
    assert result.reasons[0]["code"] == "label_format_collision"
    assert result.reasons[0]["labels"] == ["release:force_promote"]


def test_label_lookup_failure_fails_closed(capsys) -> None:
    """``LabelLookupFailedDuringForce`` — a JIRA hiccup fetching the labels
    is treated as *no override* (fail closed) + logged to stderr."""
    tickets = _tickets(["公開済み", "進行中"])
    result = _eval(tickets=tickets, labels_error=RuntimeError("JIRA 503"))

    assert result.event == "milestone_blocked"
    assert result.operator_override is False
    err = capsys.readouterr().err
    assert "LabelLookupFailedDuringForce" in err
    assert "JIRA 503" in err


def test_check_all_emits_label_invalid_not_force_promoted(capsys) -> None:
    tickets = _tickets(["進行中"])
    checker.check_all(
        jira=FakeJira(tickets, labels=["release:force-promote"]),
        gerrit=FakeGerrit(ticket.key for ticket in tickets),
        status_reader=FakeStatusReader(_NOW),
        now=_NOW,
    )
    out = capsys.readouterr().out
    assert '"event": "LabelInvalid"' in out
    assert "milestone_force_promoted" not in out
    assert "operator_override" not in out


def test_checker_no_longer_emits_force_promoted_event() -> None:
    """Static guard for RT-22: the checker has no green force event path."""
    text = SCRIPT.read_text()
    assert 'event="milestone_force_promoted"' not in text
    assert '"milestone_force_promoted"' not in text
