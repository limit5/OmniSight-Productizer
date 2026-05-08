"""OP-766 develop -> main auto-promotion tests."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from backend.agents import auto_promote_main as apm


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE = REPO_ROOT / "deploy" / "systemd" / "auto-promote-main.service"
ADR = REPO_ROOT / "docs" / "adr" / "ADR-0010-deployment-automation.md"
LESSON = (
    REPO_ROOT
    / "docs"
    / "sop"
    / "lessons"
    / "L-OP-766-release-promotion-must-stay-ff-only.md"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit_file(repo: Path, name: str, body: str) -> str:
    path = repo / name
    path.write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"commit {name}")
    return _git(repo, "rev-parse", "HEAD")


def _repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "OP-766 Test")
    _git(repo, "config", "user.email", "op-766@example.test")
    _git(repo, "remote", "add", "gerrit", str(remote))
    _commit_file(repo, "base.txt", "base\n")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "gerrit", "main:main")
    _git(repo, "checkout", "-b", "develop")
    _git(repo, "push", "gerrit", "develop:develop")
    return repo, remote


def test_milestone_ready_clean_develop_ahead_auto_promotes(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    events: list[tuple[str, dict]] = []
    audits: list[tuple[str, dict]] = []

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0"},
        repo=repo,
        remote="gerrit",
        event_sink=lambda event, payload: events.append((event, payload)),
        audit_sink=lambda action, payload: audits.append((action, payload)),
    )

    assert result.status == "promoted"
    assert _git(remote, "rev-parse", "main") == develop_tip
    assert events == [
        (
            "main_promoted",
            {
                "fixVersion": "v9.99.0",
                "source_branch": "develop",
                "target_branch": "main",
                "develop_tip": develop_tip,
                "main_tip": result.main_tip,
                "develop_only_count": 1,
                "main_only_count": 0,
                "promoted_tip": develop_tip,
                "pushed_ref": "develop:main",
            },
        )
    ]
    assert audits[0][0] == apm.AUDIT_ACTION_MAIN_PROMOTED


def test_milestone_ready_non_ff_aborts_and_alerts(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    _git(repo, "checkout", "main")
    main_tip = _commit_file(repo, "hotfix.txt", "hotfix\n")
    alerts: list[tuple[str, str, str]] = []
    events: list[tuple[str, dict]] = []
    audits: list[tuple[str, dict]] = []

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0"},
        repo=repo,
        remote="gerrit",
        notify=lambda channel, severity, detail: alerts.append((channel, severity, detail)),
        event_sink=lambda event, payload: events.append((event, payload)),
        audit_sink=lambda action, payload: audits.append((action, payload)),
    )

    assert result.status == "blocked"
    assert result.develop_tip == develop_tip
    assert result.main_tip == main_tip
    assert _git(remote, "rev-parse", "main") != develop_tip
    assert events == []
    assert len(alerts) == 1
    assert alerts[0][0] == "release-auto-promote"
    assert alerts[0][1] == "critical"
    assert "main-only commits" in alerts[0][2]
    assert "hotfix.txt" in alerts[0][2]
    assert audits[0][0] == apm.AUDIT_ACTION_MAIN_PROMOTE_BLOCKED


def test_non_milestone_ready_event_is_ignored(tmp_path: Path) -> None:
    repo, _ = _repo_with_remote(tmp_path)

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_blocked", "fixVersion": "v9.99.0"},
        repo=repo,
        audit_sink=lambda _action, _payload: None,
    )

    assert result.status == "ignored"


def test_run_once_reads_milestone_ready_from_release_log(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    event_log = tmp_path / "systemd.log"
    cursor = tmp_path / "cursor"
    event_log.write_text(
        "prefix that systemd may add "
        + json.dumps({"event": "milestone_ready", "fixVersion": "v9.99.0"})
        + "\n",
        encoding="utf-8",
    )

    results = apm.run_once(
        event_log=event_log,
        cursor=cursor,
        repo=repo,
        remote="gerrit",
        source_branch="develop",
        target_branch="main",
        event_sink=lambda _event, _payload: None,
        audit_sink=lambda _action, _payload: None,
    )

    assert [result.status for result in results] == ["promoted"]
    assert _git(remote, "rev-parse", "main") == develop_tip
    assert int(cursor.read_text(encoding="utf-8")) == event_log.stat().st_size


def test_systemd_service_tails_release_checker_log() -> None:
    text = SERVICE.read_text(encoding="utf-8")

    assert "ExecStart=/usr/bin/python3 -m backend.agents.auto_promote_main" in text
    assert "--event-log /home/user/work/sora/logs/release-milestone/systemd.log" in text
    assert "--source-branch develop" in text
    assert "--target-branch main" in text
    assert "Restart=always" in text


def test_docs_record_auto_promote_contract() -> None:
    adr = ADR.read_text(encoding="utf-8")
    lesson = LESSON.read_text(encoding="utf-8")

    assert "backend.agents.auto_promote_main" in adr
    assert "main_promoted" in adr
    assert "git push gerrit develop:main" in lesson
    assert "ticket: OP-766" in lesson
