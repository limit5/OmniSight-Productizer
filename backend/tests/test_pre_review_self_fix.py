"""AUDIT-29g-1 pre-review mergeability self-fix tests."""
from __future__ import annotations

import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from backend.agents import pre_review_self_fix as sf


class _Response:
    def __init__(self, body: str) -> None:
        self.body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body.encode("utf-8")


def _ok(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_self_fix_rebases_and_force_pushes_until_mergeable(tmp_path: Path) -> None:
    """mergeable=false once -> fetch/rebase/force-push -> mergeable=true."""

    mergeable = iter(
        [
            ")]}'\n{\"mergeable\": false}",
            ")]}'\n{\"mergeable\": true}",
        ]
    )
    seen_requests: list[str] = []
    commands: list[list[str]] = []

    def urlopen(request: urllib.request.Request, **_: Any) -> _Response:
        seen_requests.append(request.full_url)
        return _Response(next(mergeable))

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return _ok(cmd, **kwargs)

    result = sf.self_fix_mergeability(
        worktree_path=tmp_path,
        change_number=42,
        gerrit_ssh_url="ssh://codex-bot@sora.services:29418/project",
        rest_base_url="https://sora.services:29420",
        username="codex-bot",
        http_password="secret",
        run_command=run,
        urlopen=urlopen,
    )

    assert result.mergeable is True
    assert result.attempts == 1
    assert result.rebased is True
    assert result.force_pushed is True
    assert seen_requests == [
        "https://sora.services:29420/a/changes/42/revisions/current/mergeable",
        "https://sora.services:29420/a/changes/42/revisions/current/mergeable",
    ]
    assert commands == [
        ["git", "fetch", "ssh://codex-bot@sora.services:29418/project", "develop"],
        ["git", "rebase", "FETCH_HEAD"],
        [
            "git",
            "push",
            "--force",
            "--no-thin",
            "ssh://codex-bot@sora.services:29418/project",
            "HEAD:refs/for/develop",
        ],
    ]


def test_self_fix_stops_after_three_attempts_when_still_unmergeable(
    tmp_path: Path,
) -> None:
    """Persistent mergeable=false is capped at three rebase+force-push tries."""

    commands: list[list[str]] = []

    def urlopen(request: urllib.request.Request, **_: Any) -> _Response:
        return _Response(")]}'\n{\"mergeable\": false}")

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return _ok(cmd, **kwargs)

    result = sf.self_fix_mergeability(
        worktree_path=tmp_path,
        change_number=42,
        gerrit_ssh_url="ssh://codex-bot@sora.services:29418/project",
        rest_base_url="https://sora.services:29420",
        username="codex-bot",
        http_password="secret",
        run_command=run,
        urlopen=urlopen,
    )

    assert result.mergeable is False
    assert result.cap_exhausted is True
    assert result.attempts == 3
    assert commands.count(
        ["git", "fetch", "ssh://codex-bot@sora.services:29418/project", "develop"]
    ) == 3
    assert commands.count(["git", "rebase", "FETCH_HEAD"]) == 3
    assert commands.count(
        [
            "git",
            "push",
            "--force",
            "--no-thin",
            "ssh://codex-bot@sora.services:29418/project",
            "HEAD:refs/for/develop",
        ]
    ) == 3
