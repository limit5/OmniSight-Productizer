"""OP-777 release notes generator tests."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from backend.agents import jira_dispatch
from backend.agents import release_notes_generator as rng


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE = REPO_ROOT / "deploy" / "systemd" / "release-notes-generator.service"
ADR = REPO_ROOT / "docs" / "adr" / "ADR-0011-sprint-d-implementation-plan.md"
LESSON = (
    REPO_ROOT
    / "docs"
    / "sop"
    / "lessons"
    / "L-OP-777-release-notes-should-be-reviewable-patchsets.md"
)


class FakeJira:
    def __init__(self, tickets: list[rng.ReleaseTicket]) -> None:
        self.tickets = tickets
        self.seen_versions: list[str] = []

    def tickets_for_fix_version(self, version: str) -> list[rng.ReleaseTicket]:
        self.seen_versions.append(version)
        return self.tickets


class FakePusher:
    def __init__(self, url: str = "https://sora.services/c/omnisight/OmniSight-Productizer/+/777") -> None:
        self.url = url
        self.calls: list[tuple[Path, str, str]] = []

    def push(
        self,
        repo: Path,
        *,
        agent_class: str,
        target: str,
    ) -> jira_dispatch.GerritPushResult:
        self.calls.append((repo, agent_class, target))
        return jira_dispatch.GerritPushResult(
            success=True,
            change_number=777,
            change_url=self.url,
            detail="ok",
        )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "OP-777 Test")
    _git(repo, "config", "user.email", "op-777@example.test")
    (repo / "docs" / "sop" / "lessons").mkdir(parents=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    return repo


def _synthetic_tickets() -> list[rng.ReleaseTicket]:
    return [
        rng.ReleaseTicket(
            key="OP-701",
            summary="Add release dashboard export",
            component="META",
            status="Published",
            issue_type="Story",
            labels=("area:backend",),
        ),
        rng.ReleaseTicket(
            key="OP-702",
            summary="Fix milestone query pagination",
            component="META",
            status="Published",
            issue_type="Bug",
            labels=("area:backend", "bug"),
        ),
        rng.ReleaseTicket(
            key="OP-703",
            summary="Harden runner cursor recovery",
            component="META",
            status="Published",
            issue_type="Story",
            labels=("area:devops", "reliability"),
        ),
        rng.ReleaseTicket(
            key="OP-704",
            summary="Expose operator changelog summary",
            component="DOCS",
            status="Published",
            issue_type="Story",
            labels=("area:docs",),
        ),
        rng.ReleaseTicket(
            key="OP-705",
            summary="Drop legacy release note name",
            component="META",
            status="Published",
            issue_type="Story",
            labels=("area:backend", "breaking-change"),
        ),
    ]


def test_synthetic_milestone_with_five_tickets_groups_notes_correctly(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs" / "sop" / "lessons" / "L-OP-703-runner-cursors.md").write_text(
        "---\n"
        "id: L-OP-703\n"
        "ticket: OP-703\n"
        "title: Runner cursors must survive restarts\n"
        "---\n",
        encoding="utf-8",
    )
    _git(repo, "add", "docs/sop/lessons/L-OP-703-runner-cursors.md")
    _git(repo, "commit", "-m", "add lesson")
    jira = FakeJira(_synthetic_tickets())
    pusher = FakePusher()
    events: list[tuple[str, dict]] = []

    result = rng.draft_release_notes_for_event(
        {"event": "release_tagged", "fixVersion": "v9.99.0"},
        repo=repo,
        jira=jira,
        pusher=pusher,
        prepare_review=lambda _repo: None,
        event_sink=lambda event, payload: events.append((event, payload)),
        notify=lambda _channel, _severity, _detail: None,
    )

    notes = (repo / "docs" / "releases" / "v9.99.0.md").read_text(encoding="utf-8")
    assert result.status == "drafted"
    assert jira.seen_versions == ["v9.99.0"]
    assert pusher.calls == [(repo, "subscription-codex", "develop")]
    assert "## Features" in notes
    assert "### backend" in notes
    assert "- OP-701: Add release dashboard export" in notes
    assert "### docs" in notes
    assert "- OP-704: Expose operator changelog summary" in notes
    assert "## Bug fixes" in notes
    assert "- OP-702: Fix milestone query pagination" in notes
    assert "## Internal / runner reliability" in notes
    assert "- OP-703: Harden runner cursor recovery" in notes
    assert "## Lessons learned" in notes
    assert "[Runner cursors must survive restarts](../sop/lessons/L-OP-703-runner-cursors.md)" in notes
    assert "## Breaking changes" in notes
    assert "- OP-705: Drop legacy release note name" in notes
    assert _git(repo, "log", "-1", "--pretty=%s") == "Draft release notes for v9.99.0"
    assert "[Tier-S]" in _git(repo, "log", "-1", "--pretty=%B")
    assert events == [
        (
            "release_notes_drafted",
            {
                "fixVersion": "v9.99.0",
                "notes_path": "docs/releases/v9.99.0.md",
                "ticket_count": 5,
                "lesson_count": 1,
                "gerrit_change_url": pusher.url,
            },
        )
    ]


def test_run_once_reads_release_tagged_event_and_advances_cursor(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    event_log = tmp_path / "systemd.log"
    cursor = tmp_path / "cursor"
    event_log.write_text(
        "systemd prefix "
        + json.dumps({"event": "release_tagged", "tag": "v9.99.0"})
        + "\n",
        encoding="utf-8",
    )

    results = rng.run_once(
        event_log=event_log,
        cursor=cursor,
        repo=repo,
        jira=FakeJira(_synthetic_tickets()),
        pusher=FakePusher(),
        prepare_review=lambda _repo: None,
        notify=lambda _channel, _severity, _detail: None,
        event_sink=lambda _event, _payload: None,
    )

    assert [result.status for result in results] == ["drafted"]
    assert int(cursor.read_text(encoding="utf-8")) == event_log.stat().st_size
    assert (repo / "docs" / "releases" / "v9.99.0.md").exists()


def test_non_release_tagged_event_is_ignored(tmp_path: Path) -> None:
    result = rng.draft_release_notes_for_event(
        {"event": "main_promoted", "fixVersion": "v9.99.0"},
        repo=_repo(tmp_path),
        jira=FakeJira(_synthetic_tickets()),
        pusher=FakePusher(),
        prepare_review=lambda _repo: None,
    )

    assert result.status == "ignored"


def test_systemd_service_tails_release_log_with_sub_five_minute_poll() -> None:
    text = SERVICE.read_text(encoding="utf-8")

    assert "ExecStart=/usr/bin/python3 -m backend.agents.release_notes_generator" in text
    assert "--event-log /home/user/work/sora/logs/release-milestone/systemd.log" in text
    assert "--poll-seconds 60" in text
    assert "Restart=always" in text


def test_docs_record_release_notes_patchset_contract() -> None:
    adr = ADR.read_text(encoding="utf-8")
    lesson = LESSON.read_text(encoding="utf-8")

    assert "backend.agents.release_notes_generator" in adr
    assert "release_notes_drafted" in adr
    assert "ticket: OP-777" in lesson
    assert "operator-editable Gerrit patchsets" in lesson
