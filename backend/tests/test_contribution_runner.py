"""OP-1846 (P2.4.3a) — camviewpro contribution orchestrator."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import backend
from backend.agents import contribution_runner as cr
from backend.agents import github_pr_target as gpt
from backend.agents import product_source as ps


TOKEN = "ghp_secret_token"
REPO_URL = "https://github.com/limit5/camviewpro-android.git"


def _source() -> ps.ProductSource:
    return ps.ProductSource(
        repo_url=REPO_URL,
        tier="consumer",
        branch="main",
        pinned_ref="c360a9d0",
        git_account_ref="camviewpro-ro",
    )


def _install_credential(monkeypatch):
    async def _fake(account_id, *, tenant_id=None, touch=True):
        _fake.seen = (account_id, tenant_id)
        return {
            "id": account_id,
            "username": "ci",
            "repo_url": REPO_URL,
            "token": TOKEN,
        }

    _fake.seen = None
    fake_module = SimpleNamespace(pick_by_id=_fake)
    monkeypatch.setitem(sys.modules, "backend.git_credentials", fake_module)
    monkeypatch.setattr(backend, "git_credentials", fake_module, raising=False)
    return _fake


class GitRecorder:
    def __init__(self, *, changed: bool):
        self.changed = changed
        self.calls: list[tuple[list[str], Path | None]] = []
        self.remote_url = ""

    def __call__(self, cmd, *, cwd=None, text, capture_output, check):
        argv = list(cmd)
        cwd_path = Path(cwd) if cwd is not None else None
        self.calls.append((argv, cwd_path))
        if argv[:2] == ["git", "clone"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
        if argv[:4] == ["git", "remote", "set-url", "origin"]:
            self.remote_url = argv[4]
        if argv[:3] == ["git", "status", "--porcelain"]:
            stdout = " M app/src/main.kt\n" if self.changed else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if argv[:3] == ["git", "config", "--get"]:
            value = "codex-bot\n" if argv[-1] == "user.name" else "codex@example.test\n"
            return SimpleNamespace(returncode=0, stdout=value, stderr="")
        if argv[:4] == ["git", "config", "--global", "--get"]:
            value = "global-bot\n" if argv[-1] == "user.name" else "global@example.test\n"
            return SimpleNamespace(returncode=0, stdout=value, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def commands(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]

    def command_cwds(self) -> list[tuple[list[str], Path | None]]:
        return self.calls


def _install(
    monkeypatch,
    *,
    changed: bool = True,
    pr: gpt.PrResult | None = None,
):
    monkeypatch.setattr(cr, "resolve_product_source", lambda key: _source())
    pick = _install_credential(monkeypatch)
    git = GitRecorder(changed=changed)
    monkeypatch.setattr(cr.subprocess, "run", git)
    calls = []
    pr_result = pr or gpt.PrResult(
        pr_url="https://github.com/limit5/camviewpro-android/pull/42",
        number=42,
        branch="feature/OP-1846-add-rtsp-auth",
        flagged_medical=False,
    )

    def _open(**kwargs):
        calls.append(kwargs)
        return pr_result

    monkeypatch.setattr(cr, "open_contribution_pr", _open)
    return git, calls, pick, pr_result


def _run_contribution(tmp_path: Path, implement, **kwargs) -> cr.ContributionResult:
    data = {
        "project_key": "OP",
        "ticket_key": "OP-1846",
        "base": "main",
        "slug": "Add RTSP Auth",
        "implement": implement,
        "git_account_ref": "camviewpro-pr",
        "workspace_root": tmp_path,
        "tenant_id": "t-op",
    }
    data.update(kwargs)
    return asyncio.run(cr.contribute_to_product(**data))


def test_sanitized_feature_branch_passed_to_open_pr(monkeypatch, tmp_path):
    _git, calls, _pick, _pr = _install(monkeypatch)

    result = _run_contribution(tmp_path, lambda worktree: None)

    assert result.branch == "feature/OP-1846-add-rtsp-auth"
    assert calls[0]["branch"] == "feature/OP-1846-add-rtsp-auth"


def test_async_implement_seam_is_awaited_with_worktree(monkeypatch, tmp_path):
    _git, _calls, _pick, _pr = _install(monkeypatch)
    seen = {"called": False}

    async def implement(worktree):
        seen["called"] = True
        seen["worktree"] = worktree

    _run_contribution(tmp_path, implement)

    assert seen == {
        "called": True,
        "worktree": tmp_path / "camviewpro-android",
    }


def test_no_changes_returns_without_opening_pr(monkeypatch, tmp_path):
    _git, calls, _pick, _pr = _install(monkeypatch, changed=False)

    result = _run_contribution(tmp_path, lambda worktree: None)

    assert result.no_changes is True
    assert result.pr is None
    assert calls == []


def test_changes_commit_and_open_pr_with_base_and_branch(monkeypatch, tmp_path):
    git, calls, _pick, pr = _install(monkeypatch)

    result = _run_contribution(tmp_path, lambda worktree: None)

    commit = next(cmd for cmd in git.commands() if cmd[:2] == ["git", "commit"])
    assert "OP-1846" in commit[3]
    assert result.pr is pr
    assert result.no_changes is False
    assert calls[0]["base"] == "main"
    assert calls[0]["branch"] == "feature/OP-1846-add-rtsp-auth"


def test_unconfigured_project_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "resolve_product_source", lambda key: None)

    with pytest.raises(cr.ContributionError, match="project not configured"):
        _run_contribution(tmp_path, lambda worktree: None)


def test_flagged_medical_from_pr_is_surfaced(monkeypatch, tmp_path):
    pr = gpt.PrResult(
        pr_url="https://github.com/limit5/camviewpro-android/pull/42",
        number=42,
        branch="feature/OP-1846-add-rtsp-auth",
        flagged_medical=True,
    )
    _git, _calls, _pick, _pr = _install(monkeypatch, pr=pr)

    result = _run_contribution(tmp_path, lambda worktree: None)

    assert result.pr is pr
    assert result.pr.flagged_medical is True


def test_clone_token_is_scrubbed_from_recorded_remote_url(monkeypatch, tmp_path):
    git, _calls, _pick, _pr = _install(monkeypatch)

    _run_contribution(tmp_path, lambda worktree: None)

    clone = next(cmd for cmd in git.commands() if cmd[:2] == ["git", "clone"])
    assert TOKEN in clone[2]
    assert git.remote_url == REPO_URL
    assert TOKEN not in git.remote_url


def test_token_scrub_happens_after_the_authenticated_base_fetch(monkeypatch, tmp_path):
    # Regression guard (OP-1846): the base fetch needs the tokenised origin, so the
    # de-tokenising `remote set-url` MUST come after `git fetch origin <base>`.
    # Scrubbing first breaks fetch on a private repo ("could not read Username").
    git, _calls, _pick, _pr = _install(monkeypatch)

    _run_contribution(tmp_path, lambda worktree: None)

    cmds = git.commands()
    fetch_idx = next(i for i, c in enumerate(cmds) if c[:3] == ["git", "fetch", "origin"])
    scrub_idx = next(i for i, c in enumerate(cmds) if c[:4] == ["git", "remote", "set-url", "origin"])
    assert fetch_idx < scrub_idx, "token must be scrubbed only AFTER the authenticated base fetch"
