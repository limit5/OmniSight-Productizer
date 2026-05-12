"""OP-969 / AUDIT-18d — release:force-promote end-to-end integration (ADR-0019).

The operator emergency-override contract spans two components built in
isolation:

* OP-967 (AUDIT-18b) — ``scripts/release_milestone_checker.py``: a fixVersion
  carrying the ``release:force-promote`` label turns what would be a
  ``milestone_blocked`` event into ``milestone_force_promoted`` (gates green ⇒
  no-op), with the bypassed-gate ``reasons`` riding along.
* OP-968 (AUDIT-18c) — ``backend/agents/auto_promote_main.py``: that event is a
  green-equivalent authorization; the develop→main Gerrit review change gets the
  ``OPERATOR FORCE-PROMOTE WARNING:`` block as its description and the audit row
  records ``outcome=force_promoted`` (distinct from a genuine ``success``).

Their per-component unit coverage lives in ``test_release_milestone_checker.py``
and ``test_auto_promote_main.py``. This file is the integration seam — it
stitches the real wire flow on a real git remote:

    fixVersion has ``release:force-promote`` + a red gate
      → checker emits ``milestone_force_promoted`` (operator_override, reasons)
      → that JSON record lands in the release-milestone event log
      → ``auto_promote_main.run_once`` consumes it
      → it pushes ``develop`` → ``refs/for/main`` (never ``refs/heads/main``)
        with the warning block as the Gerrit change description
      → the audit row says ``outcome=force_promoted`` + carries the blockers.

The milestone-checker test doubles (``FakeJira`` / ``FakeGerrit`` /
``FakeStatusReader``) are imported from ``test_release_milestone_checker`` so
this seam exercises exactly the same gate-evaluation surface the unit tests pin.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.agents import auto_promote_main as apm
from backend.tests.test_release_milestone_checker import (
    FakeGerrit,
    FakeJira,
    FakeStatusReader,
    checker,
)


_NOW = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _release_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone + a bare ``gerrit`` remote, ``develop`` one commit ahead of ``main``."""
    remote = tmp_path / "gerrit.git"
    repo = tmp_path / "sora-bridge"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    # The promote push uses ``git push -o ...``; the receiving end must advertise
    # push options or git aborts before contacting it (real Gerrit advertises them).
    subprocess.run(
        ["git", "-C", str(remote), "config", "receive.advertisePushOptions", "true"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "OP-969 Integration")
    _git(repo, "config", "user.email", "op-969@example.test")
    _git(repo, "remote", "add", "gerrit", str(remote))
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "gerrit", "main:main")
    _git(repo, "checkout", "-b", "develop")
    (repo / "feature.txt").write_text("v9.99.0 feature\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature for v9.99.0")
    _git(repo, "push", "gerrit", "develop:develop")
    return repo, remote


class _RealPushRecorder:
    """Delegates to the real :func:`apm._push_for_review` but records the kwargs.

    Lets the seam test both (a) exercise a genuine ``git push HEAD:refs/for/main``
    against a bare remote and (b) inspect the ``change_description`` the module
    threaded through as the Gerrit ``message`` push option — a plain bare repo
    silently ignores that option, so it can't be read back off the remote.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> apm.PushOutcome:
        self.calls.append(kwargs)
        return apm._push_for_review(**kwargs)


def _emitted_events(captured_stdout: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in captured_stdout.splitlines()
        if line.strip().startswith("{")
    ]


def _run_checker(*, tickets, labels) -> dict[str, Any]:
    """Run ``check_all`` over a single synthetic fixVersion; return its one event."""
    checker.check_all(
        jira=FakeJira(tickets, labels=labels),
        gerrit=FakeGerrit(t.key for t in tickets),
        status_reader=FakeStatusReader(_NOW),
        now=_NOW,
    )


# ─────────────────────────────────────────────────────────────────────
# Force-promote: label → milestone_force_promoted → refs/for/main with
# the warning block → audit row outcome=force_promoted
# ─────────────────────────────────────────────────────────────────────


def test_force_promote_end_to_end_label_to_audit_row(tmp_path: Path, capsys) -> None:
    # 1. fixVersion v9.99.0: one in-progress ticket (a red gate) AND the operator
    #    override label ⇒ the checker emits ``milestone_force_promoted`` in place
    #    of ``milestone_blocked``, carrying the bypassed gate verbatim.
    tickets = [
        checker.JiraTicket(key="OP-901", status="公開済み"),
        checker.JiraTicket(key="OP-902", status="進行中"),
    ]
    _run_checker(tickets=tickets, labels=[checker.FORCE_PROMOTE_LABEL])
    emitted = _emitted_events(capsys.readouterr().out)
    assert len(emitted) == 1
    event_record = emitted[0]
    assert event_record["event"] == "milestone_force_promoted"
    assert event_record["operator_override"] is True
    assert event_record["fixVersion"] == "v9.99.0"
    assert event_record["reasons"] == [
        {"gate": "jira_fixversion", "code": "tickets_not_published", "tickets": ["OP-902"]}
    ]

    # 2. that JSON record lands in the release-milestone event log the
    #    auto-promote consumer tails — with a systemd-journal-shaped prefix in
    #    front of the JSON (the consumer's parser tolerates that).
    repo, remote = _release_repo(tmp_path)
    develop_tip = _git(repo, "rev-parse", "develop")
    main_before = _git(remote, "rev-parse", "refs/heads/main")
    event_log = tmp_path / "release-milestone-systemd.log"
    cursor = tmp_path / "auto-promote.cursor"
    event_log.write_text(
        "May 12 08:00:01 host release-milestone[1]: " + json.dumps(event_record) + "\n",
        encoding="utf-8",
    )

    # 3. the consumer treats the event as a green-equivalent authorization and
    #    pushes develop → refs/for/main with the warning block as the change
    #    description.
    pusher = _RealPushRecorder()
    alerts: list[tuple[str, str, str]] = []
    events: list[tuple[str, dict]] = []
    audits: list[tuple[str, dict]] = []
    results = apm.run_once(
        event_log=event_log,
        cursor=cursor,
        repo=repo,
        remote="gerrit",
        source_branch="develop",
        target_branch="main",
        notify=lambda c, s, d: alerts.append((c, s, d)),
        event_sink=lambda e, p: events.append((e, p)),
        audit_sink=lambda a, p: audits.append((a, p)),
        push_for_review=pusher,
    )
    assert [r.status for r in results] == ["change_created"]

    # 4. main advanced *through Gerrit review*, not a side-door push:
    #    refs/for/main now points at the develop tip; refs/heads/main is unchanged.
    assert _git(remote, "rev-parse", "refs/for/main") == develop_tip
    assert _git(remote, "rev-parse", "refs/heads/main") == main_before

    # 5. the Gerrit change carries the ADR-0019 warning prefix naming the
    #    bypassed gate(s).
    assert len(pusher.calls) == 1
    description = pusher.calls[0]["change_description"]
    assert description.startswith(apm.FORCE_PROMOTE_WARNING_PREFIX)
    assert description.startswith("OPERATOR FORCE-PROMOTE WARNING: gates ")
    assert "tickets_not_published" in description  # the bypassed gate is named

    # 6. the audit row records ``outcome=force_promoted`` (distinct from a genuine
    #    milestone_ready ``success``) and preserves the original blockers + the
    #    warning block — i.e. it is queryable apart from a normal promotion
    #    (``release_audit WHERE outcome='force_promoted'``).
    assert len(audits) == 1
    action, payload = audits[0]
    assert action == apm.AUDIT_ACTION_MAIN_PROMOTE_CHANGE_CREATED
    assert payload["force_promoted"] is True
    after = payload["after"]
    assert after["status"] == "change_created"
    assert after["outcome"] == apm.PROMOTE_OUTCOME_FORCE_PROMOTED == "force_promoted"
    assert "tickets_not_published" in after["force_promote_reasons"]
    assert after["change_description"].startswith(apm.FORCE_PROMOTE_WARNING_PREFIX)

    # operator notification + structured event both flag the override.
    assert len(alerts) == 1 and alerts[0][1] == "warning"
    assert alerts[0][2].startswith(apm.FORCE_PROMOTE_WARNING_PREFIX)
    assert events and events[0][0] == apm.EVENT_MAIN_PROMOTE_CHANGE_CREATED
    assert events[0][1]["force_promoted"] is True


# ─────────────────────────────────────────────────────────────────────
# Contrast: gates green ⇒ the label is a no-op end to end — a plain
# milestone_ready flows through with no warning artifact, outcome=success
# ─────────────────────────────────────────────────────────────────────


def test_force_promote_label_is_noop_end_to_end_when_gates_green(
    tmp_path: Path, capsys
) -> None:
    tickets = [
        checker.JiraTicket(key="OP-901", status="公開済み"),
        checker.JiraTicket(key="OP-902", status="公開済み"),
    ]
    # The override label is set, but every gate is green ⇒ ADR-0019 §"Emit":
    # plain ``milestone_ready``, no warning block.
    _run_checker(tickets=tickets, labels=[checker.FORCE_PROMOTE_LABEL])
    emitted = _emitted_events(capsys.readouterr().out)
    assert len(emitted) == 1
    event_record = emitted[0]
    assert event_record["event"] == "milestone_ready"
    assert "operator_override" not in event_record

    repo, remote = _release_repo(tmp_path)
    develop_tip = _git(repo, "rev-parse", "develop")
    main_before = _git(remote, "rev-parse", "refs/heads/main")
    event_log = tmp_path / "release-milestone-systemd.log"
    cursor = tmp_path / "auto-promote.cursor"
    event_log.write_text(json.dumps(event_record) + "\n", encoding="utf-8")

    pusher = _RealPushRecorder()
    audits: list[tuple[str, dict]] = []
    results = apm.run_once(
        event_log=event_log,
        cursor=cursor,
        repo=repo,
        remote="gerrit",
        source_branch="develop",
        target_branch="main",
        notify=lambda *a: None,
        event_sink=lambda *a: None,
        audit_sink=lambda a, p: audits.append((a, p)),
        push_for_review=pusher,
    )

    assert [r.status for r in results] == ["change_created"]
    assert _git(remote, "rev-parse", "refs/for/main") == develop_tip
    assert _git(remote, "rev-parse", "refs/heads/main") == main_before
    # No force-promote artifact rides a genuine ready promotion.
    assert pusher.calls[0]["change_description"] == ""
    action, payload = audits[0]
    assert action == apm.AUDIT_ACTION_MAIN_PROMOTE_CHANGE_CREATED
    assert "force_promoted" not in payload
    after = payload["after"]
    assert after["outcome"] == apm.PROMOTE_OUTCOME_SUCCESS == "success"
    assert "force_promote_reasons" not in after
