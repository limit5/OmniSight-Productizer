"""OP-750 orphan commit salvage tests."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.agents import orphan_salvage


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "develop")
    _git(repo, "config", "user.name", "Test Bot")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")


def _op_branch(repo: Path, ticket: str = "OP-9999", body: str = "work\n") -> str:
    branch = f"feature/{ticket}-runner-fresh"
    _git(repo, "switch", "-c", branch)
    (repo / f"{ticket}.txt").write_text(body)
    _git(repo, "add", f"{ticket}.txt")
    _git(repo, "commit", "-m", f"[{ticket}] salvaged work")
    _git(repo, "switch", "develop")
    return branch


def test_salvage_pushes_orphan_branch_and_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    branch = _op_branch(repo)
    (repo / "dirty.txt").write_text("uncommitted state survives\n")
    pushes: list[tuple[Path, str, str]] = []
    comments: list[tuple[str, str]] = []

    monkeypatch.setattr(orphan_salvage, "fetch_open_change_subjects", lambda agent_class: [])
    monkeypatch.setattr(
        orphan_salvage,
        "_push_branch_to_gerrit",
        lambda worktree, agent_class, pushed_branch: pushes.append(
            (worktree, agent_class, pushed_branch)
        ) or subprocess.CompletedProcess([], 0, stdout="https://gerrit/c/x/+/123", stderr=""),
    )
    monkeypatch.setattr(
        orphan_salvage.jira_dispatch,
        "make_client",
        lambda agent_class: object(),
    )
    monkeypatch.setattr(
        orphan_salvage.jira_dispatch,
        "add_comment",
        lambda client, key, text: comments.append((key, text)),
    )

    assert orphan_salvage.salvage_orphan_commits(repo, "subscription-codex") == 1

    assert pushes == [(repo, "subscription-codex", branch)]
    assert comments[0][0] == "OP-9999"
    assert "[runner-orphan-salvage]" in comments[0][1]
    assert "Branch=feature/OP-9999-runner-fresh" in comments[0][1]


def test_salvage_skips_when_open_gerrit_subject_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _op_branch(repo)

    monkeypatch.setattr(
        orphan_salvage,
        "fetch_open_change_subjects",
        lambda agent_class: ["[OP-9999] existing review"],
    )
    monkeypatch.setattr(
        orphan_salvage,
        "_push_branch_to_gerrit",
        lambda *args: pytest.fail("already-reviewed commit must not push"),
    )

    assert orphan_salvage.salvage_orphan_commits(repo, "subscription-codex") == 0


def test_salvage_skips_empty_diff_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    branch = "feature/OP-9999-runner-fresh"
    _git(repo, "switch", "-c", branch)
    _git(repo, "commit", "--allow-empty", "-m", "[OP-9999] empty")
    _git(repo, "switch", "develop")

    monkeypatch.setattr(orphan_salvage, "fetch_open_change_subjects", lambda agent_class: [])
    monkeypatch.setattr(
        orphan_salvage,
        "_push_branch_to_gerrit",
        lambda *args: pytest.fail("empty-diff commit must not push"),
    )

    assert orphan_salvage.salvage_orphan_commits(repo, "subscription-codex") == 0


def test_salvage_halts_and_alerts_when_too_many_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    for i in range(6):
        _git(repo, "switch", "-c", f"feature/OP-99{i}-runner-fresh", "develop")
        (repo / f"{i}.txt").write_text(f"{i}\n")
        _git(repo, "add", f"{i}.txt")
        _git(repo, "commit", "-m", f"[OP-99{i}] work")
    _git(repo, "switch", "develop")
    alerts = []

    monkeypatch.setattr(
        orphan_salvage,
        "fetch_open_change_subjects",
        lambda agent_class: pytest.fail("Gerrit query must not run after halt"),
    )
    monkeypatch.setattr(
        orphan_salvage.jira_dispatch,
        "notify_operator",
        lambda **kwargs: alerts.append(kwargs),
    )

    assert orphan_salvage.salvage_orphan_commits(repo, "subscription-codex") == 0
    assert alerts[0]["severity"] == "critical"
    assert "6 feature/OP-*-runner-fresh branches" in alerts[0]["detail"]


def test_salvage_alerts_and_skips_mixed_ticket_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    branch = "feature/OP-9999-runner-fresh"
    _git(repo, "switch", "-c", branch)
    (repo / "one.txt").write_text("one\n")
    _git(repo, "add", "one.txt")
    _git(repo, "commit", "-m", "[OP-8888] wrong ticket")
    (repo / "two.txt").write_text("two\n")
    _git(repo, "add", "two.txt")
    _git(repo, "commit", "-m", "[OP-9999] head ticket")
    _git(repo, "switch", "develop")
    alerts = []

    monkeypatch.setattr(orphan_salvage, "fetch_open_change_subjects", lambda agent_class: [])
    monkeypatch.setattr(
        orphan_salvage,
        "_push_branch_to_gerrit",
        lambda *args: pytest.fail("mixed-ticket branch must not push"),
    )
    monkeypatch.setattr(
        orphan_salvage.jira_dispatch,
        "notify_operator",
        lambda **kwargs: alerts.append(kwargs),
    )

    assert orphan_salvage.salvage_orphan_commits(repo, "subscription-codex") == 0
    assert "multiple tickets" in alerts[0]["detail"]
