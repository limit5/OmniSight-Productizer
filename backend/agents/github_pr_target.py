"""GitHub PR contribution target mechanics (OP-1845 / P2.4.1).

This module is the B2 write-back finish path for GitHub-reviewed external
repos such as ``limit5/camviewpro-android``. It intentionally stops at the
mechanics: resolve the configured ``git_accounts`` row, enforce branch/push
guardrails, push a feature branch, and create or update a ready-for-review
GitHub PR. Runner routing and live operator proof are follow-on tickets.

Credentials are resolved through :mod:`backend.git_credentials`; this module
does not introduce a secret store and never logs tokens. The token is only
placed in the local ``origin`` URL for the push window and is scrubbed with a
plain, token-free URL immediately afterwards.
"""
from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import httpx

MEDICAL_PATH_PREFIXES = (
    "apps/medical/",
    "libs/core-medical-grade/",
    "docs/regulatory/",
)


class GithubPrError(RuntimeError):
    """The GitHub PR contribution target could not be completed safely."""


@dataclass(frozen=True)
class PrResult:
    pr_url: str
    number: int
    branch: str
    flagged_medical: bool


def open_contribution_pr(
    *,
    worktree: Path,
    branch: str,
    base: str,
    title: str,
    body: str,
    git_account_ref: str,
    tenant_id: Optional[str] = None,
) -> PrResult:
    """Push *branch* and open/update a GitHub PR against *base*.

    All side effects are deliberately narrow and mockable: ``git`` is invoked
    through ``subprocess.run`` and GitHub is called through ``httpx.Client``.
    The branch must be an unprotected feature branch; force pushes and
    protected branch writes are rejected before any network write.
    """
    clean_branch = branch.strip()
    clean_base = base.strip()
    _validate_branch(clean_branch, clean_base)

    row = _resolve_git_account(git_account_ref, tenant_id=tenant_id)
    token = str(row.get("token") or "").strip()
    if not token:
        raise GithubPrError(
            f"git_accounts row {git_account_ref!r} has no token for GitHub "
            "PR write-back"
        )

    repo_url = _repo_url_from_row(row)
    repo = _parse_repo_url(repo_url)
    clean_url = repo.clean_url
    token_url = repo.token_url(token)

    changed_files = _changed_files(worktree, clean_base, clean_branch)
    flagged_medical = _touches_medical_lane(changed_files)

    _set_origin_url(worktree, token_url)
    try:
        _push_branch(worktree, clean_branch)
    finally:
        _set_origin_url(worktree, clean_url)

    pr = _open_or_update_pr(
        repo=repo,
        token=token,
        branch=clean_branch,
        base=clean_base,
        title=title,
        body=body,
    )
    if flagged_medical:
        _add_regulated_lane_label(repo=repo, token=token, number=pr["number"])

    return PrResult(
        pr_url=pr["html_url"],
        number=int(pr["number"]),
        branch=clean_branch,
        flagged_medical=flagged_medical,
    )


@dataclass(frozen=True)
class _RepoRef:
    host: str
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def clean_url(self) -> str:
        return f"https://{self.host}/{self.slug}.git"

    @property
    def api_base(self) -> str:
        if self.host.lower() == "github.com":
            return "https://api.github.com"
        return f"https://{self.host}/api/v3"

    def token_url(self, token: str) -> str:
        return (
            f"https://x-access-token:{quote(token, safe='')}@"
            f"{self.host}/{self.slug}.git"
        )


def _validate_branch(branch: str, base: str) -> None:
    if not branch or not base:
        raise GithubPrError("branch and base are required")
    if branch == base:
        raise GithubPrError("head branch must not equal base branch")
    if branch.startswith("+"):
        raise GithubPrError("force-push refspecs are not allowed")
    if branch in {"main", "master"} or branch.startswith("release/"):
        raise GithubPrError(f"protected branch {branch!r} cannot be used as PR head")
    if not (
        branch.startswith("feature/")
        or branch.startswith("feat/")
        or branch.startswith("fix/")
        or branch.startswith("bugfix/")
        or branch.startswith("hotfix/")
        or branch.startswith("chore/")
    ):
        raise GithubPrError(
            f"head branch {branch!r} is not an allowed feature branch"
        )


def _resolve_git_account(account_id: str, *, tenant_id: Optional[str]) -> dict:
    if not account_id:
        raise GithubPrError("git_account_ref is required")

    async def _pick() -> dict | None:
        from backend import git_credentials

        return await git_credentials.pick_by_id(account_id, tenant_id=tenant_id)

    try:
        row = asyncio.run(_pick())
    except RuntimeError as exc:
        raise GithubPrError(
            "open_contribution_pr must be called from synchronous runner code"
        ) from exc
    if row is None:
        raise GithubPrError(f"git_accounts row {account_id!r} not found")
    return row


def _repo_url_from_row(row: dict) -> str:
    repo_url = str(
        row.get("repo_url")
        or row.get("url")
        or row.get("instance_url")
        or ""
    ).strip()
    if not repo_url:
        raise GithubPrError("git_accounts row has no repo URL")
    return repo_url


def _parse_repo_url(repo_url: str) -> _RepoRef:
    url = repo_url.strip()
    if url.startswith("git@") and ":" in url:
        host_part, path_part = url.split(":", 1)
        host = host_part.split("@", 1)[1]
        path = path_part
    else:
        parsed = urlparse(url)
        if parsed.scheme == "ssh" and "@" in parsed.netloc:
            host = parsed.netloc.rsplit("@", 1)[1]
        else:
            host = parsed.hostname or ""
        path = parsed.path.lstrip("/")

    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if not host or len(parts) < 2:
        raise GithubPrError(f"cannot derive GitHub owner/repo from {repo_url!r}")
    return _RepoRef(host=host, owner=parts[-2], name=parts[-1])


def _changed_files(worktree: Path, base: str, branch: str) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{branch}"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GithubPrError(
            f"could not list changed files for {base}...{branch}: "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _touches_medical_lane(changed_files: list[str]) -> bool:
    return any(
        path.startswith(prefix)
        for path in changed_files
        for prefix in MEDICAL_PATH_PREFIXES
    )


def _set_origin_url(worktree: Path, url: str) -> None:
    _run_git(worktree, ["git", "remote", "set-url", "origin", url])


def _push_branch(worktree: Path, branch: str) -> None:
    refspec = f"{branch}:{branch}"
    argv = ["git", "push", "origin", refspec]
    if "--force" in argv or any(arg.startswith("+") for arg in argv):
        raise GithubPrError("force-push argv rejected")
    _run_git(worktree, argv)


def _run_git(worktree: Path, argv: list[str]) -> None:
    proc = subprocess.run(
        argv,
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GithubPrError(
            f"{argv[:2]} failed: {(proc.stderr or proc.stdout).strip()}"
        )


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _open_or_update_pr(
    *,
    repo: _RepoRef,
    token: str,
    branch: str,
    base: str,
    title: str,
    body: str,
) -> dict:
    pulls_url = f"{repo.api_base}/repos/{repo.slug}/pulls"
    headers = _github_headers(token)
    payload = {
        "base": base,
        "head": branch,
        "title": title,
        "body": body,
        "draft": False,
    }

    with httpx.Client(timeout=30.0) as client:
        existing = client.get(
            pulls_url,
            headers=headers,
            params={
                "state": "open",
                "head": f"{repo.owner}:{branch}",
                "base": base,
            },
        )
        _raise_for_github(existing, "list pull requests")
        matches = existing.json()
        if matches:
            number = int(matches[0]["number"])
            response = client.patch(
                f"{pulls_url}/{number}",
                headers=headers,
                json={
                    "base": base,
                    "title": title,
                    "body": body,
                },
            )
        else:
            response = client.post(pulls_url, headers=headers, json=payload)
        _raise_for_github(response, "write pull request")
        return response.json()


def _add_regulated_lane_label(*, repo: _RepoRef, token: str, number: int) -> None:
    with httpx.Client(timeout=30.0) as client:
        response = client.patch(
            f"{repo.api_base}/repos/{repo.slug}/issues/{number}/labels",
            headers=_github_headers(token),
            json={"labels": ["regulated-lane"]},
        )
    _raise_for_github(response, "label regulated-lane")


def _raise_for_github(response: httpx.Response, action: str) -> None:
    if response.status_code < 400:
        return
    detail = response.text.strip()
    raise GithubPrError(
        f"GitHub {action} failed: HTTP {response.status_code} {detail}"
    )
