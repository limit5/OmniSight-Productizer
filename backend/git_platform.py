"""Unified PR/MR creation for GitHub and GitLab.

GitHub: uses ``gh`` CLI (must be installed and authenticated).
GitLab: uses REST API v4 with Personal Access Token.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import quote_plus

from backend.git_auth import detect_platform, parse_repo_path, get_gitlab_api_url
from backend.git_credentials import pick_account_for_url, pick_default

logger = logging.getLogger(__name__)


async def create_merge_request(
    repo_path: Path,
    remote: str,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str = "",
) -> dict:
    """Create a PR (GitHub) or MR (GitLab) based on the remote's platform.

    Returns a dict with ``url``, ``id``, ``platform``, or ``error``.
    """
    # Get remote URL
    proc = await asyncio.create_subprocess_shell(
        f'git remote get-url "{remote}"',
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    remote_url = stdout.decode(errors="replace").strip()
    if not remote_url:
        return {"error": f"Could not resolve remote URL for '{remote}'"}

    platform = detect_platform(remote_url)

    if platform == "github":
        return await _create_github_pr(
            repo_path, remote_url, source_branch, target_branch, title, description,
        )
    if platform == "gitlab":
        return await _create_gitlab_mr(
            remote_url, source_branch, target_branch, title, description,
        )

    return {"error": f"Unsupported platform for remote URL: {remote_url}"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GitHub — via gh CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _create_github_pr(
    repo_path: Path,
    remote_url: str,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
) -> dict:
    """Create a GitHub Pull Request using the ``gh`` CLI."""
    repo_slug = parse_repo_path(remote_url)

    # Build env with GitHub token — Phase 5-6 (#multi-account-forge):
    # resolve via ``pick_account_for_url`` so a ``url_patterns`` match
    # on the remote URL wins over the platform default, and so
    # operator-added ``git_accounts`` rows are honoured. Legacy
    # ``settings.github_token`` flows in through the resolver's
    # internal ``_build_registry`` fallback when ``git_accounts`` is
    # empty — no separate fallback branch is needed here.
    import os
    env = {**os.environ}
    account = await pick_account_for_url(remote_url)
    if account is None:
        account = await pick_default("github")
    token = (account or {}).get("token") or ""
    if token:
        env["GITHUB_TOKEN"] = token

    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "create",
        "--repo", repo_slug,
        "--head", source_branch,
        "--base", target_branch,
        "--title", title,
        "--body", description,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()

    if proc.returncode != 0:
        # Check if gh is installed
        if "command not found" in err or "not found" in err:
            return {"error": "gh CLI not installed. Install: https://cli.github.com/"}
        return {"error": f"GitHub PR creation failed: {err or out}"}

    # gh pr create outputs the PR URL
    pr_url = out.strip().splitlines()[-1] if out else ""
    return {
        "platform": "github",
        "url": pr_url,
        "repo": repo_slug,
        "source": source_branch,
        "target": target_branch,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GitLab — via REST API v4
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _create_gitlab_mr(
    remote_url: str,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
) -> dict:
    """Create a GitLab Merge Request using the REST API."""
    # Phase 5-6 (#multi-account-forge): prefer a ``url_patterns``
    # match on the remote URL over the platform default so self-
    # hosted instances with custom org/user accounts are picked
    # correctly. The resolver falls back to the legacy shim (which
    # reads ``settings.gitlab_token``) when ``git_accounts`` has no
    # matching row, preserving backward-compatible behaviour during
    # the rollout ramp.
    account = await pick_account_for_url(remote_url)
    if account is None:
        account = await pick_default("gitlab")
    token = (account or {}).get("token") or ""
    if not token:
        return {"error": "No OMNISIGHT_GITLAB_TOKEN configured"}

    api_base = get_gitlab_api_url(remote_url)
    project_path = parse_repo_path(remote_url)
    encoded_path = quote_plus(project_path)

    api_url = f"{api_base}/api/v4/projects/{encoded_path}/merge_requests"

    payload = json.dumps({
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
        "remove_source_branch": False,
    })

    # Token and payload via env vars to avoid process list exposure
    import os
    env = {**os.environ, "_GL_TOKEN": token}
    cmd_safe = (
        'curl -s -w "\\n%{http_code}" -X POST "$_GL_API_URL"'
        ' -H "Content-Type: application/json"'
        ' -H "PRIVATE-TOKEN: $_GL_TOKEN"'
        ' -d "$_GL_PAYLOAD"'
    )
    env["_GL_API_URL"] = api_url
    env["_GL_PAYLOAD"] = payload

    proc = await asyncio.create_subprocess_shell(
        cmd_safe,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    out = stdout.decode(errors="replace").strip()

    # Last line is HTTP status code
    lines = out.rsplit("\n", 1)
    body = lines[0] if len(lines) > 1 else out
    status_code = lines[-1].strip() if len(lines) > 1 else "0"

    if not status_code.startswith("2"):
        try:
            err_data = json.loads(body)
            msg = err_data.get("message", err_data.get("error", body))
        except json.JSONDecodeError:
            msg = body
        return {"error": f"GitLab API {status_code}: {msg}"}

    try:
        mr_data = json.loads(body)
        return {
            "platform": "gitlab",
            "url": mr_data.get("web_url", ""),
            "id": mr_data.get("iid"),
            "repo": project_path,
            "source": source_branch,
            "target": target_branch,
        }
    except json.JSONDecodeError:
        return {"error": f"Failed to parse GitLab response: {body[:200]}"}
