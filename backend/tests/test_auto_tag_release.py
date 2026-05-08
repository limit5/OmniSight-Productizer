"""OP-769 staging_passed -> release tag automation tests."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from backend.agents import auto_tag_release as atr


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE = REPO_ROOT / "deploy" / "systemd" / "auto-tag-release.service"
ADR = REPO_ROOT / "docs" / "adr" / "0011-sprint-d-implementation-plan.md"
LESSON = (
    REPO_ROOT
    / "docs"
    / "sop"
    / "lessons"
    / "L-OP-769-release-tags-are-write-once.md"
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


def _repo_with_remotes(tmp_path: Path) -> tuple[Path, Path, Path]:
    gerrit = tmp_path / "gerrit.git"
    gitlab = tmp_path / "gitlab.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(gerrit)], check=True, capture_output=True)
    subprocess.run(["git", "init", "--bare", str(gitlab)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "OP-769 Test")
    _git(repo, "config", "user.email", "op-769@example.test")
    _git(repo, "remote", "add", "gerrit", str(gerrit))
    _git(repo, "remote", "add", "gitlab", str(gitlab))
    _commit_file(repo, "base.txt", "base\n")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "gerrit", "main:main")
    _git(repo, "push", "gitlab", "main:main")
    _git(repo, "checkout", "-b", "release/v9.99")
    _git(repo, "push", "gerrit", "release/v9.99:release/v9.99")
    _git(repo, "push", "gitlab", "release/v9.99:release/v9.99")
    _git(repo, "checkout", "main")
    return repo, gerrit, gitlab


def _bare_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_staging_passed_creates_tag_pushes_remotes_updates_release_branch(tmp_path: Path) -> None:
    repo, gerrit, gitlab = _repo_with_remotes(tmp_path)
    main_sha = _commit_file(repo, "release.txt", "ready\n")
    events: list[tuple[str, dict]] = []

    result = atr.tag_release_on_staging_passed(
        {
            "event": "staging_passed",
            "fixVersion": "v9.99.0",
            "release_notes_summary": "release notes summary",
        },
        repo=repo,
        tag_remotes=("gerrit", "gitlab"),
        branch_remotes=("gerrit", "gitlab"),
        event_sink=lambda event, payload: events.append((event, payload)),
    )

    assert result.status == "tagged"
    assert _git(repo, "rev-parse", "v9.99.0^{commit}") == main_sha
    assert _bare_git(gerrit, "rev-parse", "v9.99.0^{commit}") == main_sha
    assert _bare_git(gitlab, "rev-parse", "v9.99.0^{commit}") == main_sha
    assert _bare_git(gerrit, "show", "release/v9.99:release.txt") == "ready"
    assert _bare_git(gitlab, "show", "release/v9.99:release.txt") == "ready"
    assert events == [
        (
            "release_tagged",
            {
                "fixVersion": "v9.99.0",
                "tag": "v9.99.0",
                "main_branch": "main",
                "main_sha": main_sha,
                "release_branch": "release/v9.99",
                "release_branch_head": result.branch_head,
                "tag_remotes": ["gerrit", "gitlab"],
                "branch_remotes": ["gerrit", "gitlab"],
            },
        )
    ]


def test_existing_tag_at_different_sha_is_never_retagged(tmp_path: Path) -> None:
    repo, _, _ = _repo_with_remotes(tmp_path)
    old_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "-a", "v9.99.0", old_sha, "-m", "old release")
    new_sha = _commit_file(repo, "release.txt", "ready\n")

    with pytest.raises(RuntimeError, match="immutable tag"):
        atr.tag_release_on_staging_passed(
            {"event": "staging_passed", "fixVersion": "v9.99.0"},
            repo=repo,
            tag_remotes=("gerrit", "gitlab"),
            branch_remotes=("gerrit", "gitlab"),
            event_sink=lambda _event, _payload: None,
        )

    assert _git(repo, "rev-parse", "v9.99.0^{commit}") == old_sha
    assert _git(repo, "rev-parse", "main") == new_sha


def test_non_staging_passed_event_is_ignored(tmp_path: Path) -> None:
    repo, _, _ = _repo_with_remotes(tmp_path)

    result = atr.tag_release_on_staging_passed(
        {"event": "main_promoted", "fixVersion": "v9.99.0"},
        repo=repo,
    )

    assert result.status == "ignored"


def test_synthetic_staging_passed_creates_tag_within_30s(tmp_path: Path) -> None:
    repo, gerrit, _ = _repo_with_remotes(tmp_path)
    main_sha = _commit_file(repo, "release.txt", "ready\n")
    event_log = tmp_path / "staging.log"
    cursor = tmp_path / "cursor"
    event_log.write_text(
        "prefix "
        + json.dumps({"event": "staging_passed", "fixVersion": "v9.99.0"})
        + "\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    results = atr.run_once(
        event_log=event_log,
        cursor=cursor,
        repo=repo,
        main_branch="main",
        tag_remotes=("gerrit", "gitlab"),
        branch_remotes=("gerrit", "gitlab"),
        event_sink=lambda _event, _payload: None,
    )
    elapsed = time.monotonic() - started

    assert [result.status for result in results] == ["tagged"]
    assert elapsed < 30
    assert _bare_git(gerrit, "rev-parse", "v9.99.0^{commit}") == main_sha
    assert int(cursor.read_text(encoding="utf-8")) == event_log.stat().st_size


def test_systemd_service_tails_staging_log_and_pushes_both_remotes() -> None:
    text = SERVICE.read_text(encoding="utf-8")

    assert "ExecStart=/usr/bin/python3 -m backend.agents.auto_tag_release" in text
    assert "--event-log /home/user/work/sora/logs/release-milestone/staging.log" in text
    assert "--main-branch main" in text
    assert "--tag-remotes gerrit,gitlab" in text
    assert "--branch-remotes gerrit,gitlab" in text
    assert "Restart=always" in text


def test_docs_record_release_tag_contract() -> None:
    adr = ADR.read_text(encoding="utf-8")
    lesson = LESSON.read_text(encoding="utf-8")

    assert "D8: tag + release branch" in adr
    assert "backend.agents.auto_tag_release" in lesson
    assert "release_tagged" in lesson
    assert "ticket: OP-769" in lesson
