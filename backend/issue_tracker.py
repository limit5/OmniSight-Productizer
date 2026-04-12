"""Unified issue tracker client for GitHub Issues, GitLab Issues, and Jira.

Provides a platform-agnostic interface for:
- Updating issue status (open/closed or platform-specific transitions)
- Posting comments on issues
- Creating new issues

Platform detection is automatic from the issue URL.
All operations are async and fail gracefully (never block the caller).
"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import quote_plus, urlparse

from backend.config import settings
from backend.git_auth import detect_platform, parse_repo_path

logger = logging.getLogger(__name__)

# Map internal TaskStatus → external status per platform
_STATUS_MAP_GITHUB = {
    "completed": "closed",
    "blocked": "closed",
    "in_review": "open",
    "in_progress": "open",
    "assigned": "open",
    "backlog": "open",
}

_STATUS_MAP_GITLAB = _STATUS_MAP_GITHUB.copy()  # Same open/closed model

_STATUS_MAP_JIRA = {
    "assigned": "In Progress",
    "in_progress": "In Progress",
    "in_review": "In Review",
    "completed": "Done",
    "blocked": "Blocked",
    "backlog": "To Do",
}


async def sync_issue_status(issue_url: str, new_status: str, comment: str = "") -> dict:
    """Update an external issue's status based on the URL's platform.

    Args:
        issue_url: The full URL of the external issue.
        new_status: Internal TaskStatus value (e.g. "in_progress", "completed").
        comment: Optional comment to add alongside the status change.

    Returns:
        dict with ``status`` ("ok" or "error") and optional ``message``.
    """
    if not issue_url:
        return {"status": "skipped", "message": "No issue URL"}

    platform = _detect_platform_from_issue_url(issue_url)

    try:
        if platform == "github":
            return await _sync_github(issue_url, new_status, comment)
        elif platform == "gitlab":
            return await _sync_gitlab(issue_url, new_status, comment)
        elif platform == "jira":
            return await _sync_jira(issue_url, new_status, comment)
        else:
            return {"status": "skipped", "message": f"Unknown platform for {issue_url}"}
    except Exception as exc:
        logger.warning("Issue sync failed for %s: %s", issue_url, exc)
        return {"status": "error", "message": str(exc)}


async def post_issue_comment(issue_url: str, comment: str) -> dict:
    """Post a comment on an external issue."""
    if not issue_url or not comment:
        return {"status": "skipped"}

    platform = _detect_platform_from_issue_url(issue_url)
    try:
        if platform == "github":
            return await _comment_github(issue_url, comment)
        elif platform == "gitlab":
            return await _comment_gitlab(issue_url, comment)
        elif platform == "jira":
            return await _comment_jira(issue_url, comment)
        else:
            return {"status": "skipped", "message": f"Unknown platform for {issue_url}"}
    except Exception as exc:
        logger.warning("Issue comment failed for %s: %s", issue_url, exc)
        return {"status": "error", "message": str(exc)}


def _detect_platform_from_issue_url(url: str) -> str:
    """Detect platform from an issue URL (not a git remote URL)."""
    lower = url.lower()
    if "github.com" in lower:
        return "github"
    if "gitlab" in lower:
        return "gitlab"
    if settings.gitlab_url and urlparse(settings.gitlab_url).hostname in lower:
        return "gitlab"
    if settings.notification_jira_url and urlparse(settings.notification_jira_url).hostname in lower:
        return "jira"
    # Check for common Jira URL patterns
    if "/browse/" in lower or "/rest/api/" in lower:
        return "jira"
    return "unknown"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GitHub Issues (via gh CLI)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _parse_github_issue_url(url: str) -> tuple[str, str]:
    """Extract (owner/repo, issue_number) from GitHub issue URL."""
    # https://github.com/owner/repo/issues/42
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 4 and parts[2] == "issues":
        return f"{parts[0]}/{parts[1]}", parts[3]
    return "", ""


async def _sync_github(issue_url: str, status: str, comment: str) -> dict:
    import os
    repo, number = _parse_github_issue_url(issue_url)
    if not repo or not number:
        return {"status": "error", "message": f"Cannot parse GitHub issue URL: {issue_url}"}

    gh_state = _STATUS_MAP_GITHUB.get(status, "open")
    env = {**os.environ}
    if settings.github_token:
        env["GITHUB_TOKEN"] = settings.github_token

    # Update state
    proc = await asyncio.create_subprocess_exec(
        "gh", "issue", "edit", number, "--repo", repo, "--remove-label", "", "--add-label", f"status:{status}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    await asyncio.wait_for(proc.communicate(), timeout=15)

    # Close/reopen if needed
    if gh_state == "closed":
        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "close", number, "--repo", repo,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)
    else:
        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "reopen", number, "--repo", repo,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)

    if comment:
        await _comment_github(issue_url, comment)

    return {"status": "ok", "platform": "github", "issue": f"{repo}#{number}"}


async def _comment_github(issue_url: str, comment: str) -> dict:
    import os
    repo, number = _parse_github_issue_url(issue_url)
    if not repo or not number:
        return {"status": "error", "message": "Cannot parse GitHub issue URL"}

    env = {**os.environ}
    if settings.github_token:
        env["GITHUB_TOKEN"] = settings.github_token

    proc = await asyncio.create_subprocess_exec(
        "gh", "issue", "comment", number, "--repo", repo, "--body", comment,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)
    return {"status": "ok" if proc.returncode == 0 else "error"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GitLab Issues (REST API v4)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _parse_gitlab_issue_url(url: str) -> tuple[str, str, str]:
    """Extract (api_base, project_path_encoded, issue_iid) from GitLab issue URL."""
    # https://gitlab.com/group/project/-/issues/42
    parsed = urlparse(url)
    api_base = f"{parsed.scheme}://{parsed.hostname}"
    parts = parsed.path.strip("/").split("/")
    # Find "issues" keyword, then the number after it
    try:
        issues_idx = parts.index("issues")
        iid = parts[issues_idx + 1] if issues_idx + 1 < len(parts) else ""
    except (ValueError, IndexError):
        return "", "", ""
    # Project path is everything before "-" or "issues"
    dash_idx = parts.index("-") if "-" in parts else issues_idx
    project_path = "/".join(parts[:dash_idx])
    return api_base, quote_plus(project_path), iid


async def _sync_gitlab(issue_url: str, status: str, comment: str) -> dict:
    api_base, project_enc, iid = _parse_gitlab_issue_url(issue_url)
    if not project_enc or not iid:
        return {"status": "error", "message": f"Cannot parse GitLab issue URL: {issue_url}"}

    token = settings.gitlab_token
    if not token:
        return {"status": "error", "message": "No GitLab token configured"}

    gl_state = _STATUS_MAP_GITLAB.get(status, "opened")
    state_event = "close" if gl_state == "closed" else "reopen"
    api_url = f"{api_base}/api/v4/projects/{project_enc}/issues/{iid}"

    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "PUT", api_url,
        "-H", f"PRIVATE-TOKEN: {token}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"state_event": state_event, "labels": f"status:{status}"}),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=15)

    if comment:
        await _comment_gitlab(issue_url, comment)

    return {"status": "ok", "platform": "gitlab", "issue": f"{iid}"}


async def _comment_gitlab(issue_url: str, comment: str) -> dict:
    api_base, project_enc, iid = _parse_gitlab_issue_url(issue_url)
    if not project_enc or not iid:
        return {"status": "error"}

    token = settings.gitlab_token
    if not token:
        return {"status": "error", "message": "No GitLab token"}

    api_url = f"{api_base}/api/v4/projects/{project_enc}/issues/{iid}/notes"
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST", api_url,
        "-H", f"PRIVATE-TOKEN: {token}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"body": comment}),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)
    return {"status": "ok" if proc.returncode == 0 else "error"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Jira (REST API v2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _parse_jira_issue_url(url: str) -> tuple[str, str]:
    """Extract (base_url, issue_key) from Jira issue URL."""
    # https://jira.company.com/browse/OMNI-123
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        base += f":{parsed.port}"
    parts = parsed.path.strip("/").split("/")
    if "browse" in parts:
        idx = parts.index("browse")
        if idx + 1 < len(parts):
            return base, parts[idx + 1]
    return base, ""


async def _sync_jira(issue_url: str, status: str, comment: str) -> dict:
    base, issue_key = _parse_jira_issue_url(issue_url)
    if not issue_key:
        return {"status": "error", "message": f"Cannot parse Jira issue URL: {issue_url}"}

    token = settings.notification_jira_token
    if not token:
        return {"status": "error", "message": "No Jira token configured"}

    # Jira requires transition IDs — we need to query available transitions first
    transitions_url = f"{base}/rest/api/2/issue/{issue_key}/transitions"
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", transitions_url,
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    try:
        data = json.loads(stdout.decode())
        transitions = data.get("transitions", [])
    except (json.JSONDecodeError, Exception):
        transitions = []

    # Find matching transition
    target_name = _STATUS_MAP_JIRA.get(status, "")
    transition_id = None
    for t in transitions:
        if t.get("name", "").lower() == target_name.lower() or target_name.lower() in t.get("name", "").lower():
            transition_id = t["id"]
            break

    if not transition_id:
        available = [t.get("name", "?") for t in transitions]
        logger.warning("Jira: no transition found for '%s' on %s. Available: %s", target_name, issue_key, available)
        return {"status": "error", "message": f"No Jira transition for '{target_name}'. Available: {available}"}

    do_url = f"{base}/rest/api/2/issue/{issue_key}/transitions"
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-w", "\n%{http_code}", "-X", "POST", do_url,
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"transition": {"id": transition_id}}),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    out = stdout.decode(errors="replace").strip()
    # Last line is HTTP status code from -w flag
    lines = out.rsplit("\n", 1)
    http_code = lines[-1].strip() if len(lines) > 1 else "0"
    if not http_code.startswith("2"):
        body = lines[0] if len(lines) > 1 else out
        logger.warning("Jira transition failed for %s: HTTP %s — %s", issue_key, http_code, body[:200])
        return {"status": "error", "message": f"Jira transition HTTP {http_code}"}

    if comment:
        await _comment_jira(issue_url, comment)

    return {"status": "ok", "platform": "jira", "issue": issue_key}


async def _comment_jira(issue_url: str, comment: str) -> dict:
    base, issue_key = _parse_jira_issue_url(issue_url)
    if not issue_key:
        return {"status": "error"}

    token = settings.notification_jira_token
    if not token:
        return {"status": "error", "message": "No Jira token"}

    api_url = f"{base}/rest/api/2/issue/{issue_key}/comment"
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST", api_url,
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"body": comment}),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)
    return {"status": "ok" if proc.returncode == 0 else "error"}
