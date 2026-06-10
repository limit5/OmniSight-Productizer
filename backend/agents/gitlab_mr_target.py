"""GitLab MR contribution target mechanics (OP-2105 / vmnda B).

Peer of :mod:`backend.agents.github_pr_target` for the isolated
``vendor-mirrors-nda`` GitLab group. The runner uses this to open Merge
Requests against the mirror ``catalog`` and the per-vendor BSP repos — the path
that did not exist when OP-491 was filed (the runner had only ``github_pr_target``
and Gerrit push, so the catalog-repo leaves C1/C2/C3 could not be runner-built).

Like the GitHub target, this module stops at the mechanics: resolve the
configured ``git_accounts`` row, enforce branch/push guardrails, push a feature
branch, and create or update a ready-for-review MR. Credentials resolve through
:mod:`backend.git_credentials`; the token is only placed in the local ``origin``
URL for the push window and scrubbed immediately afterwards, never logged.

NDA-leak guard (ADR-0043 §6): the module REFUSES to operate when the target host
is not the configured internal GitLab. NDA vendor content must never reach a
public host (github.com / gitlab.com) or the GitLab->GitHub one-way mirror.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import httpx

# Public hosts NDA content must never be pushed to. The positive allowlist is
# the configured internal GitLab host (see _internal_host); this set is a
# belt-and-braces explicit denylist for the most likely misroute targets.
_PUBLIC_HOST_DENYLIST = frozenset({"github.com", "www.github.com", "gitlab.com", "www.gitlab.com"})

# Allowed feature-branch prefixes (mirror github_pr_target).
_ALLOWED_BRANCH_PREFIXES = ("feature/", "feat/", "fix/", "bugfix/", "hotfix/", "chore/")


class GitlabMrError(RuntimeError):
    """The GitLab MR contribution target could not be completed safely."""


@dataclass(frozen=True)
class MrResult:
    mr_url: str
    iid: int
    branch: str


@dataclass(frozen=True)
class _GitlabRef:
    host: str
    project_path: str  # namespace(/subgroup)*/name, no trailing .git

    @property
    def clean_url(self) -> str:
        return f"https://{self.host}/{self.project_path}.git"

    @property
    def api_base(self) -> str:
        return f"https://{self.host}/api/v4"

    @property
    def encoded_path(self) -> str:
        # GitLab accepts a URL-encoded "namespace/project" as the :id segment.
        return quote(self.project_path, safe="")

    def token_url(self, token: str) -> str:
        return f"https://oauth2:{quote(token, safe='')}@{self.host}/{self.project_path}.git"


def _internal_host() -> str:
    """The one GitLab host NDA MRs may target.

    Derived from ``OMNISIGHT_GITLAB_URL`` (host[:port]); defaults to the
    self-hosted instance. Kept as a function so tests + deployments can point it
    via env without import-time capture.
    """
    raw = os.environ.get("OMNISIGHT_GITLAB_URL", "https://sora.services:49156")
    netloc = urlparse(raw).netloc or raw
    return netloc.strip()


def _verify_tls() -> bool:
    # The internal GitLab sits behind a self-signed Synology TLS proxy. Default
    # to not verifying for that host; override with OMNISIGHT_GITLAB_TLS_VERIFY=1.
    return os.environ.get("OMNISIGHT_GITLAB_TLS_VERIFY", "0") == "1"


def open_contribution_mr(
    *,
    worktree: Path,
    branch: str,
    base: str,
    title: str,
    description: str,
    git_account_ref: str,
    tenant_id: Optional[str] = None,
) -> MrResult:
    """Push *branch* and open/update a GitLab MR against *base*.

    Routing repo is the worktree's ``origin`` (set by the caller to the target
    repo URL), mirroring OP-1862. The branch must be an unprotected feature
    branch. Raises :class:`GitlabMrError` (including the NDA-leak host guard)
    before any network write when a precondition fails.
    """
    clean_branch = branch.strip()
    clean_base = base.strip()
    _validate_branch(clean_branch, clean_base)

    repo = _parse_project_url(_read_worktree_origin(worktree))
    _enforce_internal_host(repo.host)

    row = _resolve_git_account(git_account_ref, tenant_id=tenant_id)
    token = str(row.get("token") or "").strip()
    if not token:
        raise GitlabMrError(
            f"git_accounts row {git_account_ref!r} has no token for GitLab MR write-back"
        )

    _set_origin_url(worktree, repo.token_url(token))
    try:
        _push_branch(worktree, clean_branch)
    finally:
        _set_origin_url(worktree, repo.clean_url)

    mr = _open_or_update_mr(
        repo=repo,
        token=token,
        branch=clean_branch,
        base=clean_base,
        title=title,
        description=description,
    )
    return MrResult(
        mr_url=str(mr["web_url"]),
        iid=int(mr["iid"]),
        branch=clean_branch,
    )


def _validate_branch(branch: str, base: str) -> None:
    if not branch or not base:
        raise GitlabMrError("branch and base are required")
    if branch == base:
        raise GitlabMrError("source branch must not equal target branch")
    if branch.startswith("+"):
        raise GitlabMrError("force-push refspecs are not allowed")
    if branch in {"main", "master"} or branch.startswith(("release/", "vendor/")):
        raise GitlabMrError(f"protected branch {branch!r} cannot be used as MR source")
    if not branch.startswith(_ALLOWED_BRANCH_PREFIXES):
        raise GitlabMrError(f"source branch {branch!r} is not an allowed feature branch")


def _enforce_internal_host(host: str) -> None:
    """ADR-0043 §6 leak guard: only the configured internal GitLab is allowed."""
    bare = host.split("@")[-1].lower()
    if bare.split(":")[0] in _PUBLIC_HOST_DENYLIST:
        raise GitlabMrError(
            f"refusing NDA MR to public host {host!r} (ADR-0043 §6 leak guard)"
        )
    if bare != _internal_host().lower():
        raise GitlabMrError(
            f"refusing NDA MR to non-internal host {host!r}; "
            f"only {_internal_host()!r} is allowed (ADR-0043 §6 leak guard)"
        )


def _resolve_git_account(account_id: str, *, tenant_id: Optional[str]) -> dict:
    if not account_id:
        raise GitlabMrError("git_account_ref is required")

    async def _pick() -> dict | None:
        from backend import git_credentials

        return await git_credentials.pick_by_id(account_id, tenant_id=tenant_id)

    try:
        row = asyncio.run(_pick())
    except RuntimeError as exc:
        raise GitlabMrError(
            "open_contribution_mr must be called from synchronous runner code"
        ) from exc
    if row is None:
        raise GitlabMrError(f"git_accounts row {account_id!r} not found")
    return row


def _parse_project_url(repo_url: str) -> _GitlabRef:
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
            host = parsed.netloc.rsplit("@", 1)[-1] if "@" in parsed.netloc else (parsed.netloc or "")
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if not host or len(parts) < 2:
        raise GitlabMrError(f"cannot derive GitLab namespace/project from {repo_url!r}")
    return _GitlabRef(host=host, project_path="/".join(parts))


def _read_worktree_origin(worktree: Path) -> str:
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=worktree, text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise GitlabMrError(
            f"could not read worktree origin (cwd={worktree}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    url = (proc.stdout or "").strip()
    if not url:
        raise GitlabMrError(
            f"worktree (cwd={worktree}) has no origin remote; the caller must set "
            "origin to the target repo URL before invoking open_contribution_mr"
        )
    return url


def _set_origin_url(worktree: Path, url: str) -> None:
    _run_git(worktree, ["git", "remote", "set-url", "origin", url])


def _push_branch(worktree: Path, branch: str) -> None:
    argv = ["git", "push", "origin", f"{branch}:{branch}"]
    if "--force" in argv or any(arg.startswith("+") for arg in argv):
        raise GitlabMrError("force-push argv rejected")
    _run_git(worktree, argv)


def _run_git(worktree: Path, argv: list[str]) -> None:
    proc = subprocess.run(argv, cwd=worktree, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise GitlabMrError(f"{argv[:2]} failed: {(proc.stderr or proc.stdout).strip()}")


def _gitlab_headers(token: str) -> dict[str, str]:
    return {"PRIVATE-TOKEN": token, "Accept": "application/json"}


def _open_or_update_mr(
    *,
    repo: _GitlabRef,
    token: str,
    branch: str,
    base: str,
    title: str,
    description: str,
) -> dict:
    mrs_url = f"{repo.api_base}/projects/{repo.encoded_path}/merge_requests"
    headers = _gitlab_headers(token)
    with httpx.Client(timeout=30.0, verify=_verify_tls()) as client:
        existing = client.get(
            mrs_url,
            headers=headers,
            params={"state": "opened", "source_branch": branch, "target_branch": base},
        )
        _raise_for_gitlab(existing, "list merge requests")
        matches = existing.json()
        if matches:
            iid = int(matches[0]["iid"])
            response = client.put(
                f"{mrs_url}/{iid}",
                headers=headers,
                json={"title": title, "description": description, "target_branch": base},
            )
        else:
            response = client.post(
                mrs_url,
                headers=headers,
                json={
                    "source_branch": branch,
                    "target_branch": base,
                    "title": title,
                    "description": description,
                    "remove_source_branch": True,
                },
            )
        _raise_for_gitlab(response, "write merge request")
        return response.json()


def _raise_for_gitlab(response: httpx.Response, action: str) -> None:
    if response.status_code >= 400:
        # Never echo the token; GitLab error bodies don't carry it, but keep the
        # message bounded.
        detail = (response.text or "")[:400]
        raise GitlabMrError(f"GitLab API failed to {action}: HTTP {response.status_code} {detail}")
