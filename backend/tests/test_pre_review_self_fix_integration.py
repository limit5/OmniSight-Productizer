"""OP-1040 / AUDIT-29g-3 — pre-review mergeability self-fix end-to-end integration.

The self-fix loop (OP-1038) and its exhaustion escalation (OP-1039) have unit
coverage in ``test_pre_review_self_fix.py`` and ``test_jira_dispatch.py`` that
stubs ``run_command``. This file is the integration seam: it drives
``pre_review_self_fix.self_fix_mergeability`` against **real git** — a bare
"gerrit" remote plus a runner worktree — with only the Gerrit ``mergeable`` REST
read stubbed, so the actual ``git fetch`` / ``git rebase`` / ``git push --force``
commands run, and then chains the resulting ``SelfFixResult`` into
``jira_dispatch.file_pre_review_self_fix_exhaustion_ticket`` to confirm the
escalation ticket the runner would file.

Two scenarios, because they are mutually exclusive in the implementation:

* a *real file conflict* on the runner branch — a clean-rebase retry cannot fix
  it, so the loop aborts the rebase and bails after one attempt (it does **not**
  burn the 3-attempt cap on a hopeless retry), and
* ``develop`` diverged in a way this branch does **not** touch but Gerrit still
  reports ``mergeable=false`` (sibling conflict / eval lag) — the rebase keeps
  succeeding, mergeability stays false, the cap is exhausted at 3, and an
  operator escalation Story is filed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from backend.agents import jira_dispatch as jd
from backend.agents import pre_review_self_fix as sf


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check
    )


def _init_identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "runner@example.test")
    _git(repo, "config", "user.name", "Runner Bot")
    _git(repo, "config", "commit.gpgsign", "false")


def _make_remote_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A bare ``gerrit`` remote with a ``develop`` branch + a runner worktree on a feature commit."""

    remote = tmp_path / "gerrit.git"
    _git(tmp_path, "init", "--bare", "-b", "develop", str(remote))

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "develop")
    _init_identity(seed)
    (seed / "shared.txt").write_text("line-1\nline-2\nline-3\n", encoding="utf-8")
    (seed / "other.txt").write_text("untouched\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed develop")
    _git(seed, "push", str(remote), "develop")

    worktree = tmp_path / "worktree"
    _git(tmp_path, "clone", "-b", "develop", str(remote), str(worktree))
    _init_identity(worktree)
    _git(worktree, "checkout", "-b", "feature/OP-1040")
    return remote, worktree


def _stub_urlopen_always_unmergeable() -> Any:
    class _Response:
        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b")]}'\n{\"mergeable\": false}"

    def urlopen(_request: Any, **_kwargs: Any) -> "_Response":
        return _Response()

    return urlopen


def test_real_file_conflict_aborts_rebase_without_burning_the_cap(tmp_path: Path) -> None:
    """A genuine conflict on the runner branch -> rebase aborted, one attempt, tree clean."""

    remote, worktree = _make_remote_and_worktree(tmp_path)

    # Runner branch edits line-2; develop edits line-2 differently -> conflict.
    (worktree / "shared.txt").write_text("line-1\nFEATURE\nline-3\n", encoding="utf-8")
    _git(worktree, "commit", "-am", "[OP-1040] feature edit")
    feature_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    bystander = tmp_path / "bystander"
    _git(tmp_path, "clone", "-b", "develop", str(remote), str(bystander))
    _init_identity(bystander)
    (bystander / "shared.txt").write_text("line-1\nDEVELOP\nline-3\n", encoding="utf-8")
    _git(bystander, "commit", "-am", "concurrent develop edit")
    _git(bystander, "push", str(remote), "develop")

    result = sf.self_fix_mergeability(
        worktree_path=worktree,
        change_number=42,
        gerrit_ssh_url=str(remote),
        rest_base_url="https://gerrit.example.test",
        username="codex-bot",
        http_password="secret",
        run_command=subprocess.run,
        urlopen=_stub_urlopen_always_unmergeable(),
    )

    assert result.mergeable is False
    assert result.cap_exhausted is False
    assert result.attempts == 1
    assert "git rebase failed" in result.detail
    # Rebase was aborted: HEAD is still the feature commit and the tree is clean.
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == feature_head
    assert _git(worktree, "status", "--porcelain").stdout.strip() == ""
    assert not (worktree / ".git" / "rebase-merge").exists()
    assert not (worktree / ".git" / "rebase-apply").exists()


def test_persistent_unmergeable_exhausts_cap_then_files_escalation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """develop diverges on a file the branch doesn't touch but Gerrit stays unmergeable.

    The clean-rebase retry succeeds every time, mergeability never recovers, so
    the 3-attempt cap is exhausted -> the runner files the operator escalation
    Story with the source ticket, Gerrit change, Change-Id, attempt count, and
    bounded diff context.
    """

    remote, worktree = _make_remote_and_worktree(tmp_path)

    # Runner branch edits other.txt; develop will edit shared.txt -> no overlap,
    # so every `git rebase FETCH_HEAD` succeeds, but our stubbed Gerrit keeps
    # reporting mergeable=false (simulating a sibling conflict / eval lag).
    (worktree / "other.txt").write_text("feature change\n", encoding="utf-8")
    _git(worktree, "commit", "-am", "[OP-1040] feature edit")

    bystander = tmp_path / "bystander"
    _git(tmp_path, "clone", "-b", "develop", str(remote), str(bystander))
    _init_identity(bystander)
    (bystander / "shared.txt").write_text("line-1\nDEVELOP-MOVED\nline-3\n", encoding="utf-8")
    _git(bystander, "commit", "-am", "concurrent develop edit")
    _git(bystander, "push", str(remote), "develop")

    recorded: list[list[str]] = []

    def recording_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded.append(list(cmd))
        return subprocess.run(cmd, **kwargs)

    result = sf.self_fix_mergeability(
        worktree_path=worktree,
        change_number=42,
        gerrit_ssh_url=str(remote),
        rest_base_url="https://gerrit.example.test",
        username="codex-bot",
        http_password="secret",
        run_command=recording_run,
        urlopen=_stub_urlopen_always_unmergeable(),
    )

    assert result.mergeable is False
    assert result.cap_exhausted is True
    assert result.attempts == 3
    # Three real fetch + rebase + force-push cycles ran.
    assert sum(c[:2] == ["git", "fetch"] for c in recorded) == 3
    assert recorded.count(["git", "rebase", "FETCH_HEAD"]) == 3
    assert sum(
        c[:4] == ["git", "push", "--force", "--no-thin"] and c[-1] == "HEAD:refs/for/develop"
        for c in recorded
    ) == 3
    # The replacement patchset really landed on the remote magic ref.
    assert _git(remote, "rev-parse", "--verify", "refs/for/develop").returncode == 0

    # --- escalation: feed the exhausted result into the JIRA filing path -------
    posted: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(_client: Any, method: str, path: str, body: dict[str, Any] | None = None, **_: Any):
        posted.append((method, path, body))
        return {"key": "OP-9999"}

    monkeypatch.setattr(jd, "_request", fake_request)
    client = jd.DispatchClient(
        agent_class="subscription-codex",
        base_url="https://jira.example.test/rest/api/3",
        project_key="OP",
        auth_header="Basic token",
        bot_account_id="bot-account",
        bot_email="bot@example.test",
    )

    escalation_key = jd.file_pre_review_self_fix_exhaustion_ticket(
        client,
        source_ticket_key="OP-1040",
        change_number=42,
        change_url="https://gerrit.example.test/c/omnisight/OmniSight-Productizer/+/42",
        change_id="Iabc123def456",
        attempts=result.attempts,
        target="develop",
        detail=result.detail,
        diff_context="diff --git a/other.txt b/other.txt\n+feature change",
    )

    assert escalation_key == "OP-9999"
    assert len(posted) == 1
    method, path, body = posted[0]
    assert (method, path) == ("POST", "/issue")
    assert body is not None
    fields = body["fields"]
    assert fields["summary"] == "pre-review-self-fix-exhausted: OP-1040 Change 42"
    assert fields["labels"] == [
        "needs-coordinator",
        "pre-review-self-fix-exhausted",
        "class:operator",
    ]
    description = fields["description"]["content"][0]["content"][0]["text"]
    assert "@coordinator" in description
    assert "Source ticket: OP-1040" in description
    assert "Change-Id: Iabc123def456" in description
    assert "Self-fix attempts: 3" in description
    assert "diff --git a/other.txt b/other.txt" in description
