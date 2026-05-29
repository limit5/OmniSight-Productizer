"""OP-1845 (P2.4.1) — GitHub PR contribution target mechanics.

All external effects are mocked: ``git`` subprocess calls are recorded and the
GitHub REST API is represented by a tiny in-memory ``httpx.Client`` stand-in.
The live camviewpro proof remains deferred to P2.4.2.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import backend
from backend.agents import github_pr_target as gpt


TOKEN = "ghp_test_token"
REPO_URL = "https://github.com/limit5/camviewpro-android.git"


def _account(*, token: str = TOKEN) -> dict:
    return {
        "id": "camviewpro-pr",
        "platform": "github",
        "repo_url": REPO_URL,
        "token": token,
    }


def _fake_pick_by_id(monkeypatch, row):
    async def _fake(account_id, *, tenant_id=None, touch=True):
        _fake.seen = (account_id, tenant_id)
        return row

    _fake.seen = None
    fake_module = SimpleNamespace(pick_by_id=_fake)
    monkeypatch.setitem(
        sys.modules,
        "backend.git_credentials",
        fake_module,
    )
    monkeypatch.setattr(backend, "git_credentials", fake_module, raising=False)
    return _fake


class GitRecorder:
    def __init__(self, changed_files: list[str] | None = None):
        self.changed_files = changed_files or ["app/src/MainActivity.kt"]
        self.calls: list[list[str]] = []
        self.remote_url = ""

    def __call__(self, argv, *, cwd, text, capture_output, check):
        self.calls.append(list(argv))
        if argv[:3] == ["git", "diff", "--name-only"]:
            return SimpleNamespace(
                returncode=0,
                stdout="\n".join(self.changed_files) + "\n",
                stderr="",
            )
        if argv[:3] == ["git", "remote", "set-url"]:
            self.remote_url = argv[-1]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:3] == ["git", "push", "origin"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git argv: {argv!r}")

    @property
    def push_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[:3] == ["git", "push", "origin"]]

    @property
    def set_url_calls(self) -> list[list[str]]:
        return [
            call for call in self.calls
            if call[:3] == ["git", "remote", "set-url"]
        ]


class FakeGithubClient:
    def __init__(self, state: dict):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, *, headers, params):
        self.state["calls"].append(("GET", url, params, None))
        return httpx.Response(200, json=self.state.get("existing", []))

    def post(self, url, *, headers, json):
        self.state["calls"].append(("POST", url, None, json))
        self.state["posted"] = True
        return httpx.Response(
            201,
            json={
                "html_url": "https://github.com/limit5/camviewpro-android/pull/42",
                "number": 42,
            },
        )

    def patch(self, url, *, headers, json):
        self.state["calls"].append(("PATCH", url, None, json))
        if "/issues/" in url:
            self.state["label_patch"] = json
            return httpx.Response(200, json={"labels": json["labels"]})
        number = int(url.rsplit("/", 1)[1])
        self.state["patched_pr"] = True
        return httpx.Response(
            200,
            json={
                "html_url": (
                    "https://github.com/limit5/camviewpro-android/pull/"
                    f"{number}"
                ),
                "number": number,
            },
        )


def _fake_http(monkeypatch, *, existing: list[dict] | None = None) -> dict:
    state = {"calls": [], "existing": existing or []}

    def _client_factory(*, timeout):
        state["timeout"] = timeout
        return FakeGithubClient(state)

    monkeypatch.setattr(gpt.httpx, "Client", _client_factory)
    return state


def _install_fakes(
    monkeypatch,
    *,
    account: dict | None = None,
    changed_files: list[str] | None = None,
    existing: list[dict] | None = None,
):
    _fake_pick_by_id(monkeypatch, account if account is not None else _account())
    git = GitRecorder(changed_files)
    monkeypatch.setattr(gpt.subprocess, "run", git)
    http = _fake_http(monkeypatch, existing=existing)
    return git, http


def _open(tmp_path: Path, **kwargs) -> gpt.PrResult:
    data = {
        "worktree": tmp_path,
        "branch": "feature/OP-1845-pr-target",
        "base": "main",
        "title": "OP-1845 write-back",
        "body": "mocked proof",
        "git_account_ref": "camviewpro-pr",
        "tenant_id": "t-op",
    }
    data.update(kwargs)
    return gpt.open_contribution_pr(**data)


def test_head_equal_base_raises(tmp_path):
    with pytest.raises(gpt.GithubPrError):
        _open(tmp_path, branch="main", base="main")


def test_protected_head_main_raises(tmp_path):
    with pytest.raises(gpt.GithubPrError):
        _open(tmp_path, branch="main", base="develop")


def test_feature_branch_pushes_and_posts_ready_pr(monkeypatch, tmp_path):
    git, http = _install_fakes(monkeypatch)

    result = _open(tmp_path)

    assert result == gpt.PrResult(
        pr_url="https://github.com/limit5/camviewpro-android/pull/42",
        number=42,
        branch="feature/OP-1845-pr-target",
        flagged_medical=False,
    )
    assert git.push_calls == [
        [
            "git",
            "push",
            "origin",
            "feature/OP-1845-pr-target:feature/OP-1845-pr-target",
        ]
    ]
    post = [call for call in http["calls"] if call[0] == "POST"][0]
    assert post[1] == "https://api.github.com/repos/limit5/camviewpro-android/pulls"
    assert post[3]["base"] == "main"
    assert post[3]["head"] == "feature/OP-1845-pr-target"
    assert post[3]["draft"] is False


def test_force_push_is_never_issued(monkeypatch, tmp_path):
    git, _ = _install_fakes(monkeypatch)

    _open(tmp_path)

    push = git.push_calls[0]
    assert "--force" not in push
    assert all(not arg.startswith("+") for arg in push)


def test_token_is_scrubbed_from_remote_url_after_push(monkeypatch, tmp_path):
    git, _ = _install_fakes(monkeypatch)

    _open(tmp_path)

    assert TOKEN in git.set_url_calls[0][-1]
    assert git.remote_url == REPO_URL
    assert TOKEN not in git.remote_url


def test_medical_change_sets_flag_and_regulated_label(monkeypatch, tmp_path):
    _, http = _install_fakes(
        monkeypatch,
        changed_files=["apps/medical/src/DeviceMode.kt"],
    )

    result = _open(tmp_path)

    assert result.flagged_medical is True
    label_calls = [
        call for call in http["calls"]
        if call[0] == "PATCH" and call[1].endswith("/issues/42/labels")
    ]
    assert label_calls
    assert label_calls[0][3] == {"labels": ["regulated-lane"]}


def test_non_medical_change_skips_regulated_label(monkeypatch, tmp_path):
    _, http = _install_fakes(
        monkeypatch,
        changed_files=["apps/consumer/src/MainActivity.kt"],
    )

    result = _open(tmp_path)

    assert result.flagged_medical is False
    assert not [
        call for call in http["calls"]
        if call[0] == "PATCH" and "/issues/" in call[1]
    ]


def test_existing_pr_for_head_updates_without_duplicate_create(monkeypatch, tmp_path):
    _, http = _install_fakes(
        monkeypatch,
        existing=[{
            "number": 77,
            "html_url": "https://github.com/limit5/camviewpro-android/pull/77",
        }],
    )

    result = _open(tmp_path, title="updated", body="updated body")

    assert result.number == 77
    assert not [call for call in http["calls"] if call[0] == "POST"]
    patch = [call for call in http["calls"] if call[0] == "PATCH"][0]
    assert patch[1].endswith("/pulls/77")
    assert patch[3]["title"] == "updated"
    assert patch[3]["body"] == "updated body"


def test_missing_token_raises(monkeypatch, tmp_path):
    _install_fakes(monkeypatch, account=_account(token=""))

    with pytest.raises(gpt.GithubPrError):
        _open(tmp_path)
