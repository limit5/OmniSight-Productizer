"""OP-2105 (vmnda B) — GitLab MR contribution target mechanics.

All external effects are mocked: ``git`` subprocess calls are recorded and the
GitLab REST API is a tiny in-memory ``httpx.Client`` stand-in. Mirrors
test_github_pr_target's style. Live MR proof is deferred.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import backend
from backend.agents import gitlab_mr_target as gmt


TOKEN = "glpat_test_token"
INTERNAL_HOST = "sora.services:49156"
REPO_URL = f"https://{INTERNAL_HOST}/vendor-mirrors-nda/catalog.git"


@pytest.fixture(autouse=True)
def _internal_env(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_GITLAB_URL", f"https://{INTERNAL_HOST}")
    monkeypatch.setenv("OMNISIGHT_GITLAB_TLS_VERIFY", "0")


def _account(*, token: str = TOKEN) -> dict:
    return {"id": "vmnda-catalog", "platform": "gitlab", "repo_url": REPO_URL, "token": token}


def _fake_pick_by_id(monkeypatch, row):
    async def _fake(account_id, *, tenant_id=None, touch=True):
        _fake.seen = (account_id, tenant_id)
        return row

    _fake.seen = None
    fake_module = SimpleNamespace(pick_by_id=_fake)
    monkeypatch.setitem(sys.modules, "backend.git_credentials", fake_module)
    monkeypatch.setattr(backend, "git_credentials", fake_module, raising=False)
    return _fake


class GitRecorder:
    def __init__(self, *, worktree_origin_url: str = REPO_URL):
        self.worktree_origin_url = worktree_origin_url
        self.calls: list[list[str]] = []
        self.remote_url = ""

    def __call__(self, argv, *, cwd, text, capture_output, check):
        self.calls.append(list(argv))
        if argv[:4] == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=0, stdout=self.worktree_origin_url + "\n", stderr="")
        if argv[:3] == ["git", "remote", "set-url"]:
            self.remote_url = argv[-1]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:3] == ["git", "push", "origin"]:
            # token must be present in origin at push time
            assert self.remote_url.startswith("https://oauth2:"), self.remote_url
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git argv: {argv!r}")

    @property
    def set_url_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["git", "remote", "set-url"]]


class FakeGitlabClient:
    def __init__(self, state: dict):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, *, headers, params):
        self.state["calls"].append(("GET", url, params, None))
        self.state["get_headers"] = headers
        return httpx.Response(200, json=self.state.get("existing", []))

    def post(self, url, *, headers, json):
        self.state["calls"].append(("POST", url, None, json))
        self.state["posted"] = True
        return httpx.Response(201, json={"web_url": f"https://{INTERNAL_HOST}/vendor-mirrors-nda/catalog/-/merge_requests/7", "iid": 7})

    def put(self, url, *, headers, json):
        self.state["calls"].append(("PUT", url, None, json))
        self.state["put"] = True
        iid = int(url.rsplit("/", 1)[1])
        return httpx.Response(200, json={"web_url": f"https://{INTERNAL_HOST}/x/-/merge_requests/{iid}", "iid": iid})


def _fake_http(monkeypatch, *, existing=None) -> dict:
    state = {"calls": [], "existing": existing or []}

    def _factory(*, timeout, verify):
        state["timeout"] = timeout
        state["verify"] = verify
        return FakeGitlabClient(state)

    monkeypatch.setattr(gmt.httpx, "Client", _factory)
    return state


def _install(monkeypatch, *, account=None, existing=None, origin=REPO_URL):
    _fake_pick_by_id(monkeypatch, account if account is not None else _account())
    rec = GitRecorder(worktree_origin_url=origin)
    monkeypatch.setattr(gmt.subprocess, "run", rec)
    state = _fake_http(monkeypatch, existing=existing)
    return rec, state


def test_opens_mr_when_none_exists(monkeypatch, tmp_path):
    rec, state = _install(monkeypatch)
    res = gmt.open_contribution_mr(
        worktree=tmp_path, branch="feature/seed-qualcomm", base="main",
        title="Seed qualcomm", description="rows", git_account_ref="vmnda-catalog",
    )
    assert res.iid == 7 and res.branch == "feature/seed-qualcomm"
    assert state["posted"] is True
    assert any(m == "POST" for m, *_ in state["calls"])
    # PRIVATE-TOKEN header used (PAT), and the MR POST carries the right branches.
    post = [c for c in state["calls"] if c[0] == "POST"][0]
    assert post[3]["source_branch"] == "feature/seed-qualcomm"
    assert post[3]["target_branch"] == "main"


def test_updates_mr_when_one_exists(monkeypatch, tmp_path):
    rec, state = _install(monkeypatch, existing=[{"iid": 11}])
    res = gmt.open_contribution_mr(
        worktree=tmp_path, branch="feature/x", base="main",
        title="t", description="d", git_account_ref="vmnda-catalog",
    )
    assert res.iid == 11
    assert state.get("put") is True
    assert not state.get("posted")


def test_origin_scrubbed_after_push(monkeypatch, tmp_path):
    rec, state = _install(monkeypatch)
    gmt.open_contribution_mr(
        worktree=tmp_path, branch="feature/x", base="main",
        title="t", description="d", git_account_ref="vmnda-catalog",
    )
    # final origin set-url must be the clean (token-free) URL
    assert rec.remote_url == f"https://{INTERNAL_HOST}/vendor-mirrors-nda/catalog.git"
    assert TOKEN not in rec.remote_url
    # token never appears in the recorded GitLab API call payloads either
    assert TOKEN not in " ".join(str(c) for c in state["calls"])


@pytest.mark.parametrize("branch", ["main", "release/1", "vendor/meta-qcom", "+feature/x", "random"])
def test_rejects_bad_branch(monkeypatch, tmp_path, branch):
    _install(monkeypatch)
    with pytest.raises(gmt.GitlabMrError):
        gmt.open_contribution_mr(
            worktree=tmp_path, branch=branch, base="main",
            title="t", description="d", git_account_ref="vmnda-catalog",
        )


@pytest.mark.parametrize("origin", [
    "https://github.com/limit5/something.git",
    "https://gitlab.com/x/y.git",
    "https://evil.example/vendor-mirrors-nda/catalog.git",
])
def test_leak_guard_rejects_non_internal_host(monkeypatch, tmp_path, origin):
    _install(monkeypatch, origin=origin)
    with pytest.raises(gmt.GitlabMrError, match="leak guard"):
        gmt.open_contribution_mr(
            worktree=tmp_path, branch="feature/x", base="main",
            title="t", description="d", git_account_ref="vmnda-catalog",
        )


def test_missing_token_raises(monkeypatch, tmp_path):
    _install(monkeypatch, account=_account(token=""))
    with pytest.raises(gmt.GitlabMrError, match="no token"):
        gmt.open_contribution_mr(
            worktree=tmp_path, branch="feature/x", base="main",
            title="t", description="d", git_account_ref="vmnda-catalog",
        )
